<!-- SPDX-License-Identifier: Apache-2.0 -->
# Changelog

## 0.2.0 (2026-11-03)

- **Open source:** relicensed under Apache-2.0 (LICENSE, NOTICE, SPDX headers). Free for anyone to use.
- **Plugin system:** entry-point group `jobbot_enterprise.plugins` with `source`, `matcher`, `sink` and `export`
  kinds, fail-safe loading, `JOBBOT_PLUGINS` allow-list, and per-tenant enable/config.
- **Built-in plugins:** `greenhouse` and `lever` sources, `bm25` and `tfidf` matchers, `slack` and signed `webhook`
  sinks, `csv` and `jsonl` exports.
- **New endpoints:** `GET /v1/plugins`, `PUT /v1/plugins/{name}`, `POST /v1/plugins/{name}/import`,
  `GET /v1/postings/{id}/export`; `POST /v1/postings/{id}/match` accepts `engine`.
- **Packaging:** pip-installable (`pyproject.toml`), `jobbot-enterprise` CLI, wheel and sdist on GitHub Releases,
  Docker image on GHCR.

## 0.1.0

- Initial engine: tenants, API keys and roles, postings, consent-first intake, explainable matching, signed
  webhooks, hash-chained audit log, retention, careers widget, Docker.
