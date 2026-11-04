# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Benjamin Stanley Frohman
"""Plugin system.

A plugin is an object (or zero-arg class) with ``name``, ``kind`` and ``description`` attributes plus the
method for its kind:

* ``source``   ``fetch(config: dict, http: httpx.Client) -> list[dict]``  (postings to import)
* ``matcher``  ``rank(query: dict, docs: list[Doc], *, boost, limit) -> list[Result]``
* ``sink``     ``deliver(event: str, payload: dict, config: dict, http: httpx.Client) -> None``
* ``export``   ``export(rows: list[dict]) -> tuple[str, bytes]``  (media type, body)

Third-party packages register plugins under the entry-point group ``jobbot_enterprise.plugins``.
Loading is fail-safe: a plugin that raises on import or is malformed is reported, never loaded.
Each tenant enables/disables plugins and stores its config through ``/v1/plugins``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any

log = logging.getLogger("jobbot_enterprise.plugins")

GROUP = "jobbot_enterprise.plugins"
KINDS = {"source": "fetch", "matcher": "rank", "sink": "deliver", "export": "export"}


class PluginError(Exception):
    pass


def validate(obj: Any) -> Any:
    if isinstance(obj, type):
        obj = obj()
    name, kind = getattr(obj, "name", None), getattr(obj, "kind", None)
    if not isinstance(name, str) or not name.replace("-", "").replace("_", "").isalnum() or len(name) > 64:
        raise PluginError(f"invalid plugin name: {name!r}")
    if kind not in KINDS:
        raise PluginError(f"plugin {name}: unknown kind {kind!r}")
    if not callable(getattr(obj, KINDS[kind], None)):
        raise PluginError(f"plugin {name}: kind {kind} requires a callable {KINDS[kind]}()")
    return obj


@dataclass
class Registry:
    plugins: dict[str, Any] = field(default_factory=dict)
    origin: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    def add(self, obj: Any, origin: str = "builtin") -> None:
        p = validate(obj)
        if p.name in self.plugins:
            raise PluginError(f"duplicate plugin name: {p.name}")
        self.plugins[p.name] = p
        self.origin[p.name] = origin

    def get(self, name: str, kind: str | None = None):
        p = self.plugins.get(name)
        if p is None or (kind and p.kind != kind):
            return None
        return p

    def of_kind(self, kind: str) -> list[Any]:
        return [p for p in self.plugins.values() if p.kind == kind]

    def describe(self) -> list[dict]:
        return [{"name": p.name, "kind": p.kind, "description": getattr(p, "description", ""),
                 "origin": self.origin[p.name], "default_enabled": bool(getattr(p, "default_enabled", False)),
                 "config_schema": getattr(p, "config_schema", {})} for p in self.plugins.values()]


def load(extra: list[Any] | None = None, discover: bool = True, allow: set[str] | None = None) -> Registry:
    """Built-ins, then entry points (optionally restricted to ``allow``), then ``extra`` objects."""
    from . import builtin
    reg = Registry()
    for obj in builtin.ALL:
        reg.add(obj)
    if discover:
        for ep in entry_points(group=GROUP):
            if allow is not None and ep.name not in allow:
                continue
            try:
                reg.add(ep.load(), origin=f"entry-point:{ep.value}")
            except Exception as e:  # never let a third-party plugin take the service down
                reg.errors[ep.name] = f"{type(e).__name__}: {e}"
                log.warning("plugin %s failed to load: %s", ep.name, e)
    for obj in extra or []:
        try:
            reg.add(obj, origin="extra")
        except Exception as e:
            reg.errors[str(getattr(obj, "name", obj))] = f"{type(e).__name__}: {e}"
    return reg
