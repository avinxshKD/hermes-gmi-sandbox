"""GMI Cloud sandbox terminal backend for Hermes Agent."""

from .provider import GmiSandboxProvider


def register(ctx):
    ctx.register_terminal_environment_provider(GmiSandboxProvider(ctx))
