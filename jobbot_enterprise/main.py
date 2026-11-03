# Copyright (c) 2026 Benjamin Stanley Frohman. All rights reserved.
# Proprietary and confidential. No use, copying or distribution without written permission.
"""FastAPI service: the Jobbot Enterprise hiring engine (see docs/openapi.yaml)."""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlparse

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from . import __version__, audit, consent, webhooks
from .auth import ROLES, Principal, hash_token, new_id, new_key, principal, require_operator
from .db import DB, dumps, now
from .matching import Doc, rank, tokenize

STATIC = Path(__file__).parent / "static"


@dataclass
class Settings:
    db_path: str = field(default_factory=lambda: os.getenv("JOBBOT_DB_PATH", "./data/jobbot.db"))
    operator_token: str = field(default_factory=lambda: os.getenv("JOBBOT_OPERATOR_TOKEN", ""))
    cors_origins: list[str] = field(default_factory=lambda: [o.strip() for o in
                                    os.getenv("JOBBOT_CORS_ORIGINS", "").split(",") if o.strip()])
    allow_http_webhooks: bool = field(default_factory=lambda: os.getenv("JOBBOT_ALLOW_HTTP_WEBHOOKS") == "1")
    public_rate_limit: int = 30  # per minute per key+IP on public endpoints


class RateLimiter:
    def __init__(self):
        self.hits: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def allow(self, key: str, limit: int, window: float = 60.0) -> bool:
        t = time.monotonic()
        with self.lock:
            xs = [h for h in self.hits.get(key, []) if t - h < window]
            ok = len(xs) < limit
            if ok:
                xs.append(t)
            self.hits[key] = xs
            return ok


# ---------- request models ----------
class TenantIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    region: str = Field(default="us", max_length=32)
    retention_days: int = Field(default=365, ge=1, le=3650)


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    role: Literal["admin", "recruiter", "auditor", "widget"]


class PostingIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=50_000)
    location: str = Field(default="", max_length=200)
    remote: bool = False
    team: str = Field(default="", max_length=200)
    status: Literal["draft", "open", "closed"] = "draft"
    comp_min: Optional[int] = Field(default=None, ge=0)
    comp_max: Optional[int] = Field(default=None, ge=0)


class PostingPatch(BaseModel):
    title: Optional[str] = Field(default=None, max_length=300)
    description: Optional[str] = Field(default=None, max_length=50_000)
    location: Optional[str] = Field(default=None, max_length=200)
    remote: Optional[bool] = None
    team: Optional[str] = Field(default=None, max_length=200)
    status: Optional[Literal["draft", "open", "closed"]] = None
    comp_min: Optional[int] = Field(default=None, ge=0)
    comp_max: Optional[int] = Field(default=None, ge=0)


class ProfileIn(BaseModel):
    name: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=320)
    phone: str = Field(default="", max_length=50)
    location: str = Field(default="", max_length=200)
    links: str = Field(default="", max_length=2000)
    skills: str = Field(default="", max_length=5000)
    experience: str = Field(default="", max_length=20_000)
    values: str = Field(default="", max_length=5000)
    causes: str = Field(default="", max_length=2000)
    desired_impact: str = Field(default="", max_length=5000)
    comp_expectation: Optional[int] = Field(default=None, ge=0)
    resume_text: str = Field(default="", max_length=100_000)


class ApplyIn(BaseModel):
    posting_id: str
    profile: ProfileIn
    consent_fields: list[str] = Field(default_factory=lambda: list(consent.DEFAULT_GRANT), max_length=20)


class ConsentIn(BaseModel):
    fields: list[str] = Field(max_length=20)


class MatchIn(BaseModel):
    limit: int = Field(default=20, ge=1, le=200)
    remote_ok: bool = True
    comp_ceiling: Optional[int] = Field(default=None, ge=0)


class FeedbackIn(BaseModel):
    candidate_id: str
    action: Literal["advance", "reject", "clear"]


class WebhookIn(BaseModel):
    url: str = Field(max_length=2000)
    events: list[Literal["application.created", "match.ready", "consent.updated"]] = Field(min_length=1)


class WidgetConfig(BaseModel):
    company_name: str = Field(default="", max_length=200)
    accent_color: str = Field(default="#4f46e5", pattern=r"^#[0-9a-fA-F]{6}$")
    intro: str = Field(default="", max_length=2000)
    ask_fields: list[str] = Field(default_factory=lambda: ["name", "email", "skills", "experience", "causes"])
    default_consent: list[str] = Field(default_factory=lambda: list(consent.DEFAULT_GRANT))
    allowed_origins: list[str] = Field(default_factory=list, max_length=20)


def _posting(r) -> dict:
    return {"id": r["id"], "title": r["title"], "description": r["description"], "location": r["location"],
            "remote": bool(r["remote"]), "team": r["team"], "status": r["status"], "comp_min": r["comp_min"],
            "comp_max": r["comp_max"], "created_at": r["created_at"], "updated_at": r["updated_at"]}


def create_app(settings: Settings | None = None, http: httpx.Client | None = None,
               webhook_backoff: float = 1.0) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="Jobbot Enterprise", version=__version__,
                  description="Proprietary. Copyright (c) 2026 Benjamin Stanley Frohman. All rights reserved.")
    app.state.settings = settings
    app.state.db = db = DB(settings.db_path)
    app.state.http = http or httpx.Client(follow_redirects=False)
    limiter = RateLimiter()
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["GET", "POST"],
                           allow_headers=["Authorization", "Content-Type", "X-Api-Key", "X-Candidate-Token"])

    def emit(tenant_id: str, event: str, data: dict) -> None:
        def run():
            webhooks.emit(db, app.state.http, tenant_id, event, data, sleep=lambda a: time.sleep(webhook_backoff * a))
        return run

    def staff(scope: str):
        def dep(p: Principal = Depends(principal)) -> Principal:
            p.need(scope)
            return p
        return dep

    def public(request: Request, p: Principal = Depends(principal)) -> Principal:
        ip = request.client.host if request.client else "?"
        if not limiter.allow(f"{p.key_id}:{ip}", settings.public_rate_limit):
            raise HTTPException(429, "Too many requests")
        return p  # every role may use the public surface

    def candidate(request: Request):
        tok = request.headers.get("x-candidate-token", "")
        row = db.one("SELECT * FROM candidates WHERE token_hash=? AND deleted_at IS NULL", hash_token(tok)) if tok else None
        if row is None:
            raise HTTPException(401, "Invalid candidate token")
        return row

    def get_posting(tid: str, pid: str):
        r = db.one("SELECT * FROM postings WHERE id=? AND tenant_id=?", pid, tid)
        if r is None:
            raise HTTPException(404, "Posting not found")
        return r

    def grant_of(cid: str) -> list[str]:
        r = db.one("SELECT fields FROM consents WHERE candidate_id=?", cid)
        return json.loads(r["fields"]) if r else []

    def candidate_view(tid: str, cid: str) -> dict:
        r = db.one("SELECT * FROM candidates WHERE id=? AND tenant_id=? AND deleted_at IS NULL", cid, tid)
        if r is None:
            raise HTTPException(404, "Candidate not found")
        grant = grant_of(cid)
        return {"id": cid, "consented_fields": grant, "profile": consent.visible(json.loads(r["profile"]), grant)}

    # ---------- ops ----------
    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": __version__}

    @app.get("/widget.js")
    def widget_js():
        return FileResponse(STATIC / "widget.js", media_type="application/javascript")

    # ---------- tenants & keys ----------
    @app.post("/v1/tenants", status_code=201, dependencies=[Depends(require_operator)])
    def create_tenant(body: TenantIn):
        tid, kid, key = new_id("ten"), new_id("key"), new_key("admin")
        db.x("INSERT INTO tenants VALUES (?,?,?,?,?)", tid, body.name, body.region, body.retention_days, now())
        db.x("INSERT INTO api_keys VALUES (?,?,?,?,?,?,NULL)", kid, tid, "bootstrap", "admin", hash_token(key), now())
        audit.record(db, tid, "operator", "tenant.created", tid, {"name": body.name, "region": body.region})
        return {"id": tid, "name": body.name, "region": body.region, "retention_days": body.retention_days,
                "admin_key": key}

    @app.get("/v1/tenant")
    def get_tenant(p: Principal = Depends(principal)):
        r = db.one("SELECT * FROM tenants WHERE id=?", p.tenant_id)
        return {"id": r["id"], "name": r["name"], "region": r["region"], "retention_days": r["retention_days"],
                "role": p.role}

    @app.post("/v1/api-keys", status_code=201)
    def create_key(body: KeyIn, p: Principal = Depends(staff("keys"))):
        kid, key = new_id("key"), new_key(body.role)
        db.x("INSERT INTO api_keys VALUES (?,?,?,?,?,?,NULL)", kid, p.tenant_id, body.name, body.role,
             hash_token(key), now())
        audit.record(db, p.tenant_id, p.actor, "api_key.created", kid, {"role": body.role})
        return {"id": kid, "name": body.name, "role": body.role, "key": key}

    @app.get("/v1/api-keys")
    def list_keys(p: Principal = Depends(staff("keys"))):
        return [{"id": r["id"], "name": r["name"], "role": r["role"], "created_at": r["created_at"],
                 "revoked": r["revoked_at"] is not None}
                for r in db.q("SELECT * FROM api_keys WHERE tenant_id=? ORDER BY created_at", p.tenant_id)]

    @app.delete("/v1/api-keys/{key_id}", status_code=204)
    def revoke_key(key_id: str, p: Principal = Depends(staff("keys"))):
        if not db.x("UPDATE api_keys SET revoked_at=? WHERE id=? AND tenant_id=?", now(), key_id, p.tenant_id):
            raise HTTPException(404, "Key not found")
        audit.record(db, p.tenant_id, p.actor, "api_key.revoked", key_id)

    # ---------- postings ----------
    @app.post("/v1/postings", status_code=201)
    def create_posting(body: PostingIn, p: Principal = Depends(staff("postings:write"))):
        pid, t = new_id("pst"), now()
        db.x("INSERT INTO postings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", pid, p.tenant_id, body.title, body.description,
             body.location, int(body.remote), body.team, body.status, body.comp_min, body.comp_max, t, t)
        audit.record(db, p.tenant_id, p.actor, "posting.created", pid, {"status": body.status})
        return _posting(get_posting(p.tenant_id, pid))

    @app.get("/v1/postings")
    def list_postings(status: Optional[str] = None, p: Principal = Depends(staff("postings:read"))):
        rows = db.q("SELECT * FROM postings WHERE tenant_id=? AND (? IS NULL OR status=?) ORDER BY created_at DESC",
                    p.tenant_id, status, status)
        return [_posting(r) for r in rows]

    @app.get("/v1/postings/{pid}")
    def read_posting(pid: str, p: Principal = Depends(staff("postings:read"))):
        return _posting(get_posting(p.tenant_id, pid))

    @app.patch("/v1/postings/{pid}")
    def update_posting(pid: str, body: PostingPatch, p: Principal = Depends(staff("postings:write"))):
        get_posting(p.tenant_id, pid)
        changes = body.model_dump(exclude_unset=True)
        for k, v in changes.items():  # keys come from the pydantic model, never from the client
            db.x(f"UPDATE postings SET {k}=?, updated_at=? WHERE id=? AND tenant_id=?",
                 int(v) if isinstance(v, bool) else v, now(), pid, p.tenant_id)
        audit.record(db, p.tenant_id, p.actor, "posting.updated", pid, {"fields": sorted(changes)})
        return _posting(get_posting(p.tenant_id, pid))

    @app.delete("/v1/postings/{pid}", status_code=204)
    def delete_posting(pid: str, p: Principal = Depends(staff("postings:write"))):
        get_posting(p.tenant_id, pid)
        db.x("UPDATE postings SET status='closed', updated_at=? WHERE id=? AND tenant_id=?", now(), pid, p.tenant_id)
        audit.record(db, p.tenant_id, p.actor, "posting.closed", pid)

    # ---------- public surface (widget key) ----------
    @app.get("/v1/public/postings")
    def public_postings(p: Principal = Depends(public)):
        rows = db.q("SELECT * FROM postings WHERE tenant_id=? AND status='open' ORDER BY created_at DESC", p.tenant_id)
        return [{k: v for k, v in _posting(r).items() if k not in ("status", "updated_at")} for r in rows]

    @app.get("/v1/public/widget/config")
    def public_widget_config(p: Principal = Depends(public)):
        r = db.one("SELECT config FROM widget_config WHERE tenant_id=?", p.tenant_id)
        return json.loads(r["config"]) if r else WidgetConfig().model_dump()

    @app.post("/v1/public/applications", status_code=201)
    def apply(body: ApplyIn, bg: BackgroundTasks, p: Principal = Depends(public)):
        post = get_posting(p.tenant_id, body.posting_id)
        if post["status"] != "open":
            raise HTTPException(409, "Posting is not open")
        try:
            grant = consent.clean_grant(body.consent_fields)
        except ValueError as e:
            raise HTTPException(422, str(e))
        cid, aid, token, t = new_id("cand"), new_id("app"), secrets.token_urlsafe(32), now()
        db.x("INSERT INTO candidates VALUES (?,?,?,?,?,NULL)", cid, p.tenant_id, hash_token(token),
             dumps(body.profile.model_dump()), t)
        db.x("INSERT INTO consents VALUES (?,?,?,?)", cid, p.tenant_id, dumps(grant), t)
        db.x("INSERT INTO applications VALUES (?,?,?,?,?,?)", aid, p.tenant_id, post["id"], cid, "new", t)
        audit.record(db, p.tenant_id, p.actor, "application.created", aid, {"candidate": cid, "consent": grant})
        bg.add_task(emit(p.tenant_id, "application.created",
                         {"application_id": aid, "posting_id": post["id"], "candidate_id": cid}))
        return {"application_id": aid, "candidate_id": cid, "candidate_token": token, "consented_fields": grant}

    # ---------- candidate self-service ----------
    @app.get("/v1/candidate/me")
    def me(c=Depends(candidate)):
        return {"id": c["id"], "profile": json.loads(c["profile"]), "consented_fields": grant_of(c["id"])}

    @app.put("/v1/candidate/me/consent")
    def set_consent(body: ConsentIn, bg: BackgroundTasks, c=Depends(candidate)):
        try:
            grant = consent.clean_grant(body.fields)
        except ValueError as e:
            raise HTTPException(422, str(e))
        db.x("UPDATE consents SET fields=?, updated_at=? WHERE candidate_id=?", dumps(grant), now(), c["id"])
        audit.record(db, c["tenant_id"], f"candidate:{c['id']}", "consent.updated", c["id"], {"fields": grant})
        bg.add_task(emit(c["tenant_id"], "consent.updated", {"candidate_id": c["id"], "fields": grant}))
        return {"id": c["id"], "consented_fields": grant}

    @app.delete("/v1/candidate/me", status_code=204)
    def erase_me(c=Depends(candidate)):
        db.x("UPDATE candidates SET profile='{}', deleted_at=? WHERE id=?", now(), c["id"])
        db.x("UPDATE consents SET fields='[]', updated_at=? WHERE candidate_id=?", now(), c["id"])
        audit.record(db, c["tenant_id"], f"candidate:{c['id']}", "candidate.erased", c["id"])

    @app.get("/v1/candidate/me/matches")
    def my_matches(limit: int = 10, c=Depends(candidate)):
        prof = json.loads(c["profile"])
        rows = db.q("SELECT * FROM postings WHERE tenant_id=? AND status='open'", c["tenant_id"])
        docs = [Doc(r["id"], f"{r['title']} {r['team']} {r['description']}", r["location"], bool(r["remote"]),
                    r["comp_max"]) for r in rows]
        q = {**prof, "comp_floor": prof.get("comp_expectation") or 0}
        return [{"posting_id": m.id, "score": m.score, "breakdown": m.breakdown, "why": " ".join(m.reasons),
                 "reasons": m.reasons} for m in rank(q, docs, limit=max(1, min(limit, 50)))]

    # ---------- staff views (consent-scoped) ----------
    @app.get("/v1/candidates")
    def list_candidates(posting_id: Optional[str] = None, p: Principal = Depends(staff("candidates:read"))):
        if posting_id:
            get_posting(p.tenant_id, posting_id)
            ids = [r["candidate_id"] for r in db.q(
                "SELECT candidate_id FROM applications WHERE tenant_id=? AND posting_id=? ORDER BY created_at",
                p.tenant_id, posting_id)]
        else:
            ids = [r["id"] for r in db.q("SELECT id FROM candidates WHERE tenant_id=? AND deleted_at IS NULL "
                                         "ORDER BY created_at", p.tenant_id)]
        out = []
        for cid in ids:
            try:
                out.append(candidate_view(p.tenant_id, cid))
            except HTTPException:
                continue
        audit.record(db, p.tenant_id, p.actor, "candidates.listed", posting_id or "*", {"count": len(out)})
        return out

    @app.get("/v1/candidates/{cid}")
    def read_candidate(cid: str, p: Principal = Depends(staff("candidates:read"))):
        v = candidate_view(p.tenant_id, cid)
        audit.record(db, p.tenant_id, p.actor, "candidate.viewed", cid, {"fields": v["consented_fields"]})
        return v

    @app.get("/v1/postings/{pid}/applications")
    def list_applications(pid: str, p: Principal = Depends(staff("candidates:read"))):
        get_posting(p.tenant_id, pid)
        return [{"id": r["id"], "candidate_id": r["candidate_id"], "status": r["status"], "created_at": r["created_at"]}
                for r in db.q("SELECT * FROM applications WHERE tenant_id=? AND posting_id=? ORDER BY created_at",
                              p.tenant_id, pid)]

    # ---------- matching ----------
    @app.post("/v1/postings/{pid}/match")
    def match(pid: str, body: MatchIn, bg: BackgroundTasks, p: Principal = Depends(staff("match"))):
        post = get_posting(p.tenant_id, pid)
        fb = {r["candidate_id"]: r["action"] for r in db.q(
            "SELECT candidate_id, action FROM feedback WHERE tenant_id=? AND posting_id=?", p.tenant_id, pid)}
        apps = db.q("SELECT a.candidate_id, c.profile FROM applications a JOIN candidates c ON c.id=a.candidate_id "
                    "WHERE a.tenant_id=? AND a.posting_id=? AND c.deleted_at IS NULL", p.tenant_id, pid)
        docs, advanced = [], []
        for r in apps:
            # Only consented, matchable fields are ever scored.
            v = consent.visible(json.loads(r["profile"]), grant_of(r["candidate_id"]))
            text = " ".join(str(v.get(k, "")) for k in consent.MATCHABLE)
            if fb.get(r["candidate_id"]) == "advance":
                advanced.append(set(tokenize(text)))
            if fb.get(r["candidate_id"]) == "reject":
                continue
            docs.append(Doc(r["candidate_id"], text, str(v.get("location", "")), body.remote_ok,
                            v.get("comp_expectation")))
        q = {"skills": f"{post['title']} {post['team']}", "experience": post["description"],
             "location": post["location"], "remote_ok": bool(post["remote"]) and body.remote_ok}

        def boost(cid: str) -> float:
            if not advanced:
                return 0.0
            toks = set(tokenize(next(d.text for d in docs if d.id == cid)))
            best = max(len(toks & a) / (len(toks | a) or 1) for a in advanced)
            return round(0.15 * best, 4) if fb.get(cid) != "advance" else 0.15
        ranked = rank(q, docs, boost=boost, limit=body.limit)
        if body.comp_ceiling is not None:
            for m in ranked:
                d = next(d for d in docs if d.id == m.id)
                if d.comp is not None and d.comp > body.comp_ceiling:
                    m.reasons.append(f"Expectation {d.comp:,} is above the {body.comp_ceiling:,} budget.")
        result = [{"candidate_id": m.id, "score": m.score, "breakdown": m.breakdown, "why": " ".join(m.reasons),
                   "reasons": m.reasons, "feedback": fb.get(m.id)} for m in ranked]
        audit.record(db, p.tenant_id, p.actor, "match.run", pid, {"candidates": len(docs)})
        bg.add_task(emit(p.tenant_id, "match.ready", {"posting_id": pid, "count": len(result),
                                                      "top": [r["candidate_id"] for r in result[:5]]}))
        return {"posting_id": pid, "engine": "bm25-local", "scored_fields": list(consent.MATCHABLE),
                "matches": result}

    @app.post("/v1/postings/{pid}/feedback")
    def feedback(pid: str, body: FeedbackIn, p: Principal = Depends(staff("feedback"))):
        get_posting(p.tenant_id, pid)
        if not db.one("SELECT 1 FROM applications WHERE tenant_id=? AND posting_id=? AND candidate_id=?",
                      p.tenant_id, pid, body.candidate_id):
            raise HTTPException(404, "Candidate did not apply to this posting")
        if body.action == "clear":
            db.x("DELETE FROM feedback WHERE posting_id=? AND candidate_id=? AND tenant_id=?", pid, body.candidate_id,
                 p.tenant_id)
        else:
            db.x("INSERT INTO feedback VALUES (?,?,?,?,?,?) ON CONFLICT(posting_id, candidate_id) DO UPDATE SET "
                 "action=excluded.action, actor=excluded.actor, created_at=excluded.created_at",
                 p.tenant_id, pid, body.candidate_id, body.action, p.actor, now())
            db.x("UPDATE applications SET status=? WHERE tenant_id=? AND posting_id=? AND candidate_id=?",
                 "advanced" if body.action == "advance" else "rejected", p.tenant_id, pid, body.candidate_id)
        audit.record(db, p.tenant_id, p.actor, f"feedback.{body.action}", pid, {"candidate": body.candidate_id})
        return {"posting_id": pid, "candidate_id": body.candidate_id, "action": body.action}

    # ---------- webhooks ----------
    @app.post("/v1/webhooks", status_code=201)
    def create_webhook(body: WebhookIn, p: Principal = Depends(staff("webhooks"))):
        u = urlparse(body.url)
        if u.scheme not in ("https", "http") or not u.netloc or (u.scheme == "http" and not settings.allow_http_webhooks):
            raise HTTPException(422, "Webhook URL must be https (set JOBBOT_ALLOW_HTTP_WEBHOOKS=1 for local dev)")
        wid, secret = new_id("wh"), "whsec_" + secrets.token_urlsafe(32)
        db.x("INSERT INTO webhooks VALUES (?,?,?,?,?,?)", wid, p.tenant_id, body.url, secret,
             dumps(sorted(set(body.events))), now())
        audit.record(db, p.tenant_id, p.actor, "webhook.created", wid, {"url": body.url, "events": body.events})
        return {"id": wid, "url": body.url, "events": sorted(set(body.events)), "secret": secret}

    @app.get("/v1/webhooks")
    def list_webhooks(p: Principal = Depends(staff("webhooks"))):
        return [{"id": r["id"], "url": r["url"], "events": json.loads(r["events"])}
                for r in db.q("SELECT * FROM webhooks WHERE tenant_id=?", p.tenant_id)]

    @app.delete("/v1/webhooks/{wid}", status_code=204)
    def delete_webhook(wid: str, p: Principal = Depends(staff("webhooks"))):
        if not db.x("DELETE FROM webhooks WHERE id=? AND tenant_id=?", wid, p.tenant_id):
            raise HTTPException(404, "Webhook not found")
        audit.record(db, p.tenant_id, p.actor, "webhook.deleted", wid)

    @app.get("/v1/webhooks/deliveries")
    def deliveries(p: Principal = Depends(staff("webhooks"))):
        return [dict(r) for r in db.q("SELECT id, webhook_id, event, status, attempts, created_at FROM deliveries "
                                      "WHERE tenant_id=? ORDER BY created_at DESC LIMIT 200", p.tenant_id)]

    # ---------- audit ----------
    @app.get("/v1/audit/export")
    def audit_export(since: int = 0, format: Literal["json", "jsonl"] = "json", p: Principal = Depends(staff("audit"))):
        rows = [{"seq": r["seq"], "actor": r["actor"], "action": r["action"], "target": r["target"],
                 "detail": json.loads(r["detail"]), "at": r["at"], "hash": r["hash"], "prev_hash": r["prev_hash"]}
                for r in db.q("SELECT * FROM audit_log WHERE tenant_id=? AND seq>? ORDER BY seq", p.tenant_id, since)]
        if format == "jsonl":
            return PlainTextResponse("".join(json.dumps(r) + "\n" for r in rows), media_type="application/x-ndjson")
        return {"tenant_id": p.tenant_id, "chain_valid": audit.verify_chain(db, p.tenant_id), "entries": rows}

    # ---------- widget config & retention ----------
    @app.get("/v1/widget/config")
    def get_widget(p: Principal = Depends(staff("widget:write"))):
        return public_widget_config(p)

    @app.put("/v1/widget/config")
    def put_widget(body: WidgetConfig, p: Principal = Depends(staff("widget:write"))):
        for lst in (body.ask_fields, body.default_consent):
            try:
                consent.clean_grant(lst)
            except ValueError as e:
                raise HTTPException(422, str(e))
        db.x("INSERT INTO widget_config VALUES (?,?) ON CONFLICT(tenant_id) DO UPDATE SET config=excluded.config",
             p.tenant_id, dumps(body.model_dump()))
        audit.record(db, p.tenant_id, p.actor, "widget.updated", p.tenant_id)
        return body.model_dump()

    @app.post("/v1/retention/run")
    def retention(p: Principal = Depends(staff("keys"))):
        days = db.one("SELECT retention_days FROM tenants WHERE id=?", p.tenant_id)["retention_days"]
        cutoff = now() - days * 86400
        n = db.x("UPDATE candidates SET profile='{}', deleted_at=? WHERE tenant_id=? AND deleted_at IS NULL "
                 "AND created_at<?", now(), p.tenant_id, cutoff)
        audit.record(db, p.tenant_id, p.actor, "retention.run", p.tenant_id, {"erased": n, "days": days})
        return {"erased": n, "retention_days": days}

    assert set(ROLES) == {"admin", "recruiter", "auditor", "widget"}
    return app


def get_app() -> FastAPI:  # uvicorn --factory jobbot_enterprise.main:get_app
    return create_app()
