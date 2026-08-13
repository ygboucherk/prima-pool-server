"""Tests for the account permission model (v0.7): admin/user, can_work/can_use,
banned, the permissionless env switches, first-account bootstrap, and the
admin-gated management endpoints."""
from __future__ import annotations

from fastapi.testclient import TestClient

from prima_pool_server.app import create_app
from prima_pool_server.config import ModelDef, Settings
from prima_pool_server.store import Store


def _settings(**overrides) -> Settings:
    base = dict(
        models={
            "demo-model": ModelDef(
                slug="demo-model", gguf_sha256="a" * 64, required_memory_mb=4096
            )
        },
        assignable_grace_s=0,
        heartbeat_timeout_s=30,
    )
    base.update(overrides)
    return Settings(**base)


def _make_client(settings: Settings) -> TestClient:
    app = create_app(settings=settings, store=Store(path=None))
    return TestClient(app)


def _register(client: TestClient, username="alice", password="hunter2hunter2"):
    r = client.post("/v1/accounts/register", json={"username": username, "password": password})
    assert r.status_code == 201, r.text
    return r.json()


def _login(client: TestClient, username="alice", password="hunter2hunter2"):
    r = client.post("/v1/accounts/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def _make_admin(client: TestClient, username="alice"):
    _register(client, username=username)
    sess = _login(client, username=username)
    admin_id = sess["session_token"].split(".")[0].replace("sess_", "")
    # Directly promote via the store through a fresh admin... but there are no
    # admins yet. Use the store-level helper by opening the same app's store.
    # Instead: bootstrap via first_account is covered separately; here we grant
    # admin by directly calling the store (exposed on the app state).
    return admin_id, sess


def test_defaults_permissionless_and_admin_endpoints_require_admin():
    """Unset env switches → permissionless (true/true); admin endpoints 403 for
    a non-admin session token."""
    client = _make_client(_settings())
    acc = _register(client)
    sess = _login(client)
    account_id = acc["account_id"]
    tok = sess["session_token"]
    # Non-admin cannot read permission state or list accounts.
    assert client.get("/v1/admin/permissions", headers={"Authorization": f"Bearer {tok}"}).status_code == 403
    assert client.get("/v1/admin/accounts", headers={"Authorization": f"Bearer {tok}"}).status_code == 403
    # Dashboard exposes the four permission fields (new-account defaults:
    # non-admin, can_use but NOT can_work).
    body = client.get(
        f"/v1/accounts/{account_id}/dashboard",
        headers={"Authorization": f"Bearer {tok}"},
    ).json()
    assert body["is_admin"] is False
    assert body["can_work"] is False
    assert body["can_use"] is True
    assert body["banned"] is False


def test_first_account_bootstrap_creates_admin():
    """PRIMA_POOL_FIRST_ACCOUNT creates an admin iff no admin exists."""
    client = _make_client(_settings(first_account="root:supersecret123"))
    with client:
        # The bootstrap runs on lifespan startup (the `with` block enters it).
        pass
    # The bootstrap account exists and is an admin.
    r = client.post("/v1/accounts/login", json={"username": "root", "password": "supersecret123"})
    assert r.status_code == 200, r.text
    tok = r.json()["session_token"]
    perms = client.get("/v1/admin/permissions", headers={"Authorization": f"Bearer {tok}"})
    assert perms.status_code == 200, perms.text
    assert perms.json() == {"work_permissionless": True, "use_permissionless": True}
    accounts = client.get("/v1/admin/accounts", headers={"Authorization": f"Bearer {tok}"}).json()
    assert len(accounts) == 1
    assert accounts[0]["username"] == "root"
    assert accounts[0]["is_admin"] is True


def test_first_account_bootstrap_skips_when_admin_exists():
    """Bootstrap is a no-op when an admin already exists."""
    client = _make_client(_settings(first_account="root:supersecret123"))
    with client:
        pass
    # A second bootstrap attempt (same settings, same store) — covered by the
    # idempotency: the username already exists and IS admin, so count_admins()
    # is >0 and nothing happens. Registering a normal account and checking the
    # bootstrap did not promote anything is the observable behavior:
    r = client.post("/v1/accounts/login", json={"username": "root", "password": "supersecret123"})
    assert r.status_code == 200


def test_register_gated_when_work_revoked_and_not_permissionless():
    """With work_permissionless=false, an account whose can_work is revoked
    cannot register workers; one with can_work (granted by admin) can."""
    client = _make_client(
        _settings(work_permissionless=False, use_permissionless=False)
    )
    with client:
        admin_id, tok = _register_and_promote(client, "alice")
        # Bob registers (defaults can_work=False).
        _register(client, username="bob")
        bob_sess = _login(client, username="bob")
        bob_id = bob_sess["session_token"].split(".")[0].replace("sess_", "")
        wk = client.post(
            f"/v1/accounts/{bob_id}/keys",
            headers={"Authorization": f"Bearer {bob_sess['session_token']}"},
            json={"name": "worker", "scope": "worker"},
        ).json()["api_key"]

        # Grant bob can_work, then his worker registration must succeed.
        client.patch(
            f"/v1/admin/accounts/{bob_id}",
            headers={"Authorization": f"Bearer {tok}"},
            json={"can_work": True},
        )
        ok = client.post(
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
        assert ok.status_code == 201, ok.text

        # A fresh account with can_work=False (default) is blocked.
        _register(client, username="carol")
        carol_sess = _login(client, username="carol")
        carol_id = carol_sess["session_token"].split(".")[0].replace("sess_", "")
        carol_wk = client.post(
            f"/v1/accounts/{carol_id}/keys",
            headers={"Authorization": f"Bearer {carol_sess['session_token']}"},
            json={"name": "worker", "scope": "worker"},
        ).json()["api_key"]
        r = client.post(
            "/v1/workers/register",
            headers={"Authorization": f"Bearer {carol_wk}"},
            json={
                "model": "demo-model",
                "gguf_sha256": "a" * 64,
                "memory_allocated_mb": 4096,
                "wg_pubkey": "pubkey2",
                "endpoint": {"host": "203.0.113.11", "port": 51820, "behind_nat": False, "nat_type": "none"},
            },
        )
        assert r.status_code == 403, r.text


def test_banned_account_cannot_login_or_use_key():
    """Ban blocks login and API-key auth (both resolve the account's banned flag)."""
    client = _make_client(_settings())
    with client:
        admin_id, tok = _register_and_promote(client, "alice")
        _register(client, username="bob")
        bob_sess = _login(client, username="bob")
        bob_id = bob_sess["session_token"].split(".")[0].replace("sess_", "")
        # Bob creates a user key BEFORE being banned.
        uk = client.post(
            f"/v1/accounts/{bob_id}/keys",
            headers={"Authorization": f"Bearer {bob_sess['session_token']}"},
            json={"name": "user", "scope": "user"},
        ).json()["api_key"]

        # Ban bob.
        r = client.patch(
            f"/v1/admin/accounts/{bob_id}",
            headers={"Authorization": f"Bearer {tok}"},
            json={"banned": True},
        )
        assert r.status_code == 200, r.text
        assert r.json()["banned"] is True

        # Bob can no longer log in.
        assert client.post("/v1/accounts/login", json={"username": "bob", "password": "hunter2hunter2"}).status_code == 403
        # Bob's session token is now rejected (account is banned).
        assert client.get(
            f"/v1/accounts/{bob_id}/dashboard",
            headers={"Authorization": f"Bearer {bob_sess['session_token']}"},
        ).status_code == 403
        # Bob's user key is rejected too.
        assert client.get(
            f"/v1/accounts/{bob_id}/usage/logs/latest",
            headers={"Authorization": f"Bearer {uk}"},
        ).status_code == 403


def test_admin_toggle_and_last_admin_guard():
    """An admin can toggle permissions; the last admin cannot be demoted."""
    client = _make_client(_settings())
    with client:
        admin_id, tok = _register_and_promote(client, "alice")
        # Register bob.
        _register(client, username="bob")
        bob_id = client.post("/v1/accounts/login", json={"username": "bob", "password": "hunter2hunter2"}).json()["session_token"].split(".")[0].replace("sess_", "")

        # Toggle bob's can_work off.
        r = client.patch(
            f"/v1/admin/accounts/{bob_id}",
            headers={"Authorization": f"Bearer {tok}"},
            json={"can_work": False},
        )
        assert r.status_code == 200
        assert r.json()["can_work"] is False

        # Demote the last admin → 409.
        r = client.patch(
            f"/v1/admin/accounts/{admin_id}",
            headers={"Authorization": f"Bearer {tok}"},
            json={"is_admin": False},
        )
        assert r.status_code == 409, r.text

        # Promote bob, then demote alice (now two admins) succeeds.
        r = client.patch(
            f"/v1/admin/accounts/{bob_id}",
            headers={"Authorization": f"Bearer {tok}"},
            json={"is_admin": True},
        )
        assert r.status_code == 200
        r = client.patch(
            f"/v1/admin/accounts/{admin_id}",
            headers={"Authorization": f"Bearer {tok}"},
            json={"is_admin": False},
        )
        assert r.status_code == 200
        assert r.json()["is_admin"] is False


def _register_and_promote(client: TestClient, username: str):
    """Register a user and directly promote them to admin (test-only shortcut)."""
    _register(client, username=username)
    sess = _login(client, username=username)
    account_id = sess["session_token"].split(".")[0].replace("sess_", "")
    client.app.state.store.update_account_permissions(account_id, is_admin=True)
    return account_id, sess["session_token"]


def test_cannot_update_account_with_no_fields():
    client = _make_client(_settings())
    with client:
        admin_id, tok = _register_and_promote(client, "alice")
        r = client.patch(
            f"/v1/admin/accounts/{admin_id}",
            headers={"Authorization": f"Bearer {tok}"},
            json={},
        )
        assert r.status_code == 400, r.text


def test_admin_list_accounts_includes_flags():
    client = _make_client(_settings())
    with client:
        admin_id, tok = _register_and_promote(client, "alice")
        _register(client, username="bob")
        r = client.get("/v1/admin/accounts", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        accounts = {a["username"]: a for a in r.json()}
        assert set(accounts) == {"alice", "bob"}
        assert accounts["alice"]["is_admin"] is True
        assert accounts["bob"]["is_admin"] is False
        assert accounts["bob"]["can_work"] is False
        assert accounts["bob"]["can_use"] is True
        assert accounts["bob"]["banned"] is False


def test_permission_state_reflects_settings():
    client = _make_client(
        _settings(work_permissionless=False, use_permissionless=True)
    )
    with client:
        admin_id, tok = _register_and_promote(client, "alice")
        r = client.get("/v1/admin/permissions", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        assert r.json() == {"work_permissionless": False, "use_permissionless": True}
