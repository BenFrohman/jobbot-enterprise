# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Benjamin Stanley Frohman
"""Outbound webhooks signed with HMAC-SHA256 (Stripe-style `t=...,v1=...`)."""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx

from .db import DB, dumps, now
from .auth import new_id

EVENTS = ("application.created", "match.ready", "consent.updated")
TOLERANCE = 300


def sign(secret: str, body: bytes, ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify(secret: str, body: bytes, header: str, tolerance: int = TOLERANCE, now_ts: int | None = None) -> bool:
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs((now_ts or int(time.time())) - ts) > tolerance:
        return False
    expected = sign(secret, body, ts).split("v1=", 1)[1]
    return hmac.compare_digest(expected, parts.get("v1", ""))


def emit(db: DB, http: httpx.Client, tenant_id: str, event: str, data: dict, retries: int = 3,
         sleep=lambda attempt: time.sleep(attempt)) -> None:
    hooks = db.q("SELECT * FROM webhooks WHERE tenant_id=?", tenant_id)
    for h in hooks:
        if event not in json.loads(h["events"]):
            continue
        delivery = new_id("dlv")
        body = json.dumps({"id": delivery, "type": event, "tenant_id": tenant_id, "created": int(now()),
                           "data": data}, separators=(",", ":")).encode()
        status, attempts = None, 0
        for attempts in range(1, retries + 1):
            try:
                r = http.post(h["url"], content=body, headers={"Content-Type": "application/json",
                              "Jobbot-Signature": sign(h["secret"], body), "Jobbot-Event": event,
                              "Jobbot-Delivery": delivery}, timeout=10)
                status = r.status_code
                if r.status_code < 500 and r.status_code != 429:
                    break
            except httpx.HTTPError:
                status = None
            if attempts < retries:
                sleep(attempts)
        db.x("INSERT INTO deliveries VALUES (?,?,?,?,?,?,?)", delivery, tenant_id, h["id"], event, status,
             attempts, now())


__all__ = ["EVENTS", "sign", "verify", "emit", "dumps"]
