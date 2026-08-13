"""Tests for the inference proxy and server peer in cluster config."""
from __future__ import annotations

import asyncio
import threading

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
        server_join_wg=True,
        server_wg_endpoint_host="203.0.113.99",
        server_wg_listen_port=51821,
        server_wg_ip_offset=254,
        api_port=8080,
    )


@pytest.fixture()
def store() -> Store:
    return Store(path=None)


@pytest.fixture()
def client(settings: Settings, store: Store):
    app = create_app(settings=settings, store=store)
    with TestClient(app) as c:
        yield c


def _new_user(client: TestClient, username="alice"):
    r = client.post("/v1/accounts/register", json={"username": username, "password": "hunter2hunter2"})
    acc = r.json()
    sess = client.post("/v1/accounts/login", json={"username": username, "password": "hunter2hunter2"}).json()
    key = client.post(
        f"/v1/accounts/{acc['account_id']}/keys",
        headers={"Authorization": f"Bearer {sess['session_token']}"},
        json={"name": "user", "scope": "user"},
    ).json()["api_key"]
    return key


def _new_worker(client: TestClient, username: str, wg_pubkey: str, memory_mb=2048):
    r = client.post("/v1/accounts/register", json={"username": username, "password": "hunter2hunter2"})
    acc = r.json()
    sess = client.post("/v1/accounts/login", json={"username": username, "password": "hunter2hunter2"}).json()
    key = client.post(
        f"/v1/accounts/{acc['account_id']}/keys",
        headers={"Authorization": f"Bearer {sess['session_token']}"},
        json={"name": "worker", "scope": "worker"},
    ).json()["api_key"]
    worker = client.post(
        "/v1/workers/register",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": "demo-model",
            "gguf_sha256": "a" * 64,
            "memory_allocated_mb": memory_mb,
            "wg_pubkey": wg_pubkey,
            "endpoint": {"host": "203.0.113.10", "port": 51820, "behind_nat": False, "nat_type": "none"},
        },
    ).json()
    return key, worker


def test_cluster_config_includes_server_peer(client: TestClient):
    # Form a live cluster and capture a member key.
    key1, w1 = _new_worker(client, "bob", "pubkey1")
    key2, w2 = _new_worker(client, "carol", "pubkey2")
    client.post(f"/v1/workers/{w1['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key1}"})
    client.post(f"/v1/workers/{w2['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key2}"})
    st1 = client.get(f"/v1/workers/{w1['worker_id']}/state", headers={"Authorization": f"Bearer {key1}"}).json()
    cluster_id = st1["cluster"]["cluster_id"]

    cfg = client.get(f"/v1/clusters/{cluster_id}/config", headers={"Authorization": f"Bearer {key1}"})
    assert cfg.status_code == 200, cfg.text
    body = cfg.json()
    # The server peer is appended with role="server".
    server_peers = [p for p in body["peers"] if p.get("role") == "server"]
    assert len(server_peers) == 1
    assert server_peers[0]["allowed_ips"] == ["10.23.1.254/32"]
    # Ring members are the two workers.
    ring_peers = [p for p in body["peers"] if p.get("role") != "server"]
    assert len(ring_peers) == 2


def test_proxy_requires_user_key(client: TestClient):
    # A worker key cannot call the proxy.
    key, _ = _new_worker(client, "frank", "pubkey5")
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": "demo-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 403


def test_proxy_no_live_cluster(client: TestClient):
    user_key = _new_user(client)
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {user_key}"},
        json={"model": "demo-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 404


def test_proxy_missing_model(client: TestClient):
    user_key = _new_user(client)
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {user_key}"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 400


def _serve_abrupt_sse() -> str:
    """Start a local HTTP server that mimics llama-server streaming: sends a
    200 + a few SSE chunks, then closes the connection abruptly (no
    terminating `data: [DONE]`). Returns its base URL.
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n')
                self.wfile.flush()
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n')
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                # Abrupt close: no [DONE], just drop the connection.
                self.connection.close()

        def log_message(self, *args):  # silence
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    return f"http://{host}:{port}", server


def test_proxy_streaming_survives_abrupt_upstream_close(client: TestClient, monkeypatch):
    """Regression: llama-server closes the TCP connection after the last SSE
    chunk instead of sending `data: [DONE]`. The proxy must treat the
    resulting ReadError as end-of-stream (client got all tokens) instead of
    crashing with an unhandled httpx.ReadError (which aborted the whole
    response — curl saw 'transfer closed with outstanding read data remaining')."""
    from prima_pool_server.router import ClusterRouter

    base_url, server = _serve_abrupt_sse()

    # Point the proxy at our local fake head.
    def fake_head_url(self, cluster):
        return base_url

    monkeypatch.setattr(ClusterRouter, "head_url", fake_head_url)

    # Form a live cluster. Disable the real server-side WG join (it would try
    # to write /etc/wireguard and fail in tests) — we're mocking the head URL.
    app = create_app(
        settings=Settings(
            models={"demo-model": ModelDef(slug="demo-model", gguf_sha256="a" * 64, required_memory_mb=4096)},
            assignable_grace_s=0,
            heartbeat_timeout_s=30,
            server_join_wg=False,
            api_port=8080,
        ),
        store=Store(path=None),
    )
    with TestClient(app) as c:
        key1, w1 = _new_worker(c, "bob", "pubkey1")
        key2, w2 = _new_worker(c, "carol", "pubkey2")
        c.post(f"/v1/workers/{w1['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key1}"})
        c.post(f"/v1/workers/{w2['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key2}"})

        # The cluster only goes LIVE when both members report ready.
        st1 = c.get(f"/v1/workers/{w1['worker_id']}/state", headers={"Authorization": f"Bearer {key1}"}).json()
        cluster_id = st1["cluster"]["cluster_id"]
        c.post(f"/v1/clusters/{cluster_id}/ready", headers={"Authorization": f"Bearer {key1}"}, json={"layer_windows": {"0": 24, "1": 24}})
        c.post(f"/v1/clusters/{cluster_id}/ready", headers={"Authorization": f"Bearer {key2}"}, json={})

        user_key = _new_user(c)
        r = c.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {user_key}"},
            json={"model": "demo-model", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        )
    server.shutdown()
    assert r.status_code == 200, r.text
    # The client receives both SSE chunks despite the abrupt upstream close.
    assert "Hel" in r.text
    assert "lo" in r.text


def _serve_usage_sse(prompt_tokens: int, completion_tokens: int) -> str:
    """A fake head that streams a couple of chunks plus a final usage chunk."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n')
            self.wfile.flush()
            usage = (
                'data: {"choices":[],"usage":{"prompt_tokens":%d,"completion_tokens":%d}}\n\n'
                % (prompt_tokens, completion_tokens)
            )
            self.wfile.write(usage.encode())
            self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        def log_message(self, *args):  # silence
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    return f"http://{host}:{port}", server


def _serve_usage_json(prompt_tokens: int, completion_tokens: int) -> str:
    """A fake head that returns a non-streaming JSON response with usage."""
    import json
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            body = json.dumps(
                {
                    "id": "cmpl-1",
                    "choices": [{"message": {"role": "assistant", "content": "Hi"}}],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # silence
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    return f"http://{host}:{port}", server


def _live_cluster(client: TestClient):
    """Form a live cluster and return (user_key, cluster_id)."""
    key1, w1 = _new_worker(client, "bob", "pubkey1")
    key2, w2 = _new_worker(client, "carol", "pubkey2")
    client.post(f"/v1/workers/{w1['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key1}"})
    client.post(f"/v1/workers/{w2['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key2}"})
    st1 = client.get(f"/v1/workers/{w1['worker_id']}/state", headers={"Authorization": f"Bearer {key1}"}).json()
    cluster_id = st1["cluster"]["cluster_id"]
    client.post(f"/v1/clusters/{cluster_id}/ready", headers={"Authorization": f"Bearer {key1}"}, json={"layer_windows": {"0": 24, "1": 24}})
    client.post(f"/v1/clusters/{cluster_id}/ready", headers={"Authorization": f"Bearer {key2}"}, json={})
    user_key = _new_user(client)
    return user_key, cluster_id


def test_proxy_records_usage_non_streaming(client: TestClient, store: Store, monkeypatch):
    from prima_pool_server.router import ClusterRouter

    base_url, server = _serve_usage_json(prompt_tokens=7, completion_tokens=9)
    monkeypatch.setattr(ClusterRouter, "head_url", lambda self, cluster: base_url)

    user_key, cluster_id = _live_cluster(client)
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {user_key}"},
        json={"model": "demo-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    server.shutdown()
    assert r.status_code == 200, r.text

    account_id = store.resolve_api_key(user_key).account_id
    reqs = store.list_requests_for_account(account_id)
    assert len(reqs) == 1
    assert reqs[0].prompt_tokens == 7
    assert reqs[0].completion_tokens == 9
    assert reqs[0].model == "demo-model"
    assert reqs[0].cluster_id == cluster_id


def test_proxy_records_usage_streaming(client: TestClient, store: Store, monkeypatch):
    from prima_pool_server.router import ClusterRouter

    base_url, server = _serve_usage_sse(prompt_tokens=3, completion_tokens=5)
    monkeypatch.setattr(ClusterRouter, "head_url", lambda self, cluster: base_url)

    user_key, cluster_id = _live_cluster(client)
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {user_key}"},
        json={"model": "demo-model", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    )
    server.shutdown()
    assert r.status_code == 200, r.text
    assert "Hi" in r.text

    account_id = store.resolve_api_key(user_key).account_id
    reqs = store.list_requests_for_account(account_id)
    assert len(reqs) == 1
    assert reqs[0].prompt_tokens == 3
    assert reqs[0].completion_tokens == 5
    assert reqs[0].cluster_id == cluster_id


def test_parse_sse_usage_extracts_last_usage():
    from prima_pool_server.app import _parse_sse_usage

    body = (
        b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
        b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":5}}\n\n'
        b"data: [DONE]\n\n"
    )
    assert _parse_sse_usage(body) == (3, 5)


def test_parse_sse_usage_returns_none_without_usage():
    from prima_pool_server.app import _parse_sse_usage

    # No usage chunk (e.g. abrupt upstream close before usage was sent).
    body = b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
    assert _parse_sse_usage(body) is None
    # Empty / non-SSE body.
    assert _parse_sse_usage(b"") is None
    assert _parse_sse_usage(b"not sse at all") is None


def test_parse_sse_usage_ignores_malformed_token_counts():
    from prima_pool_server.app import _parse_sse_usage

    # A malformed (non-numeric) token count must not crash the parser.
    body = (
        b'data: {"choices":[],"usage":{"prompt_tokens":"oops","completion_tokens":5}}\n\n'
        b'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":4}}\n\n'
    )
    assert _parse_sse_usage(body) == (2, 4)


def _record_usage(store: Store, account_id: str, key_id: str, model: str, cluster_id: str,
                  prompt: int, completion: int, created_at: float):
    from prima_pool_server.models import ClusterRecord, ClusterStatus, RequestRecord

    # Ensure the referenced cluster exists (requests.cluster_id is an FK).
    if store.get_cluster(cluster_id) is None:
        store.create_cluster(
            ClusterRecord(
                cluster_id=cluster_id,
                model=model,
                subnet="10.23.1.0/24",
                members=["w1"],
                ips={"w1": "10.23.1.1"},
                status=ClusterStatus.live,
            )
        )
    store.record_request(
        RequestRecord(
            request_id=f"req_{created_at}",
            account_id=account_id,
            key_id=key_id,
            model=model,
            cluster_id=cluster_id,
            prompt_tokens=prompt,
            completion_tokens=completion,
            created_at=created_at,
        )
    )


def test_usage_logs_endpoint(client: TestClient, store: Store):
    # Create a user + key, then record usage directly in the store.
    user_key = _new_user(client)
    account_id = store.resolve_api_key(user_key).account_id
    key_id = store.resolve_api_key(user_key).key_id

    _record_usage(store, account_id, key_id, "demo-model", "clu_1", 10, 20, 100.0)
    _record_usage(store, account_id, key_id, "demo-model", "clu_1", 30, 40, 200.0)
    _record_usage(store, account_id, key_id, "demo-model", "clu_1", 50, 60, 300.0)

    # Logs in [150, 350) -> only the 200 and 300 entries, newest first.
    r = client.get(
        f"/v1/accounts/{account_id}/usage/logs",
        params={"begin": 150, "end": 350},
        headers={"Authorization": f"Bearer {user_key}"},
    )
    assert r.status_code == 200, r.text
    logs = r.json()
    assert len(logs) == 2
    assert logs[0]["prompt_tokens"] == 50
    assert logs[0]["completion_tokens"] == 60
    assert logs[1]["prompt_tokens"] == 30
    assert logs[1]["model"] == "demo-model"
    assert logs[1]["cluster_id"] == "clu_1"


def test_usage_logs_endpoint_limit(client: TestClient, store: Store):
    user_key = _new_user(client)
    account_id = store.resolve_api_key(user_key).account_id
    key_id = store.resolve_api_key(user_key).key_id

    _record_usage(store, account_id, key_id, "demo-model", "clu_1", 10, 20, 100.0)
    _record_usage(store, account_id, key_id, "demo-model", "clu_1", 30, 40, 200.0)
    _record_usage(store, account_id, key_id, "demo-model", "clu_1", 50, 60, 300.0)

    # limit=1 -> only the newest entry in the window.
    r = client.get(
        f"/v1/accounts/{account_id}/usage/logs",
        params={"begin": 0, "end": 1000, "limit": 1},
        headers={"Authorization": f"Bearer {user_key}"},
    )
    assert r.status_code == 200, r.text
    logs = r.json()
    assert len(logs) == 1
    assert logs[0]["prompt_tokens"] == 50

    # Invalid limit is rejected.
    r = client.get(
        f"/v1/accounts/{account_id}/usage/logs",
        params={"begin": 0, "end": 1000, "limit": 0},
        headers={"Authorization": f"Bearer {user_key}"},
    )
    assert r.status_code == 400


def test_usage_logs_endpoint_requires_own_account(client: TestClient, store: Store):
    user_key = _new_user(client)
    account_id = store.resolve_api_key(user_key).account_id
    # A different account id must be rejected.
    r = client.get(
        f"/v1/accounts/acc_other/usage/logs",
        params={"begin": 0, "end": 1000},
        headers={"Authorization": f"Bearer {user_key}"},
    )
    assert r.status_code == 403


def test_usage_logs_endpoint_rejects_worker_key(client: TestClient, store: Store):
    key, _ = _new_worker(client, "frank", "pubkey5")
    r = client.get(
        "/v1/accounts/acc_x/usage/logs",
        params={"begin": 0, "end": 1000},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert r.status_code == 403


def test_usage_logs_latest_endpoint(client: TestClient, store: Store):
    user_key = _new_user(client)
    account_id = store.resolve_api_key(user_key).account_id
    key_id = store.resolve_api_key(user_key).key_id

    _record_usage(store, account_id, key_id, "demo-model", "clu_1", 10, 20, 100.0)
    _record_usage(store, account_id, key_id, "demo-model", "clu_1", 30, 40, 200.0)
    _record_usage(store, account_id, key_id, "demo-model", "clu_1", 50, 60, 300.0)

    # Latest 2 -> the two most recent, newest first.
    r = client.get(
        f"/v1/accounts/{account_id}/usage/logs/latest",
        params={"limit": 2},
        headers={"Authorization": f"Bearer {user_key}"},
    )
    assert r.status_code == 200, r.text
    logs = r.json()
    assert len(logs) == 2
    assert logs[0]["prompt_tokens"] == 50
    assert logs[0]["completion_tokens"] == 60
    assert logs[1]["prompt_tokens"] == 30

    # Default limit returns all three.
    r = client.get(
        f"/v1/accounts/{account_id}/usage/logs/latest",
        headers={"Authorization": f"Bearer {user_key}"},
    )
    assert r.status_code == 200
    assert len(r.json()) == 3

    # Invalid limit is rejected.
    r = client.get(
        f"/v1/accounts/{account_id}/usage/logs/latest",
        params={"limit": 0},
        headers={"Authorization": f"Bearer {user_key}"},
    )
    assert r.status_code == 400


def test_usage_stats_endpoint(client: TestClient, store: Store):
    user_key = _new_user(client)
    account_id = store.resolve_api_key(user_key).account_id
    key_id = store.resolve_api_key(user_key).key_id

    _record_usage(store, account_id, key_id, "model-a", "clu_1", 10, 20, 100.0)
    _record_usage(store, account_id, key_id, "model-a", "clu_1", 30, 40, 200.0)
    _record_usage(store, account_id, key_id, "model-b", "clu_1", 5, 6, 150.0)

    r = client.post(
        f"/v1/accounts/{account_id}/usage/stats",
        json={"windows": [[0, 1000]]},
        headers={"Authorization": f"Bearer {user_key}"},
    )
    assert r.status_code == 200, r.text
    stats = r.json()
    assert len(stats) == 1
    assert stats[0]["model-a"] == {"requests": 2, "prompt_tokens": 40, "completion_tokens": 60}
    assert stats[0]["model-b"] == {"requests": 1, "prompt_tokens": 5, "completion_tokens": 6}


def test_usage_stats_endpoint_multiple_windows(client: TestClient, store: Store):
    user_key = _new_user(client)
    account_id = store.resolve_api_key(user_key).account_id
    key_id = store.resolve_api_key(user_key).key_id

    _record_usage(store, account_id, key_id, "model-a", "clu_1", 10, 20, 100.0)
    _record_usage(store, account_id, key_id, "model-a", "clu_1", 30, 40, 200.0)

    r = client.post(
        f"/v1/accounts/{account_id}/usage/stats",
        json={"windows": [[0, 150], [150, 1000]]},
        headers={"Authorization": f"Bearer {user_key}"},
    )
    assert r.status_code == 200, r.text
    stats = r.json()
    assert len(stats) == 2
    assert stats[0]["model-a"] == {"requests": 1, "prompt_tokens": 10, "completion_tokens": 20}
    assert stats[1]["model-a"] == {"requests": 1, "prompt_tokens": 30, "completion_tokens": 40}


def test_parse_sse_usage_partial_counts():
    from prima_pool_server.app import _parse_sse_usage

    # A chunk carrying only prompt_tokens must still yield a result (completion
    # defaults to 0), not be dropped entirely.
    body = b'data: {"choices":[],"usage":{"prompt_tokens":7}}\n\n'
    assert _parse_sse_usage(body) == (7, 0)

    # A chunk carrying only completion_tokens.
    body = b'data: {"choices":[],"usage":{"completion_tokens":9}}\n\n'
    assert _parse_sse_usage(body) == (0, 9)

    # A later chunk fills in the missing field from an earlier one.
    body = (
        b'data: {"choices":[],"usage":{"prompt_tokens":7}}\n\n'
        b'data: {"choices":[],"usage":{"completion_tokens":9}}\n\n'
    )
    assert _parse_sse_usage(body) == (7, 9)


# ── worker-attributed usage endpoints ────────────────────────────────────
def _register_account_full(client: TestClient, username: str):
    """Register an account + login + issue a user key. Returns (acc, sess, user_key)."""
    acc = client.post(
        "/v1/accounts/register", json={"username": username, "password": "hunter2hunter2"}
    ).json()
    sess = client.post(
        "/v1/accounts/login", json={"username": username, "password": "hunter2hunter2"}
    ).json()
    user_key = client.post(
        f"/v1/accounts/{acc['account_id']}/keys",
        headers={"Authorization": f"Bearer {sess['session_token']}"},
        json={"name": "user", "scope": "user"},
    ).json()["api_key"]
    return acc, sess, user_key


def _register_worker_on_account(client: TestClient, acc: dict, sess: dict, wg_pubkey: str, memory_mb=2048):
    """Create a worker-scoped key + register a worker for an EXISTING account."""
    key = client.post(
        f"/v1/accounts/{acc['account_id']}/keys",
        headers={"Authorization": f"Bearer {sess['session_token']}"},
        json={"name": "worker", "scope": "worker"},
    ).json()["api_key"]
    worker = client.post(
        "/v1/workers/register",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": "demo-model",
            "gguf_sha256": "a" * 64,
            "memory_allocated_mb": memory_mb,
            "wg_pubkey": wg_pubkey,
            "endpoint": {"host": "203.0.113.10", "port": 51820, "behind_nat": False, "nat_type": "none"},
        },
    ).json()
    return key, worker


def _attribution_fixture(client: TestClient, store: Store, username="alice", wg_pubkey="pubkey1"):
    """Create an account owning both a user key and one worker. Returns
    (user_key, account_id, key_id, worker)."""
    acc, sess, user_key = _register_account_full(client, username)
    account_id = acc["account_id"]
    key_id = store.resolve_api_key(user_key).key_id
    _worker_key, worker = _register_worker_on_account(client, acc, sess, wg_pubkey, memory_mb=2048)
    return user_key, account_id, key_id, worker


def test_worker_logs_endpoint_share_and_effective(client: TestClient, store: Store):
    """One owned worker in a 2-worker cluster with distribution {w1:20, w2:10}.
    The user's worker gets share 2/3; the other account's worker row is absent."""
    from prima_pool_server.models import ClusterRecord, ClusterStatus

    user_key, account_id, key_id, worker = _attribution_fixture(client, store)
    # Create a second account whose worker is the other cluster member.
    other_acc, other_sess, _ = _register_account_full(client, "bob")
    other_worker = _register_worker_on_account(client, other_acc, other_sess, "pubkey2", memory_mb=2048)[1]
    cluster_id = "clu_w1"
    store.create_cluster(
        ClusterRecord(
            cluster_id=cluster_id,
            model="demo-model",
            subnet="10.23.1.0/24",
            members=[worker["worker_id"], other_worker["worker_id"]],
            ips={worker["worker_id"]: "10.23.1.1", other_worker["worker_id"]: "10.23.1.2"},
            status=ClusterStatus.live,
            ready={worker["worker_id"], other_worker["worker_id"]},
            layer_windows={worker["worker_id"]: 20, other_worker["worker_id"]: 10},
        )
    )
    _record_usage(store, account_id, key_id, "demo-model", cluster_id, 30, 60, 100.0)
    r = client.get(
        f"/v1/accounts/{account_id}/worker-logs",
        params={"begin": 0, "end": 1000},
        headers={"Authorization": f"Bearer {user_key}"},
    )
    assert r.status_code == 200, r.text
    logs = r.json()
    # Only the user's worker appears (one row), with share 2/3.
    assert len(logs) == 1
    entry = logs[0]
    assert entry["worker_id"] == worker["worker_id"]
    assert entry["prompt_tokens"] == 30
    assert entry["completion_tokens"] == 60
    assert entry["share"] == pytest.approx(20 / 30)
    assert entry["effective_prompt"] == pytest.approx(30 * 20 / 30)
    assert entry["effective_completion"] == pytest.approx(60 * 20 / 30)


def test_worker_logs_endpoint_unknown_distribution_nulls(client: TestClient, store: Store):
    """With no distribution reported, share/effective are null (not 0)."""
    from prima_pool_server.models import ClusterRecord, ClusterStatus

    user_key, account_id, key_id, worker = _attribution_fixture(client, store)
    cluster_id = "clu_w1"
    store.create_cluster(
        ClusterRecord(
            cluster_id=cluster_id,
            model="demo-model",
            subnet="10.23.1.0/24",
            members=[worker["worker_id"]],
            ips={worker["worker_id"]: "10.23.1.1"},
            status=ClusterStatus.live,
            ready={worker["worker_id"]},
            layer_windows=None,
        )
    )
    _record_usage(store, account_id, key_id, "demo-model", cluster_id, 10, 20, 100.0)
    r = client.get(
        f"/v1/accounts/{account_id}/worker-logs",
        params={"begin": 0, "end": 1000},
        headers={"Authorization": f"Bearer {user_key}"},
    )
    assert r.status_code == 200, r.text
    entry = r.json()[0]
    assert entry["share"] is None
    assert entry["effective_prompt"] is None
    assert entry["effective_completion"] is None


def test_worker_logs_endpoint_forwarder_zero(client: TestClient, store: Store):
    """A forwarder (layer_window 0) is emitted with share 0.0 and effective 0.0."""
    from prima_pool_server.models import ClusterRecord, ClusterStatus

    user_key, account_id, key_id, worker = _attribution_fixture(client, store)
    # Second member (another account) does all the layers; the user's worker
    # is a forwarder with 0 layers.
    other_acc, other_sess, _ = _register_account_full(client, "bob")
    other_worker = _register_worker_on_account(client, other_acc, other_sess, "pubkey2", memory_mb=2048)[1]
    cluster_id = "clu_w1"
    store.create_cluster(
        ClusterRecord(
            cluster_id=cluster_id,
            model="demo-model",
            subnet="10.23.1.0/24",
            members=[worker["worker_id"], other_worker["worker_id"]],
            ips={worker["worker_id"]: "10.23.1.1", other_worker["worker_id"]: "10.23.1.2"},
            status=ClusterStatus.live,
            ready={worker["worker_id"], other_worker["worker_id"]},
            layer_windows={worker["worker_id"]: 0, other_worker["worker_id"]: 36},
        )
    )
    _record_usage(store, account_id, key_id, "demo-model", cluster_id, 10, 20, 100.0)
    r = client.get(
        f"/v1/accounts/{account_id}/worker-logs",
        params={"begin": 0, "end": 1000},
        headers={"Authorization": f"Bearer {user_key}"},
    )
    assert r.status_code == 200, r.text
    entry = r.json()[0]
    assert entry["share"] == 0.0
    assert entry["effective_prompt"] == 0.0
    assert entry["effective_completion"] == 0.0


def test_worker_logs_latest_endpoint(client: TestClient, store: Store):
    from prima_pool_server.models import ClusterRecord, ClusterStatus

    user_key, account_id, key_id, worker = _attribution_fixture(client, store)
    cluster_id = "clu_w1"
    store.create_cluster(
        ClusterRecord(
            cluster_id=cluster_id,
            model="demo-model",
            subnet="10.23.1.0/24",
            members=[worker["worker_id"]],
            ips={worker["worker_id"]: "10.23.1.1"},
            status=ClusterStatus.live,
            ready={worker["worker_id"]},
            layer_windows={worker["worker_id"]: 36},
        )
    )
    _record_usage(store, account_id, key_id, "demo-model", cluster_id, 10, 20, 100.0)
    _record_usage(store, account_id, key_id, "demo-model", cluster_id, 30, 40, 200.0)
    r = client.get(
        f"/v1/accounts/{account_id}/worker-logs/latest",
        params={"limit": 1},
        headers={"Authorization": f"Bearer {user_key}"},
    )
    assert r.status_code == 200, r.text
    logs = r.json()
    assert len(logs) == 1
    assert logs[0]["prompt_tokens"] == 30
    assert logs[0]["effective_prompt"] == pytest.approx(30.0)


def test_worker_stats_endpoint(client: TestClient, store: Store):
    from prima_pool_server.models import ClusterRecord, ClusterStatus

    user_key, account_id, key_id, worker = _attribution_fixture(client, store)
    cluster_id = "clu_w1"
    store.create_cluster(
        ClusterRecord(
            cluster_id=cluster_id,
            model="demo-model",
            subnet="10.23.1.0/24",
            members=[worker["worker_id"]],
            ips={worker["worker_id"]: "10.23.1.1"},
            status=ClusterStatus.live,
            ready={worker["worker_id"]},
            layer_windows={worker["worker_id"]: 36},
        )
    )
    _record_usage(store, account_id, key_id, "demo-model", cluster_id, 10, 20, 100.0)
    _record_usage(store, account_id, key_id, "demo-model", cluster_id, 30, 40, 200.0)
    r = client.post(
        f"/v1/accounts/{account_id}/worker-stats",
        json={"windows": [[0, 1000]]},
        headers={"Authorization": f"Bearer {user_key}"},
    )
    assert r.status_code == 200, r.text
    stats = r.json()
    assert len(stats) == 1
    assert stats[0]["demo-model"]["total_tokens"] == [40.0, 60.0]
    assert stats[0]["demo-model"]["effective_tokens"] == pytest.approx([40.0, 60.0])


def test_worker_stats_endpoint_worker_ids_filter(client: TestClient, store: Store):
    from prima_pool_server.models import ClusterRecord, ClusterStatus

    user_key, account_id, key_id, worker = _attribution_fixture(client, store)
    # Add a second worker to the SAME account (alice).
    alice_sess = client.post(
        "/v1/accounts/login", json={"username": "alice", "password": "hunter2hunter2"}
    ).json()
    alice_acc_dict = {"account_id": account_id}
    _, worker2 = _register_worker_on_account(client, alice_acc_dict, alice_sess, "pubkey3", memory_mb=2048)
    cluster_id = "clu_w1"
    store.create_cluster(
        ClusterRecord(
            cluster_id=cluster_id,
            model="demo-model",
            subnet="10.23.1.0/24",
            members=[worker["worker_id"], worker2["worker_id"]],
            ips={worker["worker_id"]: "10.23.1.1", worker2["worker_id"]: "10.23.1.2"},
            status=ClusterStatus.live,
            ready={worker["worker_id"], worker2["worker_id"]},
            layer_windows={worker["worker_id"]: 30, worker2["worker_id"]: 10},
        )
    )
    _record_usage(store, account_id, key_id, "demo-model", cluster_id, 40, 80, 100.0)
    # Filter to the second worker only → total = its share of 40/80.
    r = client.post(
        f"/v1/accounts/{account_id}/worker-stats",
        json={"windows": [[0, 1000]], "worker_ids": [worker2["worker_id"]]},
        headers={"Authorization": f"Bearer {user_key}"},
    )
    assert r.status_code == 200, r.text
    stats = r.json()
    assert stats[0]["demo-model"]["total_tokens"] == [40.0, 80.0]
    assert stats[0]["demo-model"]["effective_tokens"] == pytest.approx([10.0, 20.0])


def test_worker_logs_endpoint_requires_own_account(client: TestClient, store: Store):
    user_key = _attribution_fixture(client, store)[0]
    r = client.get(
        "/v1/accounts/acc_other/worker-logs",
        params={"begin": 0, "end": 1000},
        headers={"Authorization": f"Bearer {user_key}"},
    )
    assert r.status_code == 403


def test_worker_logs_endpoint_rejects_worker_key(client: TestClient, store: Store):
    key, _ = _new_worker(client, "frank", "pubkey5")
    r = client.get(
        "/v1/accounts/acc_x/worker-logs",
        params={"begin": 0, "end": 1000},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert r.status_code == 403


def test_worker_logs_endpoint_validation(client: TestClient, store: Store):
    user_key, account_id, _, _ = _attribution_fixture(client, store)
    # begin >= end is rejected.
    r = client.get(
        f"/v1/accounts/{account_id}/worker-logs",
        params={"begin": 1000, "end": 100},
        headers={"Authorization": f"Bearer {user_key}"},
    )
    assert r.status_code == 400
    # limit 0 is rejected.
    r = client.get(
        f"/v1/accounts/{account_id}/worker-logs",
        params={"begin": 0, "end": 1000, "limit": 0},
        headers={"Authorization": f"Bearer {user_key}"},
    )
    assert r.status_code == 400
