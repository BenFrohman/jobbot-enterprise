# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Benjamin Stanley Frohman
"""Built-in plugins: Greenhouse/Lever sources, BM25/TF-IDF matchers, Slack/webhook sinks, CSV/JSONL exports."""
from __future__ import annotations

import csv
import hashlib
import hmac
import html
import io
import json
import math
import re
import time
from collections import Counter
from urllib.parse import urlparse

from ..matching import Doc, Result, rank, tokenize

_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


def _strip(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(s or ""))).strip()


def _slug(config: dict, key: str) -> str:
    v = str(config.get(key, ""))
    if not _SLUG.match(v):
        raise ValueError(f"config.{key} must be a board slug (letters, digits, . _ -)")
    return v


def _https(url: str) -> str:
    u = urlparse(url or "")
    if u.scheme != "https" or not u.netloc:
        raise ValueError("config.url must be an https URL")
    return url


# ---------------- sources ----------------
class GreenhouseSource:
    name, kind, default_enabled = "greenhouse", "source", True
    description = "Import open roles from a public Greenhouse job board (boards-api.greenhouse.io)."
    config_schema = {"board": "Greenhouse board token, e.g. 'acme'"}

    def fetch(self, config: dict, http) -> list[dict]:
        board = _slug(config, "board")
        r = http.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs", params={"content": "true"},
                     timeout=20)
        r.raise_for_status()
        out = []
        for j in r.json().get("jobs", []):
            loc = (j.get("location") or {}).get("name", "")
            out.append({"external_id": f"greenhouse:{j['id']}", "title": j.get("title", "")[:300],
                        "description": _strip(j.get("content", ""))[:50_000], "location": loc[:200],
                        "remote": "remote" in loc.lower(), "team": ((j.get("departments") or [{}])[0] or {})
                        .get("name", "")[:200], "url": j.get("absolute_url", "")})
        return out


class LeverSource:
    name, kind, default_enabled = "lever", "source", True
    description = "Import open roles from a public Lever postings site (api.lever.co)."
    config_schema = {"site": "Lever site name, e.g. 'acme'"}

    def fetch(self, config: dict, http) -> list[dict]:
        site = _slug(config, "site")
        r = http.get(f"https://api.lever.co/v0/postings/{site}", params={"mode": "json"}, timeout=20)
        r.raise_for_status()
        out = []
        for j in r.json():
            c = j.get("categories") or {}
            loc = c.get("location") or ""
            out.append({"external_id": f"lever:{j['id']}", "title": j.get("text", "")[:300],
                        "description": (j.get("descriptionPlain") or _strip(j.get("description", "")))[:50_000],
                        "location": loc[:200], "remote": "remote" in (loc + j.get("workplaceType", "")).lower(),
                        "team": (c.get("team") or "")[:200], "url": j.get("hostedUrl", "")})
        return out


# ---------------- matchers ----------------
class BM25Matcher:
    name, kind, default_enabled = "bm25", "matcher", True
    description = "Local BM25 skills + mission + preference scoring with plain-language reasons (default)."

    def rank(self, query, docs, *, boost=None, limit=50):
        return rank(query, docs, boost=boost, limit=limit)


class TfidfMatcher:
    name, kind, default_enabled = "tfidf", "matcher", True
    description = "Local TF-IDF cosine similarity between the posting and each candidate's consented fields."

    def rank(self, query, docs, *, boost=None, limit=50):
        if not docs:
            return []
        toks = [tokenize(d.text) for d in docs]
        q = tokenize(" ".join(str(query.get(k, "")) for k in ("skills", "experience")))
        n = len(docs)
        df = Counter(t for ts in toks for t in set(ts))
        idf = {t: math.log((1 + n) / (1 + c)) + 1 for t, c in df.items()}

        def vec(ts):
            tf = Counter(ts)
            v = {t: c * idf.get(t, math.log(1 + n) + 1) for t, c in tf.items()}
            norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
            return {t: x / norm for t, x in v.items()}
        qv = vec(q)
        out = []
        for d, ts in zip(docs, toks):
            dv = vec(ts)
            sim = sum(qv[t] * dv.get(t, 0.0) for t in qv)
            fb = boost(d.id) if boost else 0.0
            hits = [t for t in dict.fromkeys(q) if t in dv][:5]
            reasons = (["Shared terms: " + ", ".join(hits) + "."] if hits else []) + \
                      (["Similar to people your team advanced before."] if fb > 0 else [])
            out.append(Result(d.id, round(100 * min(sim + fb, 1.0), 1),
                              {"cosine": round(sim, 3), "feedback": round(fb, 3)},
                              reasons or ["Weak overlap; shown for completeness."]))
        out.sort(key=lambda r: (-r.score, r.id))
        return out[:limit]


# ---------------- sinks ----------------
class SlackSink:
    name, kind, default_enabled = "slack", "sink", False
    description = "Post a short notice to a Slack incoming-webhook URL. Sends IDs only, never profile data."
    config_schema = {"url": "https://hooks.slack.com/services/…", "events": "optional list of event names"}

    def deliver(self, event, payload, config, http):
        url = _https(config.get("url", ""))
        if urlparse(url).hostname != "hooks.slack.com":
            raise ValueError("config.url must be a hooks.slack.com incoming-webhook URL")
        d = payload.get("data", {})
        text = f"Jobbot: {event}" + "".join(f" · {k}={d[k]}" for k in ("posting_id", "application_id", "count")
                                            if k in d)
        http.post(url, json={"text": text}, timeout=10).raise_for_status()


class WebhookSink:
    name, kind, default_enabled = "webhook", "sink", False
    description = "POST the event JSON to any https endpoint, HMAC-SHA256 signed with config.secret."
    config_schema = {"url": "https endpoint", "secret": "shared secret", "events": "optional list"}

    def deliver(self, event, payload, config, http):
        url = _https(config.get("url", ""))
        body = json.dumps(payload, separators=(",", ":")).encode()
        t = str(int(time.time()))
        sig = hmac.new(str(config.get("secret", "")).encode(), t.encode() + b"." + body, hashlib.sha256).hexdigest()
        http.post(url, content=body, timeout=10, headers={"Content-Type": "application/json", "Jobbot-Event": event,
                                                          "Jobbot-Signature": f"t={t},v1={sig}"}).raise_for_status()


# ---------------- exports ----------------
COLUMNS = ["application_id", "candidate_id", "status", "applied_at", "name", "email", "phone", "location",
           "links", "skills", "experience", "causes", "comp_expectation"]


class CsvExport:
    name, kind, default_enabled = "csv", "export", True
    description = "Export a posting's applications (consented fields only) as RFC 4180 CSV for any ATS import."

    def export(self, rows):
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore", lineterminator="\r\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in COLUMNS})
        return "text/csv; charset=utf-8", buf.getvalue().encode()


class JsonlExport:
    name, kind, default_enabled = "jsonl", "export", True
    description = "Export a posting's applications (consented fields only) as JSON Lines."

    def export(self, rows):
        return "application/x-ndjson", "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows).encode()


ALL = [GreenhouseSource, LeverSource, BM25Matcher, TfidfMatcher, SlackSink, WebhookSink, CsvExport, JsonlExport]
__all__ = ["ALL", "Doc"]
