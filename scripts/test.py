"""Run offline plugin tests against an existing Hermes checkout."""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--hermes", type=Path, required=True)
args, pytest_args = parser.parse_known_args()
hermes = args.hermes.resolve()
if not (hermes / "agent" / "terminal_env_provider.py").is_file():
    parser.error("--hermes must point to a Hermes Agent checkout")

env = {key: value for key, value in os.environ.items() if key.upper() in {
    "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "HOME", "USERPROFILE",
}}
with tempfile.TemporaryDirectory(prefix="gmi-plugin-tests-", dir=os.environ.get("TEMP")) as home:
    env.update(HERMES_HOME=home, PYTHONPATH=str(hermes), PYTHONHASHSEED="0", TZ="UTC")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", *pytest_args],
        cwd=Path(__file__).resolve().parents[1], env=env,
    )
sys.exit(result.returncode)
