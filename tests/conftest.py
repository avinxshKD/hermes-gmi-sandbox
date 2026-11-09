import importlib.util
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "gmi_plugin", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)],
)
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)


@pytest.fixture(autouse=True)
def isolated_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.delenv("GMI_SANDBOX_API_KEY", raising=False)
    monkeypatch.delenv("GMI_AGENTBOX_API_KEY", raising=False)

    def no_network(*args, **kwargs):
        raise AssertionError("Offline tests must not open network connections")

    monkeypatch.setattr(socket.socket, "connect", no_network)


class PluginDirectory:
    @pytest.hookimpl(tryfirst=True)
    def pytest_collect_directory(self, path, parent):
        if Path(path).resolve() == ROOT:
            return pytest.Dir.from_parent(parent, path=path)


def pytest_configure(config):
    config.pluginmanager.register(PluginDirectory(), "gmi-plugin-directory")
