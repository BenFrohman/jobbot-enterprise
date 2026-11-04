# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Benjamin Stanley Frohman
import httpx
import pytest
from fastapi.testclient import TestClient

from jobbot_enterprise.main import Settings, create_app

OP = "operator-test-token"


class Hooks:
    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return httpx.Response(self.status)


@pytest.fixture
def hooks():
    return Hooks()


@pytest.fixture
def client(tmp_path, hooks):
    app = create_app(Settings(db_path=str(tmp_path / "t.db"), operator_token=OP),
                     http=httpx.Client(transport=httpx.MockTransport(hooks.handler)), webhook_backoff=0)
    with TestClient(app) as c:
        yield c


def H(key):
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def tenant(client):
    def make(name="Acme Labs"):
        r = client.post("/v1/tenants", json={"name": name}, headers={"X-Operator-Token": OP})
        assert r.status_code == 201
        t = r.json()
        w = client.post("/v1/api-keys", json={"name": "site", "role": "widget"}, headers=H(t["admin_key"])).json()
        t["widget_key"] = w["key"]
        return t
    return make
