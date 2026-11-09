"""Run Hermes's plugin admission checks without changing a user profile."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--hermes", type=Path, required=True)
args = parser.parse_args()
with tempfile.TemporaryDirectory(prefix="gmi-plugin-validate-", dir=os.environ.get("TEMP")) as home:
    env = {key: value for key, value in os.environ.items() if key.upper() in {
        "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "HOME", "USERPROFILE",
    }}
    env.update(HERMES_HOME=home, PYTHONPATH=str(args.hermes.resolve()))
    code = (
        "from pathlib import Path; from hermes_cli.plugin_validate import validate_plugin_dir; "
        "import json; r=validate_plugin_dir(Path.cwd()); "
        "print(json.dumps(r.to_dict())); raise SystemExit(r.exit_code)"
    )
    result = subprocess.run([sys.executable, "-c", code], env=env,
                            cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True)
    if result.stdout:
        print(json.dumps(json.loads(result.stdout), indent=2))
    if result.stderr:
        print(result.stderr, file=sys.stderr)
sys.exit(result.returncode)
