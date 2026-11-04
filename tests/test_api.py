# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Benjamin Stanley Frohman
from conftest import H, OP


def post_job(client, key, **kw):
    body = {"title": "Research Engineer, Alignment", "description": "Python, PyTorch, interpretability and evals "
            "for safe AI.", "remote": True, "status": "open", **kw}
    r = client.post("/v1/postings", json=body, headers=H(key))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def apply(client, wkey, pid, consent=("name", "email", "skills", "experience"), **profile):
    prof = {"name": "Ada", "email": "ada@example.com", "phone": "555-0100", "skills": "python pytorch evals",
            "experience": "built interpretability tooling", "causes": "ai safety", **profile}
    r = client.post("/v1/public/applications", json={"posting_id": pid, "profile": prof,
                                                      "consent_fields": list(consent)}, headers=H(wkey))
    assert r.status_code == 201, r.text
    return r.json()


def test_operator_and_key_auth(client, tenant):
    assert client.post("/v1/tenants", json={"name": "x"}).status_code == 401
    assert client.post("/v1/tenants", json={"name": "x"}, headers={"X-Operator-Token": "nope"}).status_code == 401
    t = tenant()
    assert client.get("/v1/postings").status_code == 401
    assert client.get("/v1/postings", headers=H("jbk_fake")).status_code == 401
    assert client.get("/v1/tenant", headers=H(t["admin_key"])).json()["role"] == "admin"
    # widget keys are publishable: they cannot reach staff endpoints
    assert client.get("/v1/postings", headers=H(t["widget_key"])).status_code == 403
    assert client.get("/v1/candidates", headers=H(t["widget_key"])).status_code == 403
    rec = client.post("/v1/api-keys", json={"name": "r", "role": "recruiter"}, headers=H(t["admin_key"])).json()
    assert client.get("/v1/audit/export", headers=H(rec["key"])).status_code == 403
    assert client.post("/v1/api-keys", json={"name": "x", "role": "admin"}, headers=H(rec["key"])).status_code == 403
    assert client.delete(f"/v1/api-keys/{rec['id']}", headers=H(t["admin_key"])).status_code == 204
    assert client.get("/v1/postings", headers=H(rec["key"])).status_code == 401  # revoked


def test_tenant_isolation(client, tenant):
    a, b = tenant("A"), tenant("B")
    pa = post_job(client, a["admin_key"])
    cand = apply(client, a["widget_key"], pa)["candidate_id"]
    kb = H(b["admin_key"])
    assert client.get("/v1/postings", headers=kb).json() == []
    assert client.get(f"/v1/postings/{pa}", headers=kb).status_code == 404
    assert client.patch(f"/v1/postings/{pa}", json={"title": "pwned"}, headers=kb).status_code == 404
    assert client.get(f"/v1/candidates/{cand}", headers=kb).status_code == 404
    assert client.get("/v1/candidates", headers=kb).json() == []
    assert client.post(f"/v1/postings/{pa}/match", json={}, headers=kb).status_code == 404
    assert client.post(f"/v1/postings/{pa}/feedback", json={"candidate_id": cand, "action": "reject"},
                       headers=kb).status_code == 404
    # B's widget cannot apply to A's posting or see it
    assert client.get("/v1/public/postings", headers=H(b["widget_key"])).json() == []
    r = client.post("/v1/public/applications", json={"posting_id": pa, "profile": {"name": "x"}},
                    headers=H(b["widget_key"]))
    assert r.status_code == 404
    assert all(e["target"] != pa for e in client.get("/v1/audit/export", headers=kb).json()["entries"])
    assert client.get(f"/v1/postings/{pa}", headers=H(a["admin_key"])).json()["title"].startswith("Research")


def test_consent_enforced_in_views_and_matching(client, tenant):
    t = tenant()
    k = H(t["admin_key"])
    pid = post_job(client, t["admin_key"])
    hidden = apply(client, t["widget_key"], pid, consent=("name",), skills="python pytorch evals interpretability")
    shown = apply(client, t["widget_key"], pid, consent=("name", "skills"), name="Grace", skills="python evals")
    v = client.get(f"/v1/candidates/{hidden['candidate_id']}", headers=k).json()
    assert v["profile"] == {"name": "Ada"} and v["consented_fields"] == ["name"]
    assert "email" not in str(client.get("/v1/candidates", headers=k).json())
    m = client.post(f"/v1/postings/{pid}/match", json={}, headers=k).json()
    scores = {x["candidate_id"]: x for x in m["matches"]}
    # undisclosed skills are never scored, so the candidate who shared less cannot rank on them
    assert scores[shown["candidate_id"]]["score"] > scores[hidden["candidate_id"]]["score"]
    assert scores[hidden["candidate_id"]]["breakdown"]["skills"] == 0
    assert "python" in scores[shown["candidate_id"]]["why"]
    # candidate widens consent, then revokes everything, then erases
    ct = {"X-Candidate-Token": hidden["candidate_token"]}
    assert client.put("/v1/candidate/me/consent", json={"fields": ["name", "skills"]}, headers=ct).status_code == 200
    assert client.get(f"/v1/candidates/{hidden['candidate_id']}", headers=k).json()["profile"]["skills"]
    assert client.put("/v1/candidate/me/consent", json={"fields": ["ssn"]}, headers=ct).status_code == 422
    client.put("/v1/candidate/me/consent", json={"fields": []}, headers=ct)
    assert client.get(f"/v1/candidates/{hidden['candidate_id']}", headers=k).json()["profile"] == {}
    assert client.get("/v1/candidate/me", headers=ct).json()["profile"]["phone"] == "555-0100"  # own data
    assert client.delete("/v1/candidate/me", headers=ct).status_code == 204
    assert client.get(f"/v1/candidates/{hidden['candidate_id']}", headers=k).status_code == 404
    assert client.get("/v1/candidate/me", headers=ct).status_code == 401
    acts = [e["action"] for e in client.get("/v1/audit/export", headers=k).json()["entries"]]
    assert "consent.updated" in acts and "candidate.erased" in acts


def test_feedback_and_candidate_matches(client, tenant):
    t = tenant()
    k = H(t["admin_key"])
    pid = post_job(client, t["admin_key"])
    other = post_job(client, t["admin_key"], title="Office Manager", description="Scheduling and facilities",
                     remote=False)
    c1 = apply(client, t["widget_key"], pid, consent=("skills",), skills="python evals")
    c2 = apply(client, t["widget_key"], pid, consent=("skills",), skills="python pytorch interpretability")
    c3 = apply(client, t["widget_key"], pid, consent=("skills",), skills="pytorch interpretability research")
    fb = lambda c, a: client.post(f"/v1/postings/{pid}/feedback", json={"candidate_id": c, "action": a}, headers=k)
    assert fb(c1["candidate_id"], "reject").status_code == 200
    assert fb(c2["candidate_id"], "advance").status_code == 200
    m = client.post(f"/v1/postings/{pid}/match", json={}, headers=k).json()["matches"]
    ids = [x["candidate_id"] for x in m]
    assert c1["candidate_id"] not in ids
    c3m = next(x for x in m if x["candidate_id"] == c3["candidate_id"])
    assert c3m["breakdown"]["feedback"] > 0 and "advanced" in c3m["why"]
    apps = client.get(f"/v1/postings/{pid}/applications", headers=k).json()
    assert {a["status"] for a in apps} == {"rejected", "advanced", "new"}
    mine = client.get("/v1/candidate/me/matches", headers={"X-Candidate-Token": c2["candidate_token"]}).json()
    assert mine[0]["posting_id"] == pid and mine[-1]["posting_id"] == other


def test_webhook_signing_and_delivery(client, tenant, hooks):
    from jobbot_enterprise.webhooks import sign, verify
    t = tenant()
    k = H(t["admin_key"])
    assert client.post("/v1/webhooks", json={"url": "http://insecure.example", "events": ["match.ready"]},
                       headers=k).status_code == 422
    wh = client.post("/v1/webhooks", json={"url": "https://hooks.acme.example/jobbot",
                                           "events": ["application.created", "match.ready"]}, headers=k).json()
    assert wh["secret"].startswith("whsec_")
    pid = post_job(client, t["admin_key"])
    apply(client, t["widget_key"], pid)
    client.post(f"/v1/postings/{pid}/match", json={}, headers=k)
    assert [r.headers["Jobbot-Event"] for r in hooks.calls] == ["application.created", "match.ready"]
    for r in hooks.calls:
        assert verify(wh["secret"], r.content, r.headers["Jobbot-Signature"])
        assert not verify("whsec_wrong", r.content, r.headers["Jobbot-Signature"])
        assert not verify(wh["secret"], r.content + b" ", r.headers["Jobbot-Signature"])
    body = b'{"x":1}'
    old = sign(wh["secret"], body, ts=1_000)
    assert not verify(wh["secret"], body, old, now_ts=1_000 + 301)  # replay window
    assert verify(wh["secret"], body, old, now_ts=1_000 + 10)
    # 5xx is retried, and deliveries are recorded
    hooks.status, hooks.calls[:] = 503, []
    apply(client, t["widget_key"], pid, name="Bo")
    assert len(hooks.calls) == 3
    d = client.get("/v1/webhooks/deliveries", headers=k).json()
    assert d[0]["status"] == 503 and d[0]["attempts"] == 3
    # other tenants' events never reach this hook
    hooks.calls[:] = []
    u = tenant("Other")
    apply(client, u["widget_key"], post_job(client, u["admin_key"]))
    assert hooks.calls == []
    assert "secret" not in str(client.get("/v1/webhooks", headers=k).json())


def test_audit_chain_widget_config_and_postings(client, tenant):
    t = tenant()
    k = H(t["admin_key"])
    pid = post_job(client, t["admin_key"], status="draft")
    assert client.get("/v1/public/postings", headers=H(t["widget_key"])).json() == []
    r = client.post("/v1/public/applications", json={"posting_id": pid, "profile": {}}, headers=H(t["widget_key"]))
    assert r.status_code == 409
    assert client.patch(f"/v1/postings/{pid}", json={"status": "open"}, headers=k).json()["status"] == "open"
    assert len(client.get("/v1/public/postings", headers=H(t["widget_key"])).json()) == 1
    cfg = {"company_name": "Acme", "accent_color": "#112233", "ask_fields": ["name", "email"],
           "default_consent": ["name"]}
    assert client.put("/v1/widget/config", json=cfg, headers=k).status_code == 200
    assert client.put("/v1/widget/config", json={**cfg, "ask_fields": ["dob"]}, headers=k).status_code == 422
    assert client.get("/v1/public/widget/config", headers=H(t["widget_key"])).json()["company_name"] == "Acme"
    assert client.delete(f"/v1/postings/{pid}", headers=k).status_code == 204
    exp = client.get("/v1/audit/export", headers=k).json()
    assert exp["chain_valid"] and [e["action"] for e in exp["entries"]][:2] == ["tenant.created", "api_key.created"]
    lines = client.get("/v1/audit/export", params={"format": "jsonl"}, headers=k).text.strip().splitlines()
    assert len(lines) == len(exp["entries"])
    # tampering breaks the chain
    client.app.state.db.x("UPDATE audit_log SET actor='someone-else' WHERE seq=(SELECT MIN(seq) FROM audit_log)")
    assert client.get("/v1/audit/export", headers=k).json()["chain_valid"] is False
    assert client.get("/widget.js").text.startswith("/* SPDX-License-Identifier: Apache-2.0")
    assert client.post("/v1/retention/run", headers=k).json()["erased"] == 0
    assert OP
