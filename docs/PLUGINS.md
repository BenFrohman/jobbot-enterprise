<!-- SPDX-License-Identifier: Apache-2.0 -->
# Plugins

Jobbot Enterprise is extended with plugins. A plugin is a Python object with three attributes, `name`
(letters, digits, `-`, `_`), `kind` and `description`, plus the method its kind requires.

| kind | method | used by |
|------|--------|---------|
| `source` | `fetch(config: dict, http: httpx.Client) -> list[dict]` | `POST /v1/plugins/{name}/import` |
| `matcher` | `rank(query: dict, docs: list[Doc], *, boost=None, limit=50) -> list[Result]` | `POST /v1/postings/{id}/match` with `{"engine": name}` |
| `sink` | `deliver(event: str, payload: dict, config: dict, http: httpx.Client) -> None` | every event (`application.created`, `match.ready`, `consent.updated`) |
| `export` | `export(rows: list[dict]) -> tuple[media_type, bytes]` | `GET /v1/postings/{id}/export?plugin=name` |

Optional attributes: `default_enabled` (bool, default `False`) and `config_schema` (a dict documenting config keys).

Source results are dicts with `external_id` (required, used for de-duplication), `title`, `description`,
`location`, `remote`, `team`. Imported roles are always created as `draft` postings so a human publishes them.
`Doc` and `Result` live in `jobbot_enterprise.matching`.

## Built-in plugins

| name | kind | default | config |
|------|------|---------|--------|
| `greenhouse` | source | on | `board`: Greenhouse board token |
| `lever` | source | on | `site`: Lever site name |
| `bm25` | matcher | on | – (the default engine: skills + mission + preferences, with reasons) |
| `tfidf` | matcher | on | – (TF-IDF cosine) |
| `slack` | sink | off | `url`: `https://hooks.slack.com/...`, optional `events` list. Sends IDs only. |
| `webhook` | sink | off | `url` (https), `secret`, optional `events`. Header `Jobbot-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, "<t>." + body)>` |
| `csv` | export | on | – RFC 4180 CSV, consented fields only |
| `jsonl` | export | on | – JSON Lines, consented fields only |

## Writing a plugin

```python
# my_pkg/plugins.py
class Upper:
    name, kind, description = "upper-csv", "export", "CSV with upper-case names"
    def export(self, rows):
        return "text/plain", "\n".join(r.get("name", "").upper() for r in rows).encode()
```

```toml
# my_pkg/pyproject.toml
[project.entry-points."jobbot_enterprise.plugins"]
upper-csv = "my_pkg.plugins:Upper"
```

`pip install` your package next to Jobbot Enterprise and restart. `jobbot-enterprise --list-plugins` shows what loaded.

## Safety

* Loading is fail-safe: a plugin that fails to import, has a bad name, duplicate name, unknown kind or missing method is
  skipped and reported in `GET /v1/plugins` under `load_errors`; it never stops the service.
* `JOBBOT_PLUGINS` controls discovery: `1` (default) all installed entry points, `0` built-ins only, or a comma list
  of allowed entry-point names.
* Every tenant enables or disables each plugin and stores its config with `PUT /v1/plugins/{name}` (admin keys only).
  A disabled plugin returns `409`. Config is limited to 10 KB and only its key names go into the audit log.
* Exports and matchers only ever receive the fields a candidate consented to.
* Sinks run after the response; a failing sink is recorded in the audit log (`plugin.sink`) and never breaks a request.
* Built-in sinks only post to `https` URLs (Slack sink: only `hooks.slack.com`). Plugins run in-process with the
  server's permissions, so install only plugins you trust.
