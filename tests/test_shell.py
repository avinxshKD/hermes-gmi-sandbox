import json
import os
import shutil
import subprocess

import pytest
from gmi_plugin.environment import GmiSandboxEnvironment
from test_environment import GmiTransport, client


class ShellTransport(GmiTransport):
    def __call__(self, method, url, headers, body, timeout):
        if "/executions?" in url:
            command = json.loads(body)["parameters"]["command"]
            result = subprocess.run(
                ["/bin/bash", "-c", command], text=True, capture_output=True, timeout=10,
                env={"PATH": os.defpath, "HOME": self.home, "LANG": "C.UTF-8"},
            )
            self.results.append({"status": "succeeded" if result.returncode == 0 else "failed",
                                 "exit_code": result.returncode, "stdout": result.stdout,
                                 "stderr": result.stderr})
        return super().__call__(method, url, headers, body, timeout)


@pytest.mark.skipif(os.name != "posix" or not shutil.which("bash"), reason="requires POSIX bash")
def test_real_shell_cwd_exports_and_payload_stdin(tmp_path):
    class TestEnvironment(GmiSandboxEnvironment):
        def get_temp_dir(self):
            return str(tmp_path)

    transport = ShellTransport()
    transport.home = str(tmp_path)
    env = TestEnvironment(client=client(transport), template_id="template", idc_name=None,
                          lifetime_seconds=900, cwd=str(tmp_path), timeout=60, task_id="task")
    try:
        result = env.execute("mkdir project; cd project; export GMI_TEST_VALUE=kept",
                             rewrite_compound_background=False)
        assert result["returncode"] == 0 and env.cwd == str(tmp_path / "project")
        result = env.execute("printf %s \"$GMI_TEST_VALUE\"", rewrite_compound_background=False)
        assert result["output"] == "kept" and result["returncode"] == 0
        payload = "quotes ' $HOME\nno trailing newline"
        result = env.execute("cat > input.txt", stdin_data=payload, rewrite_compound_background=False)
        assert result["returncode"] == 0
        assert (tmp_path / "project" / "input.txt").read_text() == payload
        result = env.execute("exit 7", rewrite_compound_background=False)
        assert result["returncode"] == 7
    finally:
        env.cleanup()
