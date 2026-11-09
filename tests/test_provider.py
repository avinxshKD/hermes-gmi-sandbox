from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from agent.secret_scope import reset_multiplex_context, reset_secret_scope, set_multiplex_context, set_secret_scope
from agent.terminal_env_provider import TerminalEnvironmentProvider
from gmi_plugin import register
from gmi_plugin.provider import GmiSandboxProvider


def context(**settings):
    return SimpleNamespace(get_config=lambda key, default=None: settings.get(key, default))


def test_registers_only_the_terminal_backend():
    ctx = context()
    ctx.register_terminal_environment_provider = Mock()
    register(ctx)
    provider = ctx.register_terminal_environment_provider.call_args.args[0]
    assert isinstance(provider, TerminalEnvironmentProvider)
    assert provider.name == "gmi_sandbox"
    assert provider.is_remote and provider.is_container
    assert not provider.supports_streams and not provider.skip_container_guards
    assert "GMI_SANDBOX_API_KEY" in provider.strip_env_keys


def test_probe_requires_scoped_credentials_and_template(monkeypatch):
    monkeypatch.setenv("GMI_SANDBOX_API_KEY", "launch-profile-key")
    routed = set_multiplex_context(True)
    scope = set_secret_scope({})
    try:
        provider = GmiSandboxProvider(context(template_id="template"))
        assert provider.probe()[0] == "needs_setup"
    finally:
        reset_secret_scope(scope)
        reset_multiplex_context(routed)
    scope = set_secret_scope({"GMI_SANDBOX_API_KEY": "profile-key"})
    try:
        assert GmiSandboxProvider(context()).probe()[0] == "needs_setup"
        assert GmiSandboxProvider(context(template_id="template")).probe() == ("ready", "")
    finally:
        reset_secret_scope(scope)


@pytest.mark.parametrize("kwargs,message", [
    ({"image": "ubuntu:22.04"}, "image overrides"),
    ({"container_config": {"container_persistent": True}}, "container_persistent"),
])
def test_unsupported_options_fail_before_creating_resources(kwargs, message):
    provider = GmiSandboxProvider(context(template_id="template"))
    with pytest.raises(ValueError, match=message):
        provider.create_environment(cwd="/workspace", timeout=60, **kwargs)


def test_factory_passes_active_profile_key_and_plugin_settings(monkeypatch):
    from gmi_plugin import environment
    from sandbox_sdk import SandboxClient

    create = Mock(return_value="environment")
    monkeypatch.setattr(environment, "GmiSandboxEnvironment", create)
    monkeypatch.setenv("GMI_SANDBOX_API_KEY", "wrong-profile-key")
    monkeypatch.setenv("GMI_SANDBOX_BASE_URL", "https://wrong-endpoint.invalid")
    scope = set_secret_scope({"GMI_SANDBOX_API_KEY": "active-profile-key"})
    try:
        provider = GmiSandboxProvider(context(template_id="template", idc_name="chosen-idc", lifetime_seconds=600))
        assert provider.create_environment(cwd="/workspace", timeout=60, task_id="task") == "environment"
    finally:
        reset_secret_scope(scope)
    kwargs = create.call_args.kwargs
    assert isinstance(kwargs["client"], SandboxClient)
    assert kwargs["client"].api_key == "active-profile-key"
    assert kwargs["client"].base_url == "https://console.gmicloud.ai/api/v2"
    assert kwargs["template_id"] == "template" and kwargs["idc_name"] == "chosen-idc"
    assert kwargs["lifetime_seconds"] == 600 and kwargs["task_id"] == "task"
