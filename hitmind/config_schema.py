"""HitMind memory-provider config surface for the Hermes Desktop config panel.

Hermes loads this by path (never as a package) from the provider directory. The
web route ``GET/PUT /api/memory/providers/hitmind/config`` renders these fields
and, with ``storage=flat_json``, reads/writes ``$HERMES_HOME/hitmind/config.json``
— so the provider needs no read/write code. The adapter reads that same file as the
highest-precedence value source (then ``HITMIND_*`` env, then built-in default).

The field list is GENERATED from ``hitmind.settings_spec.SETTINGS`` — the single
source of truth shared with the Hermes adapter and the DSH daemon — so defaults and
names cannot drift. This module imports ONLY ``plugins.memory.config_schema`` plus
the stdlib-only spec (loaded via importlib because this file is loaded by path, not
imported as a package).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from plugins.memory.config_schema import (
    KIND_BOOL,
    KIND_NUMBER,
    KIND_SECRET,
    KIND_SELECT,
    KIND_TEXT,
    STORAGE_FLAT_JSON,
    ProviderConfigSchema,
    ProviderField,
    ProviderFieldOption,
)


def _load_spec():
    """Load ``settings_spec.py`` beside this file without importing the package.

    Registered in ``sys.modules`` first: ``settings_spec`` uses ``@dataclass``, and a
    dataclass resolves ``sys.modules[cls.__module__]`` while processing — a module that
    is exec'd but never registered fails there. Hermes loads ``config_schema.py`` by
    path, so this loader must be self-sufficient.
    """
    import sys
    spec_path = Path(__file__).resolve().parent / "settings_spec.py"
    mod_name = "hitmind_settings_spec"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    mod_spec = importlib.util.spec_from_file_location(mod_name, spec_path)
    mod = importlib.util.module_from_spec(mod_spec)
    sys.modules[mod_name] = mod
    mod_spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_SPEC = _load_spec()

_KIND_TO_HERMES = {
    _SPEC.K_TEXT: KIND_TEXT,
    _SPEC.K_NUMBER: KIND_NUMBER,
    _SPEC.K_BOOL: KIND_BOOL,
    _SPEC.K_SELECT: KIND_SELECT,
    _SPEC.K_SECRET: KIND_SECRET,
}


def _to_provider_field(s) -> ProviderField:
    """Project one spec Setting onto a Hermes ProviderField."""
    if s.kind == _SPEC.K_BOOL:
        default = "true" if s.default else "false"
    else:
        default = str(s.default)
    kwargs = {}
    if s.secret or s.kind == _SPEC.K_SECRET:
        kwargs["env_key"] = s.env
    if s.kind == _SPEC.K_SELECT and s.options:
        kwargs["options"] = tuple(ProviderFieldOption(v, label) for v, label in s.options)
    return ProviderField(
        key=s.key,
        label=s.label,
        kind=_KIND_TO_HERMES.get(s.kind, KIND_TEXT),
        default=default,
        description=s.description,
        group=s.group,
        inline=s.inline,
        **kwargs,
    )


CONFIG_SCHEMA = ProviderConfigSchema(
    name="hitmind",
    label="HitMind 长期记忆",
    storage=STORAGE_FLAT_JSON,
    docs_url="",
    fields=tuple(_to_provider_field(s) for s in _SPEC.SETTINGS),
)
