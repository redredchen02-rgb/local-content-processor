"""Unit 2: the http.server transport, exercised over a REAL loopback socket.

Per docs/solutions/unit-tests-mask-integration-bugs.md, the JSON<->Python wire is
a new producer<->consumer seam that Api-only unit tests cannot prove. So these
boot a real server on 127.0.0.1:0 and drive it with a real HTTP client, asserting
the round-tripped dict equals a direct Api call — plus the security gate, arity
guard, typed-error contract, in-flight guard, doc-root lockdown, and headers.
"""

import http.client
import json
import threading

import pytest
import yaml

from lcp import webserver
from lcp.gui import Api

TOKEN = "test-token-abcdefghijklmnop"


def _api(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {"storage": {"base_dir": str(tmp_path)}, "publisher": {"reviewers": ["alice"]}}
        ),
        encoding="utf-8",
    )
    return Api(config_path=str(cfg))


@pytest.fixture()
def server(tmp_path):
    srv = webserver.build_server(_api(tmp_path), token=TOKEN, port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)


def _request(port, method, path, *, headers=None, body=None):
    """Raw request with full header control (so we can forge Host/Origin/etc.)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    raw = body.encode() if isinstance(body, str) else (body or b"")
    conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    sent = headers or {}
    for k, v in sent.items():
        conn.putheader(k, v)
    if "Host" not in sent:
        conn.putheader("Host", f"127.0.0.1:{port}")
    if raw:
        conn.putheader("Content-Length", str(len(raw)))
    conn.endheaders(raw if raw else None)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp, data


def _api_headers(port, **overrides):
    h = {
        "Host": f"127.0.0.1:{port}",
        "Authorization": f"Bearer {TOKEN}",
        "Sec-Fetch-Site": "same-origin",
        "Content-Type": "application/json",
    }
    h.update(overrides)
    return h


def _post(port, name, args, **header_overrides):
    body = json.dumps({"args": args})
    return _request(
        port,
        "POST",
        f"/api/{name}",
        headers=_api_headers(port, **header_overrides),
        body=body,
    )


# --- happy round-trip + arg binding ----------------------------------------


def test_summary_roundtrip_matches_direct_call(server, tmp_path):
    resp, data = _post(server.public_port, "summary", [])
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "application/json"
    # Equal to calling the SAME Api method directly (the wire preserves the dict).
    assert json.loads(data) == server.api.summary()


def test_list_jobs_null_arg_binds_to_none(server):
    # JSON null -> Python None for the optional `state` filter.
    resp, data = _post(server.public_port, "list_jobs", [None])
    assert resp.status == 200
    assert json.loads(data) == server.api.list_jobs(None)


def test_no_arg_call_works(server):
    resp, data = _post(server.public_port, "reviewers", [])
    assert resp.status == 200
    assert json.loads(data)["reviewers"] == ["alice"]


# --- arity guard + typed-error contract ------------------------------------


def test_over_arity_is_typed_input_error(server):
    # summary() takes no args; a too-long array is a typed input error (exit 2),
    # NOT a generic 500 and NOT a silent ignore.
    resp, data = _post(server.public_port, "summary", [1, 2, 3])
    assert resp.status == 200
    body = json.loads(data)
    assert "error" in body and body["exit_code"] == 2


def test_typed_exit_code_crosses_the_wire(server):
    # approve on a nonexistent job raises an LcpError that @bridge_safe maps to a
    # typed exit_code:2 dict; the HTTP seam must preserve it (not collapse to 500).
    resp, data = _post(server.public_port, "approve", ["nojob", "alice"])
    assert resp.status == 200
    body = json.loads(data)
    assert "error" in body and body["exit_code"] == 2


def test_malformed_body_is_typed_not_500(server):
    resp, data = _request(
        server.public_port,
        "POST",
        "/api/summary",
        headers=_api_headers(server.public_port),
        body="not json",
    )
    assert resp.status == 200
    body = json.loads(data)
    assert "error" in body and body["exit_code"] == 2


def test_negative_content_length_is_typed_not_hang(server):
    # int("-1") is truthy; without the guard rfile.read(-1) would read to EOF.
    # A forged negative Content-Length must be a typed input error, not a hang.
    h = _api_headers(server.public_port)
    h["Content-Length"] = "-1"
    resp, data = _request(server.public_port, "POST", "/api/summary", headers=h, body=b"")
    assert resp.status == 200
    body = json.loads(data)
    assert "error" in body and body["exit_code"] == 2


def test_async_over_arity_typed_error_crosses_wire(server):
    # The dispatch seam's arity guard + LcpError mapping must hold for the
    # UN-@bridge_safe async kickoff methods too: an over-long args array to
    # process_async returns a typed exit_code:2 on the synchronous kickoff, not a 500.
    resp, data = _post(
        server.public_port, "process_async", ["j1", "t", False, None, None, False, "EXTRA"]
    )
    assert resp.status == 200
    body = json.loads(data)
    assert "error" in body and body["exit_code"] == 2


# --- in-flight guard (sync route) + shared instance ------------------------


def test_concurrent_same_job_sync_process_runs_once(server, monkeypatch):
    # Monkeypatch the SHARED Api instance's `process` to a slow stub: two
    # concurrent POST /api/process on one job must run it ONCE; the second sees
    # the in-flight status. Proves the seam guard AND the single shared instance.
    started = []
    in_slow = threading.Event()  # deterministic: set when the first call is inside
    release = threading.Event()

    def slow(job_id, *a, **k):
        started.append(job_id)
        in_slow.set()
        release.wait(timeout=3)
        return {"job_id": job_id, "ran": True}

    monkeypatch.setattr(server.api, "process", slow)

    results = {}

    def fire(tag):
        _, data = _post(server.public_port, "process", ["j1"])
        results[tag] = json.loads(data)

    t1 = threading.Thread(target=fire, args=("a",))
    t1.start()
    # Deterministically wait until the first call is INSIDE slow() (holding the
    # in-flight slot) before firing the second — no timing-based polling.
    assert in_slow.wait(timeout=3), "first request never entered the guarded call"
    t2 = threading.Thread(target=fire, args=("b",))
    t2.start()
    t2.join(timeout=3)
    release.set()
    t1.join(timeout=3)

    assert len(started) == 1, "the guarded job ran more than once"
    statuses = {tuple(sorted(r.items())) for r in results.values()}
    running = [r for r in results.values() if r.get("status") == "running"]
    ran = [r for r in results.values() if r.get("ran")]
    assert len(running) == 1 and len(ran) == 1, (results, statuses)


def test_async_process_blocks_concurrent_sync_process(server, monkeypatch):
    """SEC-002: process_async holding the inflight slot must block a concurrent
    sync process() on the same job — both paths must share the same registry."""
    started = []
    in_slow = threading.Event()
    release = threading.Event()

    def slow(job_id, *a, **k):
        started.append(job_id)
        in_slow.set()
        release.wait(timeout=3)
        return {"job_id": job_id, "ran": True}

    monkeypatch.setattr(server.api, "process", slow)

    results = {}

    def fire_async():
        _, data = _post(server.public_port, "process_async", ["j2", "", False, None, None, False])
        results["async"] = json.loads(data)

    def fire_sync():
        _, data = _post(server.public_port, "process", ["j2"])
        results["sync"] = json.loads(data)

    t1 = threading.Thread(target=fire_async)
    t1.start()
    # Wait until the async background thread is inside slow() (slot occupied).
    assert in_slow.wait(timeout=3), "async worker never entered the guarded call"
    t2 = threading.Thread(target=fire_sync)
    t2.start()
    t2.join(timeout=3)
    release.set()
    t1.join(timeout=3)

    assert len(started) == 1, "the guarded job ran more than once"
    assert results.get("sync", {}).get("status") == "running", (
        "sync process() should have seen the inflight guard, got: " + repr(results)
    )


# --- security gate, fail-closed, over the real socket ----------------------


def test_missing_token_401(server):
    h = _api_headers(server.public_port)
    del h["Authorization"]
    resp, _ = _request(server.public_port, "POST", "/api/summary", headers=h, body='{"args":[]}')
    assert resp.status == 401


def test_bad_host_403(server):
    resp, _ = _post(server.public_port, "summary", [], Host=f"evil.com:{server.public_port}")
    assert resp.status == 403


def test_cross_site_403(server):
    resp, _ = _post(server.public_port, "summary", [], **{"Sec-Fetch-Site": "cross-site"})
    assert resp.status == 403


def test_get_on_api_405(server):
    resp, _ = _request(
        server.public_port,
        "GET",
        "/api/summary",
        headers={"Host": f"127.0.0.1:{server.public_port}"},
    )
    assert resp.status == 405


def test_private_and_unknown_routes_404(server):
    for name in ("_ctx", "__init__", "nope"):
        resp, _ = _post(server.public_port, name, [])
        assert resp.status == 404, name


# --- doc-root lockdown ------------------------------------------------------


def test_path_traversal_blocked(server):
    for path in ("/../config.yaml", "/../../config.yaml"):
        resp, _ = _request(
            server.public_port,
            "GET",
            path,
            headers={"Host": f"127.0.0.1:{server.public_port}"},
        )
        assert resp.status == 404, path


# --- static serving + token injection + headers ----------------------------


def test_index_served_with_token_injected_and_headers(server):
    resp, data = _request(
        server.public_port,
        "GET",
        "/",
        headers={"Host": f"127.0.0.1:{server.public_port}", "Sec-Fetch-Site": "none"},
    )
    assert resp.status == 200
    text = data.decode()
    assert TOKEN in text  # placeholder replaced by the live token
    assert webserver.TOKEN_PLACEHOLDER not in text
    csp = resp.getheader("Content-Security-Policy")
    assert csp and "frame-ancestors 'none'" in csp
    assert resp.getheader("X-Frame-Options") == "DENY"
    assert "no-store" in (resp.getheader("Cache-Control") or "")


def test_app_js_served(server):
    resp, data = _request(
        server.public_port,
        "GET",
        "/app.js",
        headers={"Host": f"127.0.0.1:{server.public_port}"},
    )
    assert resp.status == 200
    assert b"function" in data  # it's the real JS asset


# --- parity (Finding 6): route table vs INDEPENDENT literal list -----------

# Hand-maintained: the public Api operator surface. Independent of the server's
# dir(Api) introspection, so adding/removing an Api method (or a route) without
# updating this list FAILS — the test is falsifiable, not a tautology.
EXPECTED_ROUTES = frozenset(
    {
        "init_workspace",
        "create_and_crawl",
        "crawl_ingested",
        "ingest_dir",
        "ingest_gossip",
        "templates",
        "process",
        "create_and_crawl_async",
        "process_async",
        "job_status",
        "get_job",
        "make_review_packet",
        "get_packet",
        "cover_report",
        "approve",
        "reject",
        "resolve",
        "backfill",
        "supersede",
        "list_jobs",
        "summary",
        "dashboard_stats",
        "saved_sources",
        "add_saved_source",
        "delete_saved_source",
        "reviewers",
        "disclaimer",
        "get_settings",
        "save_settings",
    }
)


def test_route_table_matches_independent_expected_list():
    assert webserver.public_routes(Api) == EXPECTED_ROUTES


def test_bound_socket_is_loopback(server):
    assert server.server_address[0] == "127.0.0.1"
