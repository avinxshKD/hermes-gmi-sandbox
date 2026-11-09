"""Backend selection and profile-scoped configuration."""

import importlib.util

from agent.secret_scope import get_secret_str
from agent.terminal_env_provider import TerminalEnvironmentProvider


class GmiSandboxProvider(TerminalEnvironmentProvider):
    name = "gmi_sandbox"
    display_name = "GMI Cloud Sandbox"
    is_remote = True
    is_container = True
    supports_streams = False
    session_isolated_when_nonpersistent = True
    skip_container_guards = False
    default_cwd = "/workspace"

    def __init__(self, ctx):
        self.ctx = ctx

    @property
    def description(self):
        return "Run terminal and file operations in an ephemeral GMI Cloud sandbox."

    @property
    def strip_env_keys(self):
        return frozenset({"GMI_SANDBOX_API_KEY", "GMI_AGENTBOX_API_KEY"})

    def probe(self):
        if importlib.util.find_spec("sandbox_sdk") is None:
            return "needs_setup", "Install the plugin's gmi-sandbox-sdk dependency."
        if not get_secret_str("GMI_SANDBOX_API_KEY"):
            return "needs_setup", "Set GMI_SANDBOX_API_KEY to a GMI Compute API key."
        if not self.ctx.get_config("template_id", ""):
            return "needs_setup", "Set the plugin's template_id to a ready GMI Sandbox template."
        return "ready", ""

    def is_available(self):
        return self.probe()[0] == "ready"

    def setup_instructions(self):
        return [
            "Set GMI_SANDBOX_API_KEY to a Compute API key, not an inference key.",
            "Set plugins.entries.gmi-sandbox.settings.template_id to a ready template.",
            "Set terminal.container_persistent to false; sandboxes are deleted on cleanup.",
            "Sandbox runtime is billed to your GMI account. See the plugin README.",
        ]

    def create_environment(self, *, cwd, timeout, task_id="default", image=None,
                           container_config=None, **kwargs):
        if image:
            raise ValueError("GMI Sandbox uses template_id; per-task image overrides are not supported.")
        if (container_config or {}).get("container_persistent", False):
            raise ValueError("GMI Sandbox is ephemeral; set terminal.container_persistent to false.")
        status, reason = self.probe()
        if status != "ready":
            raise ValueError(reason)

        from sandbox_sdk import SandboxClient, SandboxSDKError

        from .environment import GmiSandboxEnvironment

        try:
            client = SandboxClient(
                api_key=get_secret_str("GMI_SANDBOX_API_KEY"),
                base_url="https://console.gmicloud.ai/api/v2",
                timeout=10,
            )
        except (SandboxSDKError, ValueError):
            raise RuntimeError("Could not initialize the GMI Sandbox client; check plugin configuration") from None
        return GmiSandboxEnvironment(
            client=client, template_id=self.ctx.get_config("template_id"),
            idc_name=self.ctx.get_config("idc_name", "") or None,
            lifetime_seconds=self.ctx.get_config("lifetime_seconds", 900),
            cwd=cwd, timeout=timeout, task_id=task_id,
        )
