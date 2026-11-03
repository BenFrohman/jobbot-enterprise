# Jobbot Enterprise

[![CI](https://github.com/BenFrohman/jobbot-enterprise/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/BenFrohman/jobbot-enterprise/actions/workflows/ci.yml?query=branch%3Amain)

**Proprietary and confidential.** Copyright (c) 2026 Benjamin Stanley Frohman. All rights reserved. No use, copying or distribution without written permission. See [LICENSE](LICENSE).

This repository is **separate from** the open-source [Overemployed-Job Bot](https://github.com/BenFrohman/overemployed-job-bot) (Apache-2.0). Jobbot Enterprise may use that project as a library, but no enterprise code goes into the public repository. The matcher here follows the same local, explainable approach and was re-implemented for this codebase.

## What it is

Companies, including frontier AI labs, use Jobbot to offer their own hiring experience through an API. Jobbot provides the hiring engine (backend plus embeddable frontend), **deployed in the company's own infrastructure**. Candidate data stays in their hands, and the compute and operations complexity is handled by Jobbot's packaged engine.

- **Tenant-scoped API keys** with roles (`admin`, `recruiter`, `auditor`, publishable `widget`), stored as hashes and revocable
- **Postings** CRUD and **application intake** from an embeddable careers widget
- **Candidate consent**: the candidate decides which fields the company sees and scores, and can change that or erase themselves at any time
- **Explainable matching**: local BM25 skills fit, mission fit and preferences, with recruiter feedback (advance/reject) tuning later rankings. Each result has a score breakdown and a plain-language "why". It runs on CPU by default, and customers can plug in their own embedding model or GPUs.
- **Signed webhooks** (`application.created`, `match.ready`, `consent.updated`): HMAC-SHA256, replay window, retries, delivery log
- **Hash-chained audit log** with JSON/JSONL export and tamper detection; per-tenant retention
- **Self-hosting**: Dockerfile and docker-compose (non-root, read-only filesystem, no capabilities)

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/openapi.yaml](docs/openapi.yaml) (OpenAPI 3.1; every operation is marked `implemented` or `planned`).

## Implemented in v0.1 vs specified only

| Area | v0.1 | Planned |
|------|------|---------|
| Auth | API keys + roles, operator token | OIDC/SSO with group-to-role mapping |
| Data | SQLite on a customer volume | Postgres, envelope encryption with customer KMS keys |
| Matching | BM25 + mission + prefs + feedback, `Embedder` interface | Ready-made GPU / embedding adapters |
| Console | Full REST API | Admin web UI |
| Compliance | Consent, audit, retention, erasure | Bias-audit report endpoint |
| Ops | Docker / compose | Helm chart, managed control plane (anonymous metrics only) |

## Run it yourself

```bash
cp .env.example .env    # set JOBBOT_OPERATOR_TOKEN and JOBBOT_CORS_ORIGINS
docker compose up -d --build
curl -s -X POST localhost:8080/v1/tenants -H "X-Operator-Token: $JOBBOT_OPERATOR_TOKEN" \
  -H 'Content-Type: application/json' -d '{"name":"Acme Labs","region":"us"}'   # returns admin_key once
```

Embed the careers board with a publishable key (`role: widget`):

```html
<div id="jobbot"></div>
<script src="https://jobs.internal.example.com/widget.js" data-api="https://jobs.internal.example.com"
        data-key="jbp_..." data-target="#jobbot"></script>
```

Verify webhooks: `jobbot_enterprise.webhooks.verify(secret, raw_body, request.headers["Jobbot-Signature"])`.

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q tests
```

The tests cover tenant isolation, consent enforcement in views and matching, webhook signing, retries and replay protection, audit-chain tamper detection, and drift between the spec and the code.
