# Defending a browser-reachable localhost HTTP API

## Problem

When you expose a control API over a real HTTP socket on `127.0.0.1` so a browser
can reach it (e.g. moving an in-process desktop bridge to a webui you can debug in
Chrome), you re-open an attack surface the in-process bridge never had. Loopback
binding stops an *off-host* attacker, but **not**:

- **CSRF** — any website the operator visits can `fetch('http://127.0.0.1:PORT/api/...')`
  or auto-submit a form to your port.
- **DNS rebinding** — a remote page rebinds its domain to `127.0.0.1` and reaches
  your API. This is the 2025 MCP-SDK CVE class (CVE-2025-66414/66416) and MLflow
  CVE-2025-14279: `http.server` does **not** validate `Host` by default.
- **Other local processes** — anything on the box can `curl` the port and forge
  Host/Origin/Sec-Fetch headers (those are only trustworthy *from a browser*).

## Pattern: a fail-closed request-auth chain, run before any business logic

Rebuild the lost trust boundary as a short, ordered chain (cheapest/most-decisive
first, mirroring a fail-closed gate chain). Any miss denies; never fail open.

1. **Host header allowlist** — the ONLY defense against DNS rebinding (the browser
   keeps the attacker's domain in `Host`, so an exact-match allowlist rejects it).
   Compare with **exact, port-inclusive equality** against
   `{127.0.0.1:PORT, localhost:PORT, [::1]:PORT}` — never `startswith`/substring
   (the MLflow #22095 bypass was comparing host without the port). Applies to
   **every** request, static included.
2. **Per-launch random token** (`secrets.token_urlsafe(32)`, `hmac.compare_digest`)
   — the ONLY thing that stops *other local processes* (they can forge the
   browser-only headers). Mint it fresh each launch; deliver it **only** by
   injecting it into the served page's `<meta>` (read by external JS, CSP-safe);
   **never** a URL query, a log, or a file. Sent as `Authorization: Bearer` — a
   custom header, which also forces a CORS preflight that blocks cross-site callers.
3. **Origin / Sec-Fetch-Site** — anti cross-site CSRF. `Sec-Fetch-Site` is
   browser-set and JS-unforgeable: require `same-origin`. Fall back to an `Origin`
   loopback allowlist for older clients; if **neither** is present on a
   state-changing request, **fail closed**. Don't trust `Referer`.

Token + CSRF apply to `/api/*` only; static navigations legitimately have
`Sec-Fetch-Site: none` and no token, so they get the Host check alone.

## Supporting invariants

- **Bind `127.0.0.1` explicitly** — never `""`/`0.0.0.0` (`http.server` examples
  often default to all interfaces, which turns the tool into a LAN service). Assert
  the bound socket is loopback in a test.
- **Lock the document root** to the asset dir; never serve data/PII dirs; reject
  path traversal.
- **Send CSP as a real response header** (defence-in-depth with the page's `<meta>`),
  add `frame-ancestors 'none'` + `X-Frame-Options: DENY` (clickjacking), and
  `Cache-Control: no-store` on token-bearing HTML (keep the token out of the browser
  disk cache). Keep `connect-src 'self'` — the same-origin fetch already works; never
  widen it.
- **Never leak a stack/secret**: no traceback in a response body; log method + path +
  status only, never the request body (it may carry an API key).

## Why these and not "loopback is enough"

Loopback binding is necessary but not sufficient once a browser is in the loop. Each
layer covers a distinct attacker the others miss: Host → remote rebinding; token →
local processes; Origin/Sec-Fetch → cross-site tabs. The result is a trust boundary
*equivalent to the old in-process bridge for the network property only* — it is
strictly **not** equivalent overall (a socket + an always-on DevTools surface + a
token whose confidentiality now depends on the page's CSP/XSS defence are all new).
Say so honestly; don't let a future change assume a safety margin that isn't there.

## Bypasses to avoid (each has bitten a real project)

- Comparing `Host` without the port, or with `startswith`/regex → rebinding passes.
- Trusting `Sec-Fetch-Site` as the *sole* defense → a non-browser local process
  forges it; you still need the token.
- Failing **open** when Origin/Sec-Fetch are absent → fail closed instead.
- Token in a URL/log/world-readable file → equivalent to no token.
- `==` on the token → timing side-channel; use `hmac.compare_digest`.
- Binding `0.0.0.0` "for convenience" → it's a network service now.

## Testing

- **Pure** negative tests for the chain (synthetic header dicts, one per bad/missing
  header) — fast, exhaustive, fail-closed by construction.
- **One real-socket integration test** (bind `127.0.0.1:0`, real HTTP client): the
  JSON↔language-runtime wire is a producer↔consumer seam that mocked unit tests
  can't prove. Assert the round-trip equals a direct call, plus each security
  variant rejects.

## See also

- `docs/solutions/fail-closed-catch-at-gate-boundary.md` — the same fail-closed,
  narrow-`except` discipline this chain follows.
- `docs/solutions/unit-tests-mask-integration-bugs.md` — why the real-socket test is
  mandatory, not optional.
