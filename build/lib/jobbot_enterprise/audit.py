# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Benjamin Stanley Frohman
"""Tamper-evident audit log: each entry hashes the previous one, per tenant."""
from __future__ import annotations

import hashlib

from .db import DB, dumps, now


def record(db: DB, tenant_id: str, actor: str, action: str, target: str = "", detail: dict | None = None) -> None:
    with db.lock:
        prev = db.one("SELECT hash FROM audit_log WHERE tenant_id=? ORDER BY seq DESC LIMIT 1", tenant_id)
        prev_hash = prev["hash"] if prev else "genesis"
        at = now()
        body = dumps({"t": tenant_id, "a": actor, "x": action, "g": target, "d": detail or {}, "at": at,
                      "p": prev_hash})
        h = hashlib.sha256(body.encode()).hexdigest()
        db.x("INSERT INTO audit_log (tenant_id, actor, action, target, detail, at, prev_hash, hash) "
             "VALUES (?,?,?,?,?,?,?,?)", tenant_id, actor, action, target, dumps(detail or {}), at, prev_hash, h)


def verify_chain(db: DB, tenant_id: str) -> bool:
    import json
    prev = "genesis"
    for r in db.q("SELECT * FROM audit_log WHERE tenant_id=? ORDER BY seq", tenant_id):
        body = dumps({"t": r["tenant_id"], "a": r["actor"], "x": r["action"], "g": r["target"],
                      "d": json.loads(r["detail"]), "at": r["at"], "p": prev})
        if r["prev_hash"] != prev or hashlib.sha256(body.encode()).hexdigest() != r["hash"]:
            return False
        prev = r["hash"]
    return True
