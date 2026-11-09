"""GMI command executions adapted to Hermes's shell environment."""

import logging
import os
import posixpath
import shlex
import subprocess
import threading
import time
import uuid

from agent.memory_provider import spawn_context_thread
from sandbox_sdk import APIError, NotFoundError, SandboxSDKError
from tools.environments.base import BaseEnvironment

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {"succeeded", "failed", "error", "canceled", "cancelled", "timed_out"}


def sdk_failure(operation, error):
    if isinstance(error, APIError):
        return RuntimeError(f"GMI Sandbox {operation} failed (HTTP {error.status_code}); check credentials, quota and service status")
    return RuntimeError(f"GMI Sandbox {operation} failed; the remote outcome may be unknown")


class GmiCommand:
    """A process handle for one remote execution; Hermes owns waiting and interrupts."""

    def __init__(self, sandbox, command, timeout):
        self.sandbox = sandbox
        self.command = command
        self.timeout = timeout
        self.execution = None
        self.returncode = None
        self._lock = threading.Lock()
        self._cancelled = threading.Event()
        self._done = threading.Event()
        self._cancel_lock = threading.Lock()
        self.cancel_error = None
        read_fd, self._write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, "r", encoding="utf-8", errors="replace")
        self._thread = spawn_context_thread(self._run, name="gmi-sandbox-command")

    def start(self):
        self._thread.start()
        return self

    def _run(self):
        try:
            output, code = self._execute()
        except APIError as error:
            output, code = str(sdk_failure("execution request", error)) + "\n", 1
            try:
                self.kill()
            except RuntimeError:
                output += "Cancellation could not be confirmed; the server-side timeout still applies.\n"
        except Exception:
            # SDK errors may contain URLs, response bodies, or credentials.
            output, code = "GMI Sandbox command failed; its outcome may be unknown. Do not retry blindly.\n", 1
            try:
                self.kill()
            except RuntimeError:
                output += "Cancellation could not be confirmed; the server-side timeout still applies.\n"
        try:
            data = output.encode("utf-8", errors="replace")
            while data:
                written = os.write(self._write_fd, data)
                data = data[written:]
        except OSError:
            logger.debug("GMI command output reader closed")
        finally:
            os.close(self._write_fd)
            self.returncode = code
            self._done.set()

    def _execute(self):
        if self._cancelled.is_set():
            return "", 130
        execution = self.sandbox.commands.run(
            self.command, wait=False, execution_timeout_seconds=self.timeout,
        )
        with self._lock:
            self.execution = execution
        if self._cancelled.is_set():
            self.kill()

        deadline = time.monotonic() + self.timeout
        while execution.status not in _TERMINAL_STATUSES:
            if self._cancelled.is_set():
                self.kill()
                return "", 130
            if time.monotonic() >= deadline:
                self.kill()
                return "GMI Sandbox command timed out.\n", 124
            execution.refresh()
            if execution.status not in _TERMINAL_STATUSES:
                self._cancelled.wait(0.25)

        if execution.stdout_truncated or execution.stderr_truncated:
            return "GMI Sandbox truncated command output; redirect large output to a file.\n", 1
        output = (execution.stdout or "") + (execution.stderr or "")
        if execution.status == "timed_out":
            return output + "\n[GMI execution timed out]\n", 124
        if execution.status in {"cancelled", "canceled"}:
            return output, 130
        code = execution.exit_code
        return output, code if isinstance(code, int) else 1

    def poll(self):
        return self.returncode if self._done.is_set() else None

    def wait(self, timeout=None):
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired("GMI Sandbox execution", timeout)
        return self.returncode

    def kill(self):
        self._cancelled.set()
        with self._cancel_lock:
            with self._lock:
                execution = self.execution
            if execution is None or execution.status in _TERMINAL_STATUSES:
                return
            try:
                execution.cancel()
                deadline = time.monotonic() + 2
                while execution.status not in _TERMINAL_STATUSES:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Cancellation pending")
                    execution.refresh()
                    self._done.wait(0.1)
            except Exception:
                self.cancel_error = "GMI command cancellation is unconfirmed; the server-side timeout still applies"
                raise RuntimeError(self.cancel_error) from None


class GmiSandboxEnvironment(BaseEnvironment):
    _stdin_mode = "heredoc"
    uses_host_filesystem = False

    def __init__(self, *, client, template_id, idc_name, lifetime_seconds, cwd, timeout, task_id):
        if not isinstance(cwd, str) or not cwd.startswith("/") or posixpath.normpath(cwd) != cwd:
            raise ValueError("GMI sandbox cwd must be an absolute, normalized path")
        if isinstance(lifetime_seconds, bool) or not isinstance(lifetime_seconds, int) or lifetime_seconds < 60:
            raise ValueError("lifetime_seconds must be an integer of at least 60 seconds")
        self._command_timeout(timeout)
        super().__init__(cwd=cwd, timeout=timeout)
        self._lock = threading.Lock()
        self._sandbox = None
        self._closed = False
        self._commands = []
        self._lifetime = max(lifetime_seconds, timeout + 60)
        try:
            self._sandbox = client.sandboxes.create(
                template_id=template_id, idc_name=idc_name, timeout=self._lifetime,
                metadata={"integration": "hermes-gmi-sandbox", "task_id": task_id},
                idempotency_key=uuid.uuid4().hex,
            )
            self._sandbox.wait_until_running(timeout=300)
            self._sandbox.connect()
            check = self._sandbox.commands.run(
                f"mkdir -p -- {shlex.quote(cwd)} && command -v bash && command -v python3",
                wait=False, execution_timeout_seconds=30,
            ).wait(timeout=30)
            if check.exit_code != 0:
                raise RuntimeError("GMI sandbox template must provide bash and python3")
            self.init_session()
            if not self._snapshot_ready:
                raise RuntimeError("Could not initialize the remote bash session")
        except Exception:
            self.cleanup()
            raise RuntimeError("GMI Sandbox startup failed; check Compute credentials, template, IDC, and quota") from None

    @staticmethod
    def _command_timeout(timeout):
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 3600:
            raise ValueError("GMI command timeout must be an integer from 1 to 3600 seconds")
        return timeout

    def _run_bash(self, cmd_string, *, login=False, timeout=120, stdin_data=None):
        timeout = self._command_timeout(timeout)
        if stdin_data is not None:
            cmd_string = self._embed_stdin_heredoc(cmd_string, stdin_data)
        command = f"bash {'-l ' if login else ''}-c {shlex.quote(cmd_string)}"
        with self._lock:
            if self._closed:
                raise RuntimeError("GMI sandbox has been cleaned up")
            if any(p.cancel_error for p in self._commands):
                raise RuntimeError("GMI sandbox has an unconfirmed cancellation; clean it up before running more commands")
            try:
                self._sandbox.set_timeout(max(self._lifetime, timeout + 60))
            except SandboxSDKError as error:
                raise sdk_failure("lifetime renewal", error) from None
            self._commands = [p for p in self._commands if p.poll() is None]
            process = GmiCommand(self._sandbox, command, timeout)
            self._commands.append(process)
            return process.start()

    def cleanup(self):
        if not hasattr(self, "_lock"):
            return
        with self._lock:
            self._closed = True
            for process in self._commands:
                try:
                    process.kill()
                except RuntimeError:
                    logger.warning("Deleting sandbox after an unconfirmed command cancellation")
            if self._sandbox is None:
                return
            try:
                result = self._sandbox.delete()
            except NotFoundError:
                self._sandbox = None
            except SandboxSDKError:
                logger.warning("GMI sandbox deletion failed; retry cleanup or delete it in the GMI Console")
                raise RuntimeError("Could not delete GMI sandbox; its configured lifetime still applies") from None
            else:
                if result.get("data", {}).get("state") != "deleted":
                    logger.warning("GMI sandbox deletion is pending; verify resource release in the GMI Console")
                    raise RuntimeError("GMI sandbox deletion has not completed; retry cleanup to confirm release")
                self._sandbox = None
