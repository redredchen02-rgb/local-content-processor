"""Localhost webui server (imperative shell) — replaces the pywebview window.

WHY THIS MODULE EXISTS
======================
The operator GUI used to be a pywebview desktop window whose built-in HTTP server
bound 127.0.0.1 and whose in-process js_api bridge exposed :class:`lcp.gui.Api`.
We now serve the SAME ``web/`` assets and the SAME ``Api`` methods over a real
stdlib :mod:`http.server` bound to 127.0.0.1, so the operator can open the UI in
Chrome and debug it. ``Api`` is unchanged; only the transport moves here.

THE NETWORK TRUST BOUNDARY IS REBUILT BY HAND
=============================================
An in-process bridge has NO socket, so it could not be reached by other local
processes, by other browser tabs (CSRF), or via DNS rebinding. A real HTTP socket
re-opens all of that. :func:`authorize` rebuilds the lost boundary as a
fail-closed chain run BEFORE any business logic, mirroring the Stage-2 gate ethos
(cheapest, most-decisive check first):

  1. **Host allowlist** (every request) — the only defense against DNS rebinding;
     ``http.server`` does NOT validate Host by default (the 2025 MCP SDK / MLflow
     CVE class). Exact, port-inclusive equality — never startswith/substring.
  2. **Per-launch token** (``/api/*`` only) — the only thing that stops OTHER local
     processes, which can forge Host/Origin/Sec-Fetch via curl. Constant-time
     compared; minted fresh per launch; delivered only via the served page's
     ``<meta>`` (never a URL/log/file).
  3. **Origin / Sec-Fetch-Site** (``/api/*`` only) — anti cross-site CSRF.
     ``Sec-Fetch-Site`` is browser-set and JS-unforgeable; ``Origin`` is the
     fallback. Either missing on a state-changing request -> fail closed.

Static GETs legitimately carry ``Sec-Fetch-Site: none`` and no token, so the
token/CSRF legs are scoped to ``/api/*``; the Host check applies to everything.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping

# The ONLY bind address. Never "" / 0.0.0.0 — that would expose the tool to the
# LAN and defeat every check below. A test asserts the bound socket is loopback.
SERVER_HOST = "127.0.0.1"

# Path prefix whose requests carry the full token + CSRF chain (state-mutating
# and data-returning API calls). Static asset GETs get the Host check only.
API_PREFIX = "/api/"


def allowed_hosts(port: int) -> frozenset[str]:
    """The exact, PORT-INCLUSIVE set of acceptable ``Host`` header values.

    Port inclusion matters: comparing the host without the port is the
    documented DNS-rebinding bypass (MLflow #22095). ``localhost`` and ``[::1]``
    are included for operator convenience even though we bind IPv4 127.0.0.1 —
    allowing a name that cannot reach the socket is harmless; the reverse
    (binding a name we do not allow) is what breaks."""
    return frozenset(
        {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    )


def loopback_origins(port: int) -> frozenset[str]:
    """Acceptable ``Origin`` header values (the http loopback origins)."""
    return frozenset(
        {f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}"}
    )


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive single-header lookup that works for a plain ``dict``
    (tests) and an ``email.message.Message`` (``http.server`` ``self.headers``)."""
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def authorize(
    *, path: str, headers: Mapping[str, str], token: str, port: int
) -> str | None:
    """Run the fail-closed request-auth chain. Return a denial REASON, or ``None``
    when the request may proceed. Order is Host -> token -> CSRF; any miss denies.

    Kept pure (header-dict in, reason-or-None out) so every denial variant is a
    fast unit test and the handler merely adapts ``self.headers``/``self.path``."""
    # 1. Host allowlist — applies to EVERY request (static + api).
    host = _header(headers, "Host")
    if host is None or host.lower() not in allowed_hosts(port):
        return "bad host"

    # Static assets stop here: navigations have Sec-Fetch-Site: none and no token.
    if not path.startswith(API_PREFIX):
        return None

    # 2. Per-launch token (Authorization: Bearer <token>), constant-time compared.
    auth = _header(headers, "Authorization")
    expected = f"Bearer {token}"
    if auth is None or not hmac.compare_digest(auth, expected):
        return "bad or missing token"

    # 3. CSRF: Sec-Fetch-Site (primary, JS-unforgeable) then Origin (fallback).
    sec_fetch = _header(headers, "Sec-Fetch-Site")
    if sec_fetch is not None:
        if sec_fetch != "same-origin":
            return "cross-origin request refused"
        return None
    origin = _header(headers, "Origin")
    if origin is not None and origin in loopback_origins(port):
        return None
    # Neither browser signal vouches for same-origin on a state-changing call.
    return "missing origin / sec-fetch-site"
