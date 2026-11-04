# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Benjamin Stanley Frohman
import csv
import hashlib
import hmac
import io
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import OP, H
from jobbot_enterprise import plugins
from jobbot_enterprise.main import Settings, create_app
from jobbot_enterprise.matching import Doc
from jobbot_enterprise.plugins import builtin
from test_api import apply, post_job

BUILTINS = {"greenhouse", "lever", "bm25", "tfidf", "slack", "webhook", "csv", "jsonl"}


def test_registry_loads_builtins_and_rejects_bad_plugins():
    bad = [SimpleNamespace(name="x y", kind="sink", deliver=lambda *a: None),
           SimpleNamespace(name="nokind", kind="weird"),
           SimpleNamespace(name="nomethod", kind="matcher")]
    good = SimpleNamespace(name="echo", kind="export", export=lambda rows: ("text/plain", b"ok"))
    reg = plugins.load(extra=bad + [good], discover=False)
    assert BUILTINS | {"echo"} == set(reg.plugins)
    assert set(reg.errors) == {"x y", "nokind", "nomethod"}
    with pytest.raises(plugins.PluginError):
        reg.add(builtin.CsvExport)  # duplicate name


def test_entry_point_failure_is_isolated(monkeypatch):
    class EP:
        name, value = "boom", "nowhere:Boom"

        def load(self):
            raise ImportError("missing dep")
    monkeypatch.setattr(plugins, "entry_points", lambda group: [EP()] if group == plugins.GROUP else [])
    reg = plugins.load()
    assert "boom" in reg.errors and BUILTINS <= set(reg.plugins)
    assert "boom" not in plugins.load(allow=set()).errors  # allow-list skips it entirely


def gh_lever_handler(req: httpx.Request) -> httpx.Response:
    if req.url.host == "boards-api.greenhouse.io":
        return httpx.Response(200, json={"jobs": [{"id": 1, "title": "ML Engineer", "content": "&lt;p&gt;PyTorch&lt;/p&gt;",
                                                   "location": {"name": "Remote"}, "departments": [{"name": "AI"}],
                                                   "absolute_url": "https://x"}]})
    if req.url.host == "api.lever.co":
        return httpx.Response(200, json=[{"id": "a", "text": "Designer", "descriptionPlain": "Figma",
                                          "categories": {"location": "NYC", "team": "Design"}, "hostedUrl": "u"}])
    return httpx.Response(404)


def test_sources_parse():
    http = httpx.Client(transport=httpx.MockTransport(gh_lever_handler))
    g = builtin.GreenhouseSource().fetch({"board": "acme"}, http)
    assert g[0]["external_id"] == "greenhouse:1" and g[0]["description"] == "PyTorch" and g[0]["remote"]
    lv = builtin.LeverSource().fetch({"site": "acme"}, http)
    assert lv[0]["title"] == "Designer" and lv[0]["team"] == "Design" and not lv[0]["remote"]
    with pytest.raises(ValueError):
        builtin.GreenhouseSource().fetch({"board": "../etc"}, http)


def test_matchers_rank_relevant_first():
    docs = [Doc("a", "python pytorch interpretability evals"), Doc("b", "accounting excel payroll")]
    q = {"skills": "python pytorch", "experience": "evals"}
    for m in (builtin.BM25Matcher(), builtin.TfidfMatcher()):
        r = m.rank(q, docs, limit=5)
        assert [x.id for x in r] == ["a", "b"] and r[0].score > r[1].score and r[0].reasons
    assert builtin.TfidfMatcher().rank(q, [], limit=5) == []


def test_sinks_and_exports():
    seen = []
    http = httpx.Client(transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200)))
    builtin.SlackSink().deliver("match.ready", {"data": {"posting_id": "p1", "count": 2}},
                                {"url": "https://hooks.slack.com/services/T/B/x"}, http)
    assert "p1" in json.loads(seen[-1].content)["text"]
    with pytest.raises(ValueError):
        builtin.SlackSink().deliver("e", {}, {"url": "https://evil.example.com/"}, http)
    builtin.WebhookSink().deliver("e", {"a": 1}, {"url": "https://h.example.com/", "secret": "s"}, http)
    t, v1 = (x.split("=", 1)[1] for x in seen[-1].headers["jobbot-signature"].split(","))
    assert v1 == hmac.new(b"s", f"{t}.".encode() + seen[-1].content, hashlib.sha256).hexdigest()
    with pytest.raises(ValueError):
        builtin.WebhookSink().deliver("e", {}, {"url": "http://h.example.com/"}, http)
    media, body = builtin.CsvExport().export([{"candidate_id": "c1", "name": 'Ada, "A"'}])
    assert media.startswith("text/csv") and list(csv.DictReader(io.StringIO(body.decode())))[0]["name"] == 'Ada, "A"'
    assert json.loads(builtin.JsonlExport().export([{"x": 1}])[1]) == {"x": 1}


@pytest.fixture
def pclient(tmp_path):
    sent = []

    def handler(req):
        sent.append(req)
        return gh_lever_handler(req) if req.url.host != "hooks.slack.com" else httpx.Response(200)
    app = create_app(Settings(db_path=str(tmp_path / "p.db"), operator_token=OP, plugins="0"),
                     http=httpx.Client(transport=httpx.MockTransport(handler)), webhook_backoff=0)
    with TestClient(app) as c:
        c.sent = sent
        yield c


def test_plugin_api_end_to_end(pclient):
    c = pclient
    t = c.post("/v1/tenants", json={"name": "Lab"}, headers={"X-Operator-Token": OP}).json()
    a = H(t["admin_key"])
    lst = c.get("/v1/plugins", headers=a).json()
    state = {p["name"]: p["enabled"] for p in lst["plugins"]}
    assert set(state) == BUILTINS and state["bm25"] and not state["slack"]
    # source import: needs config, then dedupes
    assert c.post("/v1/plugins/greenhouse/import", headers=a).status_code == 422
    assert c.put("/v1/plugins/greenhouse", json={"enabled": True, "config": {"board": "acme"}}, headers=a).status_code == 200
    assert c.post("/v1/plugins/greenhouse/import", headers=a).json()["created"] == 1
    assert c.post("/v1/plugins/greenhouse/import", headers=a).json() == {"plugin": "greenhouse", "created": 0,
                                                                         "skipped": 1, "status": "draft"}
    assert c.post("/v1/plugins/nope/import", headers=a).status_code == 404
    # recruiters cannot configure plugins
    rec = c.post("/v1/api-keys", json={"name": "r", "role": "recruiter"}, headers=a).json()["key"]
    assert c.put("/v1/plugins/slack", json={"enabled": True}, headers=H(rec)).status_code == 403
    # sink fires on events; matcher choice; disabled matcher refused
    c.put("/v1/plugins/slack", json={"enabled": True, "config": {"url": "https://hooks.slack.com/services/T/B/x",
                                                                   "events": ["match.ready"]}}, headers=a)
    w = c.post("/v1/api-keys", json={"name": "w", "role": "widget"}, headers=a).json()["key"]
    pid = post_job(c, t["admin_key"])
    apply(c, w, pid)
    m = c.post(f"/v1/postings/{pid}/match", json={"engine": "tfidf"}, headers=a).json()
    assert m["engine"] == "tfidf" and len(m["matches"]) == 1
    assert any(r.url.host == "hooks.slack.com" for r in c.sent)
    c.put("/v1/plugins/tfidf", json={"enabled": False}, headers=a)
    assert c.post(f"/v1/postings/{pid}/match", json={"engine": "tfidf"}, headers=a).status_code == 409
    # export only consented fields
    r = c.get(f"/v1/postings/{pid}/export?plugin=csv", headers=a)
    row = list(csv.DictReader(io.StringIO(r.text)))[0]
    assert row["email"] == "ada@example.com" and row["phone"] == ""  # phone was not consented
    assert c.get(f"/v1/postings/{pid}/export?plugin=bm25", headers=a).status_code == 404
    assert c.get(f"/v1/postings/{pid}/export", headers=H(w)).status_code == 403
