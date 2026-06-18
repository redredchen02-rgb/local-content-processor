"""Unit 1: the webui server's fail-closed request-authorization chain.

These exercise the PURE ``authorize`` helper with synthetic header dicts — no
socket, no server — so every denial variant is a fast, deterministic negative
test (mirroring the Stage-2 fail-closed ethos: a bad/missing header must be
REFUSED, never allowed). The chain order is Host allowlist -> token -> CSRF
(Origin/Sec-Fetch-Site), and it is the in-process bridge's network trust boundary
rebuilt by hand (see docs/plans/2026-06-18-003-...-plan.md, Unit 1).

``authorize`` returns a short denial REASON string, or ``None`` when the request
is allowed. Tests assert on "allowed vs denied", not on the exact reason text.
"""

from lcp import webserver

PORT = 8765
GOOD_TOKEN = "s3cr3t-token-value"


def _api_headers(**overrides):
    """A fully-valid /api/* request's headers; override one to make it bad."""
    h = {
        "Host": f"127.0.0.1:{PORT}",
        "Authorization": f"Bearer {GOOD_TOKEN}",
        "Sec-Fetch-Site": "same-origin",
    }
    h.update(overrides)
    return h


def _authz(path, headers):
    return webserver.authorize(
        path=path, headers=headers, token=GOOD_TOKEN, port=PORT
    )


# --- happy paths -----------------------------------------------------------


def test_valid_api_request_allowed():
    assert _authz("/api/summary", _api_headers()) is None


def test_static_get_allowed_with_host_only():
    # A navigation/static GET legitimately has no token and Sec-Fetch-Site: none
    # (or absent) — it must still be served as long as the Host is loopback.
    assert _authz("/app.js", {"Host": f"127.0.0.1:{PORT}"}) is None
    assert _authz("/", {"Host": f"localhost:{PORT}", "Sec-Fetch-Site": "none"}) is None


# --- Host allowlist (anti DNS-rebinding) -----------------------------------


def test_host_localhost_and_ipv6_loopback_allowed():
    for host in (f"localhost:{PORT}", f"[::1]:{PORT}", f"127.0.0.1:{PORT}"):
        assert _authz("/api/summary", _api_headers(Host=host)) is None, host


def test_host_without_port_denied():
    assert _authz("/api/summary", _api_headers(Host="127.0.0.1")) is not None


def test_host_wrong_port_denied():
    assert _authz("/api/summary", _api_headers(Host=f"127.0.0.1:{PORT + 1}")) is not None


def test_host_rebinding_domains_denied():
    # The DNS-rebinding case: the browser keeps the attacker's domain in Host.
    # A substring/startswith check would wrongly pass `127.0.0.1.evil.com`.
    for host in (
        f"127.0.0.1.evil.com:{PORT}",
        f"evil.com:{PORT}",
        f"127.0.0.1:{PORT}.evil.com",
    ):
        assert _authz("/api/summary", _api_headers(Host=host)) is not None, host


def test_missing_host_denied():
    h = _api_headers()
    del h["Host"]
    assert _authz("/api/summary", h) is not None


def test_static_get_bad_host_denied():
    # Host check applies to static requests too (rebinding can target the page).
    assert _authz("/app.js", {"Host": f"evil.com:{PORT}"}) is not None


# --- token (anti local-process forgery) ------------------------------------


def test_api_missing_token_denied():
    h = _api_headers()
    del h["Authorization"]
    assert _authz("/api/approve", h) is not None


def test_api_wrong_token_denied():
    assert _authz("/api/approve", _api_headers(Authorization="Bearer nope")) is not None


def test_api_wrong_token_same_length_denied():
    # Constant-time compare must still reject a same-length wrong token.
    wrong = "x" * len(GOOD_TOKEN)
    assert len(wrong) == len(GOOD_TOKEN)
    assert _authz("/api/approve", _api_headers(Authorization=f"Bearer {wrong}")) is not None


def test_api_token_without_bearer_prefix_denied():
    assert _authz("/api/approve", _api_headers(Authorization=GOOD_TOKEN)) is not None


# --- CSRF: Origin / Sec-Fetch-Site -----------------------------------------


def test_api_cross_site_denied():
    assert _authz("/api/approve", _api_headers(**{"Sec-Fetch-Site": "cross-site"})) is not None


def test_api_same_site_denied():
    # same-site is NOT same-origin; a sibling origin must not pass.
    assert _authz("/api/approve", _api_headers(**{"Sec-Fetch-Site": "same-site"})) is not None


def test_api_origin_fallback_allowed_when_loopback():
    # No Sec-Fetch-Site (older client) but a loopback Origin -> allowed.
    h = _api_headers()
    del h["Sec-Fetch-Site"]
    h["Origin"] = f"http://127.0.0.1:{PORT}"
    assert _authz("/api/approve", h) is None


def test_api_origin_fallback_evil_denied():
    h = _api_headers()
    del h["Sec-Fetch-Site"]
    h["Origin"] = "https://evil.com"
    assert _authz("/api/approve", h) is not None


def test_api_no_secfetch_no_origin_denied():
    # Fail-closed: neither browser signal present on a state-changing request.
    h = _api_headers()
    del h["Sec-Fetch-Site"]
    assert "Origin" not in h
    assert _authz("/api/approve", h) is not None


# --- helper surfaces --------------------------------------------------------


def test_allowed_hosts_are_port_inclusive():
    hosts = webserver.allowed_hosts(PORT)
    assert f"127.0.0.1:{PORT}" in hosts
    assert "127.0.0.1" not in hosts  # bare host (no port) is never allowed


def test_server_host_is_loopback():
    assert webserver.SERVER_HOST == "127.0.0.1"
