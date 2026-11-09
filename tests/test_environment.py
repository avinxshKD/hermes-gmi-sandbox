import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from gmi_plugin.environment import GmiCommand, GmiSandboxEnvironment
from sandbox_sdk import SandboxClient
from sandbox_sdk._transport import RawResponse


class GmiTransport:
    def __init__(self):
        self.calls = []
        self.executions = {}
        self.results = []
        self.startup_error = False
        self.deleted = False
        self.delete_state = "deleted"
        self.submit_error = False
        self.cancelled = threading.Event()
        self.submitted = threading.Event()
        self.release_submit = None
        self.keep_running = False
        self.polled = threading.Event()
        self.cancel_fails = False
        self.renewal_fails = False
        self.cancel_pending = False

    def __call__(self, method, url, headers, body, timeout):
        payload = json.loads(body) if body else None
        self.calls.append((method, url, dict(headers), payload))
        status = 200
        if url.endswith("/sandboxes"):
            result = {"data": {"id": "sandbox", "state": "running"}}
        elif url.endswith("/connect"):
            if self.startup_error:
                status, result = 403, {"message": "secret-startup-token"}
            else:
                result = {"data": {"sandbox_key": "box", "domain": "example.invalid",
                                   "sandbox_access_token": "data-token"}}
        elif url.endswith("/timeout"):
            if self.renewal_fails:
                status, result = 401, {"message": "secret-renewal-token"}
            else:
                result = {}
        elif method == "DELETE":
            self.deleted = True
            result = {"data": {"state": self.delete_state}}
        elif "/executions?" in url:
            if self.submit_error:
                raise OSError("secret-token in a lost submission response")
            execution_id = f"exec-{len(self.executions)}"
            result = {"execution_id": execution_id, "status": "running"}
            final = self.results.pop(0) if self.results else {
                "status": "succeeded", "exit_code": 0, "stdout": "", "stderr": "",
            }
            self.executions[execution_id] = {"execution_id": execution_id, **final}
            self.submitted.set()
            if self.release_submit is not None:
                assert self.release_submit.wait(5)
        elif url.endswith("/cancel"):
            if self.cancel_fails:
                raise OSError("secret-cancellation-token")
            execution_id = url.split("/")[-2]
            self.executions[execution_id].update(status="cancelled", exit_code=None)
            self.cancelled.set()
            result = ({"execution_id": execution_id, "status": "cancelling"}
                      if self.cancel_pending else self.executions[execution_id])
        else:
            execution_id = url.rsplit("/", 1)[-1]
            self.polled.set()
            result = ({"execution_id": execution_id, "status": "running"}
                      if self.keep_running else self.executions[execution_id])
        return RawResponse(status_code=status, headers={}, body=json.dumps(result).encode())


def client(transport):
    return SandboxClient(api_key="compute-key", transport=transport)


def connected_sandbox(transport):
    sandbox = client(transport).sandboxes.create(template_id="template")
    return sandbox.connect()


def read_command(command):
    with ThreadPoolExecutor(max_workers=1) as pool:
        output = pool.submit(command.stdout.read)
        command.wait(5)
        result = output.result(5), command.returncode
        command.stdout.close()
        return result


def test_command_uses_data_plane_token_and_preserves_exit_status():
    transport = GmiTransport()
    sandbox = connected_sandbox(transport)
    transport.results = [{"status": "failed", "exit_code": 7, "stdout": "hello\n", "stderr": "failure\n"}]
    result = read_command(GmiCommand(sandbox, "printf hello; exit 7", 60).start())
    assert result == ("hello\nfailure\n", 7)
    commands = [c for c in transport.calls if "/executions?" in c[1]]
    assert len(commands) == 1
    assert commands[0][1].startswith("https://box.example.invalid/")
    assert commands[0][2]["X-Access-Token"] == "data-token"
    assert "Authorization" not in commands[0][2]
    assert commands[0][3]["parameters"]["execution_timeout_seconds"] == 60


@pytest.mark.parametrize("field", ["stdout_truncated", "stderr_truncated"])
def test_truncated_output_is_not_returned_as_a_complete_file(field):
    transport = GmiTransport()
    sandbox = connected_sandbox(transport)
    transport.results = [{"status": "succeeded", "exit_code": 0, "stdout": "partial-file", field: True}]
    output, code = read_command(GmiCommand(sandbox, "cat file", 60).start())
    assert code != 0 and "truncated" in output
    assert "partial-file" not in output


def test_cancel_during_submission_cancels_the_returned_execution():
    transport = GmiTransport()
    sandbox = connected_sandbox(transport)
    transport.release_submit = threading.Event()
    process = GmiCommand(sandbox, "sleep 60", 60).start()
    try:
        assert transport.submitted.wait(5)
        process.kill()
    finally:
        transport.release_submit.set()
    output, code = read_command(process)
    assert code == 130
    assert transport.cancelled.is_set()
    assert len([c for c in transport.calls if "/executions?" in c[1]]) == 1


def test_running_command_can_be_interrupted_without_deleting_the_sandbox():
    transport = GmiTransport()
    sandbox = connected_sandbox(transport)
    transport.keep_running = True
    process = GmiCommand(sandbox, "sleep 60", 60).start()
    try:
        assert transport.polled.wait(5)
    finally:
        process.kill()
    assert read_command(process)[1] == 130
    assert transport.cancelled.is_set() and not transport.deleted


def test_startup_failure_deletes_the_sandbox_and_hides_sdk_error():
    transport = GmiTransport()
    transport.startup_error = True
    with pytest.raises(RuntimeError, match="startup failed") as error:
        GmiSandboxEnvironment(client=client(transport), template_id="template", idc_name=None,
                              lifetime_seconds=900, cwd="/workspace", timeout=60, task_id="task")
    assert transport.deleted
    assert "secret-startup-token" not in str(error.value)


def test_environment_dispatches_bootstrap_and_cleans_up_once():
    transport = GmiTransport()
    env = GmiSandboxEnvironment(client=client(transport), template_id="template", idc_name=None,
                                lifetime_seconds=900, cwd="/workspace", timeout=60, task_id="task")
    try:
        assert env._snapshot_ready
        assert not env.uses_host_filesystem
        process = env._run_bash("cat", timeout=60, stdin_data="payload without newline")
        assert read_command(process)[1] == 0
        commands = [c[3]["parameters"]["command"] for c in transport.calls if "/executions?" in c[1]]
        assert any("payload without newline" in command for command in commands)
        assert any("hermes-snap-" in command for command in commands)
        create = transport.calls[0][3]
        assert create["timeout"] == 900 and create["metadata"]["task_id"] == "task"
        assert "env_vars" not in create
    finally:
        env.cleanup()
        env.cleanup()
    assert len([c for c in transport.calls if c[0] == "DELETE"]) == 1
    with pytest.raises(RuntimeError, match="cleaned up"):
        env._run_bash("echo no", timeout=60)


def test_execute_strips_hermes_markers_and_keeps_working_directory():
    transport = GmiTransport()
    env = GmiSandboxEnvironment(client=client(transport), template_id="template", idc_name=None,
                                lifetime_seconds=900, cwd="/workspace", timeout=60, task_id="task")
    try:
        transport.results = [{"status": "succeeded", "exit_code": 0, "stdout": (
            f"hello\n{env._pipe_marker}0{env._pipe_marker}\n"
            f"{env._cwd_marker}/workspace/project{env._cwd_marker}\n"
        )}]
        result = env.execute("mkdir -p project; cd project; printf hello",
                             rewrite_compound_background=False)
        assert result["returncode"] == 0 and result["output"].strip() == "hello"
        assert result["cwd"] == env.cwd == "/workspace/project"
        assert result["pipeline_status"] == [0]
    finally:
        env.cleanup()


@pytest.mark.parametrize("timeout", [0, -1, True, 3601])
def test_invalid_command_timeout_does_not_create_a_sandbox(timeout):
    transport = GmiTransport()
    with pytest.raises(ValueError, match="timeout"):
        GmiSandboxEnvironment(client=client(transport), template_id="template", idc_name=None,
                              lifetime_seconds=900, cwd="/workspace", timeout=timeout, task_id="task")
    assert transport.calls == []


def test_lost_submission_response_is_not_retried_or_exposed():
    transport = GmiTransport()
    sandbox = connected_sandbox(transport)
    transport.submit_error = True
    output, code = read_command(GmiCommand(sandbox, "billable-command", 60).start())
    assert code == 1 and "unknown" in output
    assert "secret-token" not in output
    assert len([c for c in transport.calls if "/executions?" in c[1]]) == 1


@pytest.mark.parametrize("status,code", [("timed_out", 124), ("cancelled", 130), ("failed", 1)])
def test_terminal_execution_without_exit_code_is_never_success(status, code):
    transport = GmiTransport()
    sandbox = connected_sandbox(transport)
    transport.results = [{"status": status, "exit_code": None}]
    assert read_command(GmiCommand(sandbox, "command", 60).start())[1] == code


def test_pending_deletion_retains_the_sandbox_for_cleanup_retry():
    transport = GmiTransport()
    env = GmiSandboxEnvironment(client=client(transport), template_id="template", idc_name=None,
                                lifetime_seconds=900, cwd="/workspace", timeout=60, task_id="task")
    transport.delete_state = "deleting"
    with pytest.raises(RuntimeError, match="not completed"):
        env.cleanup()
    assert env._sandbox is not None
    transport.delete_state = "deleted"
    env.cleanup()
    assert env._sandbox is None


def test_lifetime_renewal_failure_is_sanitized():
    transport = GmiTransport()
    env = GmiSandboxEnvironment(client=client(transport), template_id="template", idc_name=None,
                                lifetime_seconds=900, cwd="/workspace", timeout=60, task_id="task")
    try:
        transport.renewal_fails = True
        with pytest.raises(RuntimeError, match="HTTP 401") as error:
            env._run_bash("echo no", timeout=60)
        assert "secret-renewal-token" not in str(error.value)
    finally:
        env.cleanup()


def test_failed_cancellation_is_not_reported_as_confirmed():
    transport = GmiTransport()
    sandbox = connected_sandbox(transport)
    transport.keep_running = True
    transport.cancel_fails = True
    process = GmiCommand(sandbox, "sleep 60", 60).start()
    assert transport.polled.wait(5)
    with pytest.raises(RuntimeError, match="unconfirmed") as error:
        process.kill()
    assert "secret-cancellation-token" not in str(error.value)
    assert process.cancel_error is not None
    assert read_command(process)[1] != 130


def test_cancellation_polls_past_the_accepted_response():
    transport = GmiTransport()
    sandbox = connected_sandbox(transport)
    transport.release_submit = threading.Event()
    transport.cancel_pending = True
    process = GmiCommand(sandbox, "sleep 60", 60).start()
    assert transport.submitted.wait(5)
    process.kill()
    transport.release_submit.set()
    assert read_command(process)[1] == 130
    assert transport.cancelled.is_set() and transport.polled.is_set()
