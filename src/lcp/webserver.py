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
import inspect
import json
import mimetypes
import secrets
import sys
import threading
import time
import webbrowser
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

from .core.errors import EXIT_INTERNAL, InputValidationError, LcpError

# The ONLY bind address. Never "" / 0.0.0.0 — that would expose the tool to the
# LAN and defeat every check below. A test asserts the bound socket is loopback.
SERVER_HOST = "127.0.0.1"

# A stable default so the Claude-in-Chrome URL is predictable; --port overrides.
DEFAULT_PORT = 8765

# Upper bound on a request body before we read it (defence-in-depth vs a
# slow-send / OOM on the loopback socket; real API bodies are a few KB).
MAX_REQUEST_BODY_SIZE = 16 * 1024 * 1024

# Path prefix whose requests carry the full token + CSRF chain (state-mutating
# and data-returning API calls). Static asset GETs get the Host check only.
API_PREFIX = "/api/"

# The web/ assets directory (the ONLY document root — data/jobs/ is never served).
WEB_DIR = (Path(__file__).resolve().parent / "web").resolve()

# The strict CSP, sent as a real response header (defence-in-depth with the
# <meta> in index.html). frame-ancestors blocks clickjacking of the now-framable
# mutating page; connect-src 'self' already permits the same-origin fetch proxy.
CSP = (
    "default-src 'none'; script-src 'self'; img-src 'self'; style-src 'self'; "
    "connect-src 'self'; object-src 'none'; frame-ancestors 'none'"
)

# The index.html placeholder the server replaces with the live per-launch token.
TOKEN_PLACEHOLDER = "__LCP_CSRF_TOKEN__"  # noqa: S105 - a sentinel, not a secret
# Notification enabled flag: replaced at page load from config.notification.enabled.
NOTIFICATION_PLACEHOLDER = "__LCP_NOTIFICATION_ENABLED__"  # noqa: S105 - a sentinel

# Synchronous long routes whose first arg is a job_id: a concurrent same-job call
# must not run two Stage-1/Stage-2 passes that race the caller-owned `.processing`
# marker. The async variants are guarded inside Api._run_bg instead (they finish on
# a background thread, so the seam cannot bracket their work). See the plan, Unit 2.
_INFLIGHT_GUARDED = frozenset({"process", "create_and_crawl", "ingest_dir"})


def allowed_hosts(port: int) -> frozenset[str]:
    """The exact, PORT-INCLUSIVE set of acceptable ``Host`` header values.

    Port inclusion matters: comparing the host without the port is the
    documented DNS-rebinding bypass (MLflow #22095). ``localhost`` and ``[::1]``
    are included for operator convenience even though we bind IPv4 127.0.0.1 —
    allowing a name that cannot reach the socket is harmless; the reverse
    (binding a name we do not allow) is what breaks."""
    return frozenset({f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"})


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


def authorize(*, path: str, headers: Mapping[str, str], token: str, port: int) -> str | None:
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


# --- dispatch: route + arity-guard + typed-error contract ------------------


def _error_dict_for(exc: LcpError) -> dict[str, Any]:
    """Map an LcpError to the same bridge-safe dict gui.Api produces (escaped
    message + typed exit_code). Imported lazily so this module stays importable
    without pulling gui's heavy deps until a server is actually built/served."""
    from .gui import _error_dict

    return _error_dict(exc)


def public_routes(api_cls: type) -> frozenset[str]:
    """The route table the server dispatches to: every PUBLIC ``Api`` method.

    Derived by introspection (no leading underscore, callable). A guard test
    asserts this equals an INDEPENDENT hand-maintained literal list, so adding a
    method without updating the list (or the reverse) fails the test rather than
    silently exposing/hiding a route (plan Finding 6)."""
    return frozenset(
        name
        for name in dir(api_cls)
        if not name.startswith("_") and callable(getattr(api_cls, name))
    )


def dispatch(api: Any, name: str, args: list[Any]) -> dict[str, Any]:
    """Call ``api.<name>(*args)`` and return its dict. Two guarantees:

    * **Arity guard** — ``inspect.signature().bind(*args)`` rejects a too-long /
      wrong-shape ``args`` as a typed ``InputValidationError`` (exit 2) before the
      call. (It cannot catch a too-SHORT array — that falls back to method
      defaults; safe because the frontend always sends the full positional vector.)
    * **Typed-error contract at the seam** — any ``LcpError`` (incl. the arity one)
      becomes ``_error_dict`` regardless of whether the method carries
      ``@bridge_safe``, so the typed ``exit_code`` survives for un-decorated methods
      (the two ``*_async`` kickoffs). A non-``LcpError`` is left to the handler's
      last-resort net.
    """
    method = getattr(api, name)
    try:
        try:
            inspect.signature(method).bind(*args)
        except TypeError as e:
            raise InputValidationError(f"bad arguments for {name}") from e
        result = method(*args)
        # Api methods always return a JSON-able dict; guard the contract.
        if not isinstance(result, dict):
            raise InputValidationError(f"{name} returned a non-dict")
        return cast("dict[str, Any]", result)
    except LcpError as e:
        return _error_dict_for(e)


# --- HTTP server ------------------------------------------------------------


class _Server(ThreadingHTTPServer):
    """A loopback-bound, threaded HTTP server holding the single shared ``Api``,
    the per-launch token, and the per-job in-flight registry. ``daemon_threads``
    keeps Ctrl-C from hanging on a live request thread."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], handler: type, *, api: Any, token: str):
        super().__init__(address, handler)
        self.api = api
        self.token = token
        # The ACTUAL bound port (0 -> ephemeral) drives the Host allowlist.
        self.public_port: int = self.server_address[1]
        self.routes: frozenset[str] = public_routes(type(api))
        # inflight registry is owned by Api so _run_bg (async) and the sync
        # handler share the same set. Expose via properties so all existing
        # server.inflight / server.inflight_lock references need no changes.

    @property
    def inflight(self) -> set[str]:
        return self.api.inflight  # type: ignore[no-any-return]

    @property
    def inflight_lock(self) -> threading.Lock:
        return self.api.inflight_lock  # type: ignore[no-any-return]

    @property
    def notification_enabled(self) -> bool:
        """Read notification.enabled from the api's config at request time.

        Rebuilt per page load (api._ctx() is cheap — just a config read) so a
        config edit + server restart reflects correctly without restarting the
        process. Falls back to False on any exception (fail-closed)."""
        try:
            return bool(self.api._ctx().config.notification.enabled)
        except Exception:  # noqa: BLE001 - fail-closed
            return False


def build_server(api: Any, *, token: str, port: int) -> _Server:
    """Construct (but do not start) the loopback server. Exposed so tests can run
    it on an ephemeral port in a thread without going through blocking ``serve``."""
    return _Server((SERVER_HOST, port), _Handler, api=api, token=token)


def _make_api(config_path: str | None) -> Any:
    """Build the shared ``Api`` with a CONCRETE config path.

    Defaulting ``None`` -> ``"config.yaml"`` is load-bearing: the Settings panel
    writes via ``Api._settings_path`` (which itself falls back to ``config.yaml``),
    but reads/readiness go through ``Api._ctx`` -> ``load_config(self._config_path)``.
    With ``_config_path`` left ``None``, ``_ctx`` loads DEFAULTS and never reads the
    file the panel just wrote — so saved base_url/model silently never take effect
    (the GUI reports "endpoint 缺" forever). Resolving to ``config.yaml`` makes the
    panel write and the rest read the SAME file (the old pywebview ``launch`` did
    this; the move to ``serve`` must preserve it)."""
    from .gui import Api

    return Api(config_path=config_path or "config.yaml")


class _Handler(BaseHTTPRequestHandler):
    # Quiet, body-free access log: the request LINE (method + path) never contains
    # the POST body, so a `save_settings` api_key cannot leak here.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib name
        sys.stderr.write(f"lcp webui: {self.command} {self.path.split('?', 1)[0]}\n")

    @property
    def _srv(self) -> _Server:
        return cast("_Server", self.server)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # On EVERY response: strict CSP, no framing, and no-store so the
        # token-bearing index.html never lands in the browser disk cache.
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj: dict[str, Any], code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _deny(self, code: int, reason: str) -> None:
        # Minimal, generic body — never the internal authorize reason or a stack.
        self._send_json({"error": reason}, code)

    def _authorized(self) -> bool:
        """Run the gate chain; on denial send a minimal 401 (token) / 403 (other)
        and return False. The exact internal reason is never sent to the client."""
        reason = authorize(
            path=self.path,
            headers=dict(self.headers.items()),
            token=self._srv.token,
            port=self._srv.public_port,
        )
        if reason is None:
            return True
        if "token" in reason:
            self._deny(401, "unauthorized")
        else:
            self._deny(403, "forbidden")
        return False

    # --- GET: static assets only ------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        if self.path.startswith(API_PREFIX):
            self._deny(405, "method not allowed")  # API is POST-only
            return
        if not self._authorized():
            return
        self._serve_static()

    def _serve_static(self) -> None:
        rel = self.path.split("?", 1)[0].lstrip("/") or "index.html"
        candidate = (WEB_DIR / rel).resolve()
        # Lock the document root to web/: reject traversal and anything outside it
        # (data/jobs/ PII is never served), and only existing files.
        if not candidate.is_relative_to(WEB_DIR) or not candidate.is_file():
            self._deny(404, "not found")
            return
        data = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if candidate.name == "index.html":
            # Inject the live per-launch token into the page's <meta>.
            data = data.replace(TOKEN_PLACEHOLDER.encode(), self._srv.token.encode())
            # Inject notification.enabled state into the page's <meta>.
            notif_val = b"true" if self._srv.notification_enabled else b"false"
            data = data.replace(NOTIFICATION_PLACEHOLDER.encode(), notif_val)
        self._send(200, data, content_type)

    # --- POST: /api/<method> dispatch -------------------------------------
    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        if not self.path.startswith(API_PREFIX):
            self._deny(405, "method not allowed")
            return
        if not self._authorized():
            return
        name = self.path[len(API_PREFIX) :].split("?", 1)[0]
        if name not in self._srv.routes:
            self._deny(404, "not found")
            return
        try:
            self._dispatch_guarded(name)
        except Exception:  # noqa: BLE001 - last-resort net: never leak a stack
            self._send_json({"error": "internal error", "exit_code": EXIT_INTERNAL})

    def _dispatch_guarded(self, name: str) -> None:
        # Parse {"args": [...]} — a malformed body is a typed input error, not a 500.
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            # Reject negative (int("-1") is truthy -> rfile.read(-1) reads to EOF)
            # and oversized bodies before reading them (slow-send / OOM defence;
            # API bodies are tiny — job ids, urls, settings).
            if length < 0 or length > MAX_REQUEST_BODY_SIZE:
                raise InputValidationError("request body too large or malformed")
            raw = self.rfile.read(length) if length else b""
            payload = json.loads(raw) if raw else {}
            args = payload.get("args", []) if isinstance(payload, dict) else None
            if not isinstance(args, list):
                raise InputValidationError('request body must be {"args": [...]}')
        except (ValueError, InputValidationError) as e:
            err = e if isinstance(e, LcpError) else InputValidationError("malformed request body")
            self._send_json(_error_dict_for(err))
            return

        api = self._srv.api
        # In-flight guard for synchronous long routes (Finding 3 + Finding A): one
        # Stage-1/Stage-2 pass per job_id at a time. Async variants self-guard in
        # _run_bg. job_id is args[0] by convention for these methods; coerce to str
        # so the registry key is stable (a JSON int 123 must not bypass a str "123").
        job_id = str(args[0]) if (name in _INFLIGHT_GUARDED and args) else None
        if job_id is not None:
            with self._srv.inflight_lock:
                if job_id in self._srv.inflight:
                    self._send_json({"job_id": job_id, "status": "running"})
                    return
                self._srv.inflight.add(job_id)
        try:
            self._send_json(dispatch(api, name, args))
        finally:
            if job_id is not None:
                with self._srv.inflight_lock:
                    self._srv.inflight.discard(job_id)


def serve(  # pragma: no cover - blocking, desktop/operator entry point
    config_path: str | None = None, *, port: int = DEFAULT_PORT, open_browser: bool = True
) -> None:
    """Build the shared ``Api``, start the loopback server, and run until Ctrl-C.

    The PRINTED ``http://127.0.0.1:PORT/`` URL is the guaranteed deliverable (R1) —
    ``webbrowser.open`` opens the OS *default* browser (possibly Safari, where
    Claude-in-Chrome cannot attach), so it is best-effort only. On Ctrl-C, wait for
    any in-flight job (a second Ctrl-C abandons it, leaving an `interrupted` job to
    re-process, exactly like a crash)."""
    api = _make_api(config_path)
    token = secrets.token_urlsafe(32)
    server = build_server(api, token=token, port=port)
    url = f"http://{SERVER_HOST}:{server.public_port}/"
    # flush=True: the URL is the guaranteed deliverable (R1); without it a piped/
    # redirected stdout buffers and the operator never sees where to point Chrome.
    print(f"lcp webui serving at {url}", flush=True)
    print(
        "open this URL in Chrome to drive/debug with Claude in Chrome; Ctrl-C to stop",
        flush=True,
    )
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        with server.inflight_lock:
            running = len(server.inflight)
        if running:
            print(f"\n{running} job(s) in flight — waiting; press Ctrl-C again to abandon")
            try:
                while True:
                    with server.inflight_lock:
                        if not server.inflight:
                            break
                    time.sleep(0.3)
            except KeyboardInterrupt:
                print("\nabandoning in-flight job(s); they will need re-processing")
    finally:
        server.server_close()
