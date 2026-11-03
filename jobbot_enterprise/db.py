# Copyright (c) 2026 Benjamin Stanley Frohman. All rights reserved.
# Proprietary and confidential. No use, copying or distribution without written permission.
"""SQLite storage. Every tenant-owned table carries tenant_id and every query filters on it."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (id TEXT PRIMARY KEY, name TEXT NOT NULL, region TEXT NOT NULL,
  retention_days INTEGER NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS api_keys (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
  name TEXT NOT NULL, role TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE, created_at REAL NOT NULL,
  revoked_at REAL);
CREATE TABLE IF NOT EXISTS postings (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, title TEXT NOT NULL,
  description TEXT NOT NULL, location TEXT NOT NULL, remote INTEGER NOT NULL, team TEXT NOT NULL,
  status TEXT NOT NULL, comp_min INTEGER, comp_max INTEGER, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS candidates (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, token_hash TEXT NOT NULL,
  profile TEXT NOT NULL, created_at REAL NOT NULL, deleted_at REAL);
CREATE TABLE IF NOT EXISTS consents (candidate_id TEXT PRIMARY KEY REFERENCES candidates(id),
  tenant_id TEXT NOT NULL, fields TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS applications (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, posting_id TEXT NOT NULL,
  candidate_id TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE (posting_id, candidate_id));
CREATE TABLE IF NOT EXISTS feedback (tenant_id TEXT NOT NULL, posting_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
  action TEXT NOT NULL, actor TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY (posting_id, candidate_id));
CREATE TABLE IF NOT EXISTS webhooks (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, url TEXT NOT NULL,
  secret TEXT NOT NULL, events TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, webhook_id TEXT NOT NULL,
  event TEXT NOT NULL, status INTEGER, attempts INTEGER NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit_log (seq INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
  actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, detail TEXT NOT NULL, at REAL NOT NULL,
  prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS widget_config (tenant_id TEXT PRIMARY KEY, config TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_postings_tenant ON postings(tenant_id, status);
CREATE INDEX IF NOT EXISTS ix_apps_tenant ON applications(tenant_id, posting_id);
CREATE INDEX IF NOT EXISTS ix_audit_tenant ON audit_log(tenant_id, seq);
"""


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.lock = threading.RLock()

    def q(self, sql: str, *args) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, *args):
        r = self.q(sql, *args)
        return r[0] if r else None

    def x(self, sql: str, *args) -> int:
        with self.lock:
            return self.conn.execute(sql, args).rowcount


def now() -> float:
    return time.time()


def dumps(v) -> str:
    return json.dumps(v, separators=(",", ":"), sort_keys=True)
