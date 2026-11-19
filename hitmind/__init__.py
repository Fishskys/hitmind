"""HitMind — local-first, lightweight, multi-agent long-term memory service.

Host-agnostic core engine; Hermes adapter and MCP server are thin bindings over it.

This package root is also the Hermes *memory-provider plugin directory*: when Hermes
points ``memory.provider: hitmind`` at a directory copy of this package, its loader
imports this ``__init__`` and calls ``register(ctx)``, which in turn calls
``ctx.register_memory_provider(<HitMind provider>)``. The Hermes import surface
(``agent.memory_provider``, ``hermes_cli.plugins``) is only touched inside that call,
so plain ``import hitmind`` (MCP, tests, gateway) never needs Hermes on the path.
"""

from .core.engine import Engine

__version__ = "0.1.3"


def register(ctx) -> None:
    """Hermes memory-provider plugin entry point (called by Hermes' loader)."""
    from .adapters.hermes import register as _register
    _register(ctx)


__all__ = ["Engine", "__version__", "register"]

