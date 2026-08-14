"""Tests for the account balance system (v0.8).

Covers the store layer (migration, set/adjust atomicity, append-only events
with no FK), the admin-gated management endpoints, the account-scoped view
endpoints, and the string-serialized wire contract.
"""
from __future__ import annotations

import sqlite3

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
    return TestClient(create_app(settings=settings, store=Store(path=None)))


def _register(client: TestClient, username="alice", password="hunter2hunter2"):
    r = client.post("/v1/accounts/register", json={"username": username, "password": password})
    assert r.status_code == 201, r.text
    return r.json()


def _login(client: TestClient, username="alice", password="hunter2hunter2"):
    r = client.post("/v1/accounts/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def _promote(client: TestClient, username="alice"):
    """Register + login + directly promote to admin (test-only shortcut)."""
    _register(client, username=username)
    sess = _login(client, username=username)
    account_id = sess["session_token"].split(".")[0].replace("sess_", "")
    client.app.state.store.update_account_permissions(account_id, is_admin=True)
    return account_id, sess["session_token"]


# ── store layer ──────────────────────────────────────────────────────────
def test_migrate_existing_db_adds_balance(tmp_path):
    """A pre-v0.8 accounts table is upgraded in place: balance_minor is added
    with default 0, and existing rows read back 0."""
    db = str(tmp_path / "store.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE accounts (account_id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, created_at REAL NOT NULL);
        """
    )
    conn.execute("INSERT INTO accounts VALUES ('acc_1','alice','h',1.0)")
    conn.commit()
    conn.close()

    s = Store(path=db)
    acc = s.get_account("acc_1")
    assert acc is not None
    assert acc.balance == 0
    # New accounts also default to 0.
    new = s.create_account("bob", "hunter2hunter2")
    assert new is not None
    assert new.balance == 0
    s.close()


def test_balance_set_and_adjust_roundtrip(tmp_path):
    s = Store(path=str(tmp_path / "store.db"))
    acc = s.create_account("alice", "hunter2hunter2")
    assert acc is not None
    assert s.get_balance(acc.account_id) == 0

    # Set to an absolute value.
    assert s.set_balance(acc.account_id, 1500000000000000000) is True
    assert s.get_balance(acc.account_id) == 1500000000000000000

    # Adjust by a positive delta.
    assert s.adjust_balance(acc.account_id, 250000000000000000) == 1750000000000000000
    # Adjust by a negative delta (deduction) — allowed, may go negative.
    assert s.adjust_balance(acc.account_id, -2000000000000000000) == -250000000000000000

    # Nonexistent account → None / False.
    assert s.get_balance("acc_missing") is None
    assert s.set_balance("acc_missing", 5) is False
    assert s.adjust_balance("acc_missing", 5) is None
    s.close()


def test_balance_events_are_append_only_and_ordered(tmp_path):
    s = Store(path=str(tmp_path / "store.db"))
    acc = s.create_account("alice", "hunter2hunter2")
    s.set_balance(acc.account_id, 1000, admin_account_id="acc_admin", reason="grant")
    s.adjust_balance(acc.account_id, 500, admin_account_id="acc_admin", reason="bonus")
    s.adjust_balance(acc.account_id, -200, admin_account_id="acc_admin")

    events = s.list_balance_events(acc.account_id)
    # Newest first.
    assert [e.kind for e in events] == ["adjust", "adjust", "set"]
    assert [e.delta for e in events] == [-200, 500, 1000]
    assert [e.balance_after for e in events] == [1300, 1500, 1000]
    assert [e.balance_before for e in events] == [1500, 1000, 0]
    # The "set" event's delta is the absolute transition (1000 - 0).
    assert events[2].delta == 1000
    assert events[2].kind == "set"
    assert events[0].reason is None
    assert events[2].reason == "grant"
    assert all(e.admin_account_id == "acc_admin" for e in events)
    s.close()


def test_balance_events_have_no_fk_and_survive_account_deletion(tmp_path):
    """balance_events.account_id is FK-less, so history survives a raw account
    row deletion (mirrors requests.cluster_id / cluster_members.worker_id)."""
    s = Store(path=str(tmp_path / "store.db"))
    acc = s.create_account("alice", "hunter2hunter2")
    s.set_balance(acc.account_id, 500)

    # Hard-delete the account row directly (no account-deletion API yet).
    with s._lock:
        with s._conn:
            s._conn.execute("DELETE FROM accounts WHERE account_id = ?", (acc.account_id,))

    events = s.list_balance_events(acc.account_id)
    assert len(events) == 1
    assert events[0].balance_after == 500
    s.close()


def test_balance_events_limit(tmp_path):
    s = Store(path=str(tmp_path / "store.db"))
    acc = s.create_account("alice", "hunter2hunter2")
    for i in range(5):
        s.adjust_balance(acc.account_id, 1)
    assert len(s.list_balance_events(acc.account_id, limit=3)) == 3
    assert len(s.list_balance_events(acc.account_id)) == 5
    s.close()


# ── API layer ────────────────────────────────────────────────────────────
def test_admin_can_set_and_adjust_balance():
    client = _make_client(_settings())
    with client:
        admin_id, tok = _promote(client, "alice")
        _register(client, username="bob")
        bob_id = _login(client, username="bob")["session_token"].split(".")[0].replace("sess_", "")

        # Set to a large value (transported as a string to avoid float64 loss).
        r = client.put(
            f"/v1/admin/accounts/{bob_id}/balance",
            headers={"Authorization": f"Bearer {tok}"},
            json={"balance": "1500000000000000000"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["balance"] == "1500000000000000000"

        # Adjust up.
        r = client.post(
            f"/v1/admin/accounts/{bob_id}/balance/adjust",
            headers={"Authorization": f"Bearer {tok}"},
            json={"delta": 250000000000000000},
        )
        assert r.status_code == 200, r.text
        assert r.json()["balance"] == "1750000000000000000"

        # Adjust down (negative delta).
        r = client.post(
            f"/v1/admin/accounts/{bob_id}/balance/adjust",
            headers={"Authorization": f"Bearer {tok}"},
            json={"delta": "-2000000000000000000"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["balance"] == "-250000000000000000"


def test_balance_is_string_serialized_and_present_on_admin_list():
    client = _make_client(_settings())
    with client:
        admin_id, tok = _promote(client, "alice")
        _register(client, username="bob")
        bob_id = _login(client, username="bob")["session_token"].split(".")[0].replace("sess_", "")
        client.put(
            f"/v1/admin/accounts/{bob_id}/balance",
            headers={"Authorization": f"Bearer {tok}"},
            json={"balance": "9000000000000000000"},
        )
        # AdminAccount carries balance as a string.
        accounts = {a["username"]: a for a in client.get(
            "/v1/admin/accounts", headers={"Authorization": f"Bearer {tok}"}
        ).json()}
        assert accounts["bob"]["balance"] == "9000000000000000000"
        assert isinstance(accounts["bob"]["balance"], str)
        # Register response + dashboard also expose balance.
        assert isinstance(_register(client, username="carol")["balance"], str)


def test_balance_over_int64_is_rejected():
    """A balance beyond the SQLite INTEGER ceiling is rejected with 422 (not a
    500 from OverflowError)."""
    client = _make_client(_settings())
    with client:
        admin_id, tok = _promote(client, "alice")
        _register(client, username="bob")
        bob_id = _login(client, username="bob")["session_token"].split(".")[0].replace("sess_", "")
        r = client.put(
            f"/v1/admin/accounts/{bob_id}/balance",
            headers={"Authorization": f"Bearer {tok}"},
            json={"balance": "9223372036854775808"},  # 2^63
        )
        assert r.status_code == 422, r.text


def test_adjust_overflow_returns_400_not_500():
    """balance + delta can overflow 64-bit even when both inputs are in range;
    the store must reject it cleanly (400) rather than raise OverflowError."""
    client = _make_client(_settings())
    with client:
        admin_id, tok = _promote(client, "alice")
        _register(client, username="bob")
        bob_sess = _login(client, username="bob")
        bob_id = bob_sess["session_token"].split(".")[0].replace("sess_", "")
        maxv = str(2**63 - 1)
        # Set to INT64_MAX, then adjust +1 → overflow.
        client.put(
            f"/v1/admin/accounts/{bob_id}/balance",
            headers={"Authorization": f"Bearer {tok}"},
            json={"balance": maxv},
        )
        r = client.post(
            f"/v1/admin/accounts/{bob_id}/balance/adjust",
            headers={"Authorization": f"Bearer {tok}"},
            json={"delta": 1},
        )
        assert r.status_code == 400, r.text
        # Balance unchanged.
        assert client.get(
            f"/v1/accounts/{bob_id}/balance",
            headers={"Authorization": f"Bearer {bob_sess['session_token']}"},
        ).json()["balance"] == maxv


def test_set_delta_overflow_returns_400_not_500():
    """A set whose recorded delta (after - before) exceeds 64-bit (e.g.
    INT64_MIN → INT64_MAX) must be rejected cleanly, not 500 on the event."""
    client = _make_client(_settings())
    with client:
        admin_id, tok = _promote(client, "alice")
        _register(client, username="bob")
        bob_sess = _login(client, username="bob")
        bob_id = bob_sess["session_token"].split(".")[0].replace("sess_", "")
        minv = str(-(2**63))
        maxv = str(2**63 - 1)
        client.put(
            f"/v1/admin/accounts/{bob_id}/balance",
            headers={"Authorization": f"Bearer {tok}"},
            json={"balance": minv},
        )
        r = client.put(
            f"/v1/admin/accounts/{bob_id}/balance",
            headers={"Authorization": f"Bearer {tok}"},
            json={"balance": maxv},
        )
        assert r.status_code == 400, r.text
        # Balance unchanged (still INT64_MIN).
        assert client.get(
            f"/v1/accounts/{bob_id}/balance",
            headers={"Authorization": f"Bearer {bob_sess['session_token']}"},
        ).json()["balance"] == minv


def test_admin_events_handles_null_admin():
    """An event with no recorded admin (admin_account_id is None) must not 500
    the admin events endpoint — admin_username is None in the response."""
    client = _make_client(_settings())
    with client:
        admin_id, tok = _promote(client, "alice")
        _register(client, username="bob")
        bob_id = _login(client, username="bob")["session_token"].split(".")[0].replace("sess_", "")
        # Direct store write with no admin (simulates a store-level op).
        client.app.state.store.set_balance(bob_id, 7)
        r = client.get(
            f"/v1/admin/accounts/{bob_id}/balance/events",
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 200, r.text
        assert r.json()[0]["admin_username"] is None


def test_account_can_view_own_balance_and_events():
    client = _make_client(_settings())
    with client:
        admin_id, tok = _promote(client, "alice")
        _register(client, username="bob")
        bob_sess = _login(client, username="bob")
        bob_id = bob_sess["session_token"].split(".")[0].replace("sess_", "")

        client.put(
            f"/v1/admin/accounts/{bob_id}/balance",
            headers={"Authorization": f"Bearer {tok}"},
            json={"balance": "5000", "reason": "deposit"},
        )

        # Own balance (session token).
        r = client.get(
            f"/v1/accounts/{bob_id}/balance",
            headers={"Authorization": f"Bearer {bob_sess['session_token']}"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["balance"] == "5000"

        # Own events (session token) — no admin identity leaked.
        evs = client.get(
            f"/v1/accounts/{bob_id}/balance/events",
            headers={"Authorization": f"Bearer {bob_sess['session_token']}"},
        ).json()
        assert len(evs) == 1
        assert evs[0]["kind"] == "set"
        assert evs[0]["balance_after"] == "5000"
        assert evs[0]["reason"] == "deposit"
        assert "admin_username" not in evs[0]


def test_admin_events_expose_admin_username():
    client = _make_client(_settings())
    with client:
        admin_id, tok = _promote(client, "alice")
        _register(client, username="bob")
        bob_id = _login(client, username="bob")["session_token"].split(".")[0].replace("sess_", "")
        client.put(
            f"/v1/admin/accounts/{bob_id}/balance",
            headers={"Authorization": f"Bearer {tok}"},
            json={"balance": "42"},
        )
        evs = client.get(
            f"/v1/admin/accounts/{bob_id}/balance/events",
            headers={"Authorization": f"Bearer {tok}"},
        ).json()
        assert len(evs) == 1
        assert evs[0]["admin_username"] == "alice"


def test_balance_endpoints_require_admin():
    client = _make_client(_settings())
    with client:
        _register(client, username="alice")
        sess = _login(client, username="alice")
        account_id = sess["session_token"].split(".")[0].replace("sess_", "")
        tok = sess["session_token"]

        # Non-admin cannot set/adjust/list (403).
        assert client.put(
            f"/v1/admin/accounts/{account_id}/balance",
            headers={"Authorization": f"Bearer {tok}"},
            json={"balance": 10},
        ).status_code == 403
        assert client.post(
            f"/v1/admin/accounts/{account_id}/balance/adjust",
            headers={"Authorization": f"Bearer {tok}"},
            json={"delta": 10},
        ).status_code == 403
        assert client.get(
            f"/v1/admin/accounts/{account_id}/balance/events",
            headers={"Authorization": f"Bearer {tok}"},
        ).status_code == 403

        # Missing account → 404.
        admin_id, atok = _promote(client, "root")
        assert client.put(
            "/v1/admin/accounts/acc_missing/balance",
            headers={"Authorization": f"Bearer {atok}"},
            json={"balance": 10},
        ).status_code == 404


def test_account_cannot_view_another_balance():
    client = _make_client(_settings())
    with client:
        _register(client, username="alice")
        _register(client, username="bob")
        alice_sess = _login(client, username="alice")
        bob_id = _login(client, username="bob")["session_token"].split(".")[0].replace("sess_", "")
        r = client.get(
            f"/v1/accounts/{bob_id}/balance",
            headers={"Authorization": f"Bearer {alice_sess['session_token']}"},
        )
        assert r.status_code == 403, r.text
        r = client.get(
            f"/v1/accounts/{bob_id}/balance/events",
            headers={"Authorization": f"Bearer {alice_sess['session_token']}"},
        )
        assert r.status_code == 403, r.text


def test_dashboard_exposes_balance():
    client = _make_client(_settings())
    with client:
        sess = _login(client, username=_register(client)["username"])
        account_id = sess["session_token"].split(".")[0].replace("sess_", "")
        r = client.get(
            f"/v1/accounts/{account_id}/dashboard",
            headers={"Authorization": f"Bearer {sess['session_token']}"},
        )
        assert r.status_code == 200
        assert "balance" in r.json()
        assert isinstance(r.json()["balance"], str)


def test_adjust_balance_zero_and_reason_roundtrip():
    client = _make_client(_settings())
    with client:
        admin_id, tok = _promote(client, "alice")
        _register(client, username="bob")
        bob_id = _login(client, username="bob")["session_token"].split(".")[0].replace("sess_", "")
        # A zero delta is valid and still records an event.
        r = client.post(
            f"/v1/admin/accounts/{bob_id}/balance/adjust",
            headers={"Authorization": f"Bearer {tok}"},
            json={"delta": 0, "reason": "noop"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["balance"] == "0"
        evs = client.get(
            f"/v1/admin/accounts/{bob_id}/balance/events",
            headers={"Authorization": f"Bearer {tok}"},
        ).json()
        assert evs[0]["delta"] == "0"
        assert evs[0]["reason"] == "noop"
