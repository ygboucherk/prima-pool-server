"""Tests for the static GUI: page serving and the account dashboard endpoint."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from prima_pool_server.app import create_app
from prima_pool_server.config import ModelDef, Settings
from prima_pool_server.store import Store


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        models={
            "demo-model": ModelDef(
                slug="demo-model", gguf_sha256="a" * 64, required_memory_mb=4096
            )
        },
        assignable_grace_s=0,
        heartbeat_timeout_s=30,
    )


@pytest.fixture()
def store() -> Store:
    return Store(path=None)


@pytest.fixture()
def client(settings: Settings, store: Store):
    app = create_app(settings=settings, store=store)
    with TestClient(app) as c:
        yield c


def _register_and_login(client: TestClient, username="alice"):
    client.post("/v1/accounts/register", json={"username": username, "password": "hunter2hunter2"})
    return client.post("/v1/accounts/login", json={"username": username, "password": "hunter2hunter2"}).json()


def test_gui_page_served(client: TestClient):
    r = client.get("/ui")
    assert r.status_code == 200
    assert "prima-pool" in r.text
    assert "login" in r.text.lower()


def test_gui_static_assets_served(client: TestClient):
    for path in ("/ui/static/dashboard.css", "/ui/static/dashboard.js"):
        r = client.get(path)
        assert r.status_code == 200, path


def test_gui_has_account_tab_and_change_password_form(client: TestClient):
    r = client.get("/ui")
    assert r.status_code == 200
    # Account tab button in the sidebar.
    assert 'data-tab="account"' in r.text
    assert "Account" in r.text
    # Change-password form fields.
    assert 'id="change-password-form"' in r.text
    assert 'id="cp-current"' in r.text
    assert 'id="cp-new"' in r.text
    assert 'id="cp-confirm"' in r.text


def test_gui_has_balance_views(client: TestClient):
    r = client.get("/ui")
    assert r.status_code == 200
    # Account tab shows the account's balance + balance-history table.
    assert 'id="account-balance"' in r.text
    assert 'id="account-balance-events-body"' in r.text
    # Admin tab has the balances table.
    assert 'id="admin-balances-body"' in r.text


def test_dashboard_requires_session(client: TestClient):
    r = client.get("/v1/accounts/acc_1/dashboard")
    assert r.status_code == 401


def test_dashboard_own_account(client: TestClient):
    sess = _register_and_login(client)
    account_id = sess["session_token"].split(".")[0].replace("sess_", "")
    r = client.get(
        f"/v1/accounts/{account_id}/dashboard",
        headers={"Authorization": f"Bearer {sess['session_token']}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["account_id"] == account_id
    assert body["username"] == "alice"
    assert body["workers"] == []
    assert body["keys"] == []


def test_dashboard_cannot_access_other_account(client: TestClient):
    sess = _register_and_login(client)
    account_id = sess["session_token"].split(".")[0].replace("sess_", "")
    # Bob cannot view Alice's dashboard.
    _register_and_login(client, username="bob")
    bob_sess = client.post("/v1/accounts/login", json={"username": "bob", "password": "hunter2hunter2"}).json()
    r = client.get(
        f"/v1/accounts/{account_id}/dashboard",
        headers={"Authorization": f"Bearer {bob_sess['session_token']}"},
    )
    assert r.status_code == 403


def test_dashboard_includes_workers_and_keys(client: TestClient):
    sess = _register_and_login(client)
    account_id = sess["session_token"].split(".")[0].replace("sess_", "")
    # Create a worker key + register a worker.
    wk = client.post(
        f"/v1/accounts/{account_id}/keys",
        headers={"Authorization": f"Bearer {sess['session_token']}"},
        json={"name": "worker", "scope": "worker"},
    ).json()["api_key"]
    client.post(
        "/v1/workers/register",
        headers={"Authorization": f"Bearer {wk}"},
        json={
            "model": "demo-model",
            "gguf_sha256": "a" * 64,
            "memory_allocated_mb": 4096,
            "wg_pubkey": "pubkey1",
            "endpoint": {"host": "203.0.113.10", "port": 51820, "behind_nat": False, "nat_type": "none"},
        },
    )
    r = client.get(
        f"/v1/accounts/{account_id}/dashboard",
        headers={"Authorization": f"Bearer {sess['session_token']}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["keys"]) == 1
    assert len(body["workers"]) == 1
    assert body["workers"][0]["model"] == "demo-model"
    assert body["workers"][0]["memory_mb"] == 4096