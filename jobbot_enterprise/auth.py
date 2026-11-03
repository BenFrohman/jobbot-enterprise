# Copyright (c) 2026 Benjamin Stanley Frohman. All rights reserved.
# Proprietary and confidential. No use, copying or distribution without written permission.
"""API keys, roles and the operator token. Keys are stored only as SHA-256 hashes."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from fastapi import HTTPException, Request

ROLES = {
    # role: allowed scopes
    "admin": {"postings:write", "postings:read", "candidates:read", "match", "feedback", "webhooks", "audit",
              "widget:write", "keys"},
    "recruiter": {"postings:write", "postings:read", "candidates:read", "match", "feedback"},
    "auditor": {"audit", "postings:read"},
    # Publishable key embedded in the careers page: may only list open roles and submit applications.
    "widget": {"public"},
}


def new_key(role: str) -> str:
    prefix = "jbp" if role == "widget" else "jbk"
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_id(kind: str) -> str:
    return f"{kind}_{secrets.token_hex(8)}"


@dataclass
class Principal:
    tenant_id: str
    key_id: str
    role: str

    @property
    def actor(self) -> str:
        return f"key:{self.key_id}"

    def need(self, scope: str) -> None:
        if scope not in ROLES[self.role]:
            raise HTTPException(403, f"API key role '{self.role}' lacks scope '{scope}'")


def principal(request: Request) -> Principal:
    db = request.app.state.db
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
    if not token:
        raise HTTPException(401, "Missing API key")
    row = db.one("SELECT id, tenant_id, role FROM api_keys WHERE key_hash=? AND revoked_at IS NULL",
                 hash_token(token))
    if row is None:
        raise HTTPException(401, "Invalid API key")
    return Principal(row["tenant_id"], row["id"], row["role"])


def require_operator(request: Request) -> None:
    expected = request.app.state.settings.operator_token
    got = request.headers.get("x-operator-token", "")
    if not expected or not hmac.compare_digest(expected, got):
        raise HTTPException(401, "Operator token required")
