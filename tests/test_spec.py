# Copyright (c) 2026 Benjamin Stanley Frohman. All rights reserved.
# Proprietary and confidential. No use, copying or distribution without written permission.
"""The spec and the service must not drift: every implemented route is in docs/openapi.yaml."""
from pathlib import Path

import yaml

from jobbot_enterprise.main import Settings, create_app

SPEC = yaml.safe_load((Path(__file__).parent.parent / "docs" / "openapi.yaml").read_text())


def _norm(path: str) -> str:
    import re
    return re.sub(r"\{[^}]+\}", "{}", path)


def test_spec_is_3_1_and_covers_implementation():
    assert SPEC["openapi"] == "3.1.0"
    spec_ops = {(_norm(p), m.upper()) for p, ops in SPEC["paths"].items() for m in ops if m != "parameters"}
    app = create_app(Settings(db_path=":memory:", operator_token="x"))
    impl = {(_norm(r.path), m) for r in app.routes if r.path.startswith(("/v1", "/healthz"))
            for m in getattr(r, "methods", set()) - {"HEAD"}}
    assert impl - spec_ops == set()
    implemented = {(_norm(p), m.upper()) for p, ops in SPEC["paths"].items() for m, o in ops.items()
                   if m != "parameters" and o.get("x-jobbot-status") == "implemented"}
    assert implemented == impl  # nothing marked implemented is missing
    assert set(SPEC["webhooks"]) == {"application.created", "match.ready", "consent.updated"}
