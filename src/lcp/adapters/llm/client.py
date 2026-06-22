"""OpenAI-compatible LLM client wrapper (Unit 7a).

SECURITY POSTURE — this client is built so the LLM has ZERO capability beyond a
single Chat Completions call returning text (lethal-trifecta defence, plan 紅線
1&3). It never exposes tools, never follows links, never writes. The ONLY
network it does is `chat.completions.create`.

R40 transport rules enforced here:
- Non-loopback / non-private hosts MUST use https. Plain http is permitted ONLY
  for a host explicitly allowlisted by the caller AND that resolves as a
  loopback/private literal (an internal endpoint), never the public internet.
- The base_url host must be in `config.llm.allowed_hosts`.
- We NEVER disable certificate verification (no verify=False anywhere). The
  private-CA escape hatch supplies a CA *bundle* (a file of trusted roots) via
  an httpx client with `verify=<path>` — this still verifies, it just trusts an
  extra root. There is no code path that turns verification off.

Failure mapping (plan R34):
- missing api_key / missing base_url  -> DependencyError (exit 3)
- timeout / rate-limit / 5xx / connection -> ExternalServiceError (exit 4)
- finish_reason != "stop", or empty/None content -> NOT an error: returns a
  ChatResult with needs_revision=True and a specific reason, so the job goes to
  NEEDS_REVISION (truncated/empty) rather than failing the run.

Secrets: the api_key is read via config_io.resolve_api_key() (keyring/env) and is
never logged, never put in an exception message, never echoed in headers. All log
/ error text is passed through runtime_hardening.redact()."""

from __future__ import annotations

import ipaddress
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlsplit

from ...core.config import Config
from ...core.errors import DependencyError, ExternalServiceError, InputValidationError
from ...runtime_hardening import redact
from ..storage.config_io import resolve_api_key

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)

# Only finish_reason == "stop" is a clean completion. Everything else (length,
# content_filter, tool_calls, function_call, or an unknown value) means the text
# is truncated/filtered/unexpected and must go to human revision.
CLEAN_FINISH_REASON = "stop"

# Temperature ceiling: constrained rewrite must stay low-variance (plan 0–0.3).
MAX_TEMPERATURE = 0.3

# Per-process cooldown (Unit 14, R10). The OpenAI SDK already does backoff+jitter
# WITHIN a single call, but nothing stops a sustained-5xx provider from being
# re-hammered on every job re-run. After this many CONSECUTIVE
# ExternalServiceErrors we stop making new calls for COOLDOWN_SECONDS and raise
# immediately instead — a counter + last-failure window, deliberately NOT a full
# closed/open/half-open circuit breaker (over-machinery for one in-process
# consumer). A single transient failure that then succeeds never trips it: the
# counter resets on any success.
#
# WORST-CASE STALL: each real call can itself block up to
# (max_retries + 1) × timeout_seconds while the SDK retries. Operators should NOT
# set both `llm.max_retries` and `llm.timeout_seconds` high, or a single
# pre-cooldown call can stall for that whole product before this cooldown even
# has a chance to engage.
COOLDOWN_FAILURE_THRESHOLD = 3
COOLDOWN_SECONDS = 60.0


@dataclass(frozen=True)
class ChatResult:
    """Outcome of one chat call (or a dry-run stub).

    `needs_revision` + `revision_reason` carry the finish_reason gate result so
    the assembler/job can route to NEEDS_REVISION without re-parsing the SDK
    response. `executed=False` marks a dry-run (no API call, no tokens)."""

    text: str
    finish_reason: str | None
    model: str
    needs_revision: bool = False
    revision_reason: str | None = None
    executed: bool = True


def _is_loopback_or_private(host: str) -> bool:
    """True iff `host` is a literal loopback/private/link-local IP, or the
    'localhost' name. A real hostname (needs DNS) is treated as public here:
    plain http is only ever allowed for an explicit internal literal."""
    h = host.strip().strip("[]")
    if h.lower() == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(h)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_unspecified


def _validate_base_url(
    base_url: str,
    allowed_hosts: list[str],
    allow_http_hosts: frozenset[str],
) -> tuple[str, str]:
    """Validate base_url for transport safety (R40). Returns (scheme, host).

    Raises InputValidationError on any violation. NEVER relaxes cert
    verification — that is simply not an option exposed anywhere."""
    if not base_url:
        # Caller treats this as a dependency problem (handled before us), but be
        # defensive.
        raise InputValidationError("LLM base_url is empty")

    parts = urlsplit(base_url)
    scheme = parts.scheme.lower()
    host = parts.hostname
    if scheme not in ("http", "https"):
        raise InputValidationError(f"LLM base_url scheme not allowed: {scheme!r} (http/https only)")
    if not host:
        raise InputValidationError(f"LLM base_url has no host: {base_url!r}")

    # Host allowlist (R40): the endpoint host must be explicitly configured.
    if host not in allowed_hosts:
        raise InputValidationError(
            f"LLM base_url host {host!r} not in allowed_hosts "
            f"{sorted(allowed_hosts)!r} (R40 host allowlist)"
        )

    # base_url must include the /v1 prefix (openai SDK v2 expectation). Checked
    # here (the single base_url validator) rather than separately in the caller.
    if not base_url.rstrip("/").endswith("/v1"):
        raise InputValidationError(
            "LLM base_url must end with '/v1' "
            f"(got {base_url!r}); the openai SDK appends paths to it"
        )

    if scheme == "http":
        # Plain http only for an explicitly allowlisted loopback/private host.
        if host not in allow_http_hosts:
            raise InputValidationError(
                f"refusing plain-http LLM base_url for {host!r}: https is "
                "required for non-loopback hosts (R40); add the host to the "
                "explicit http allowlist only if it is an internal endpoint"
            )
        if not _is_loopback_or_private(host):
            raise InputValidationError(
                f"refusing plain-http for non-loopback/non-private host "
                f"{host!r} (R40): https is mandatory on the public internet"
            )
    return scheme, host


def _build_http_client(ca_bundle: str | None) -> "httpx.Client | None":
    """Build an httpx client that trusts an extra CA bundle, WITHOUT disabling
    verification. Returns None when no bundle is supplied (SDK uses its default
    verifying client).

    We pass a real, hostname-checking SSLContext built from the bundle path
    (ssl.create_default_context). This is the private-CA escape hatch: it STILL
    performs full chain + hostname verification, it just trusts the supplied
    root. There is deliberately no code path that produces verify=False / an
    unverified context."""
    if not ca_bundle:
        return None
    import ssl

    import httpx

    ctx = ssl.create_default_context(cafile=ca_bundle)
    return httpx.Client(verify=ctx)


class LlmClient:
    """Thin wrapper over an OpenAI-compatible Chat Completions endpoint.

    Constructed lazily: the underlying openai client is only built on the first
    real call (or never, in dry_run), so importing/instantiating this class
    spends no network and needs no api_key until you actually chat."""

    def __init__(
        self,
        config: Config,
        *,
        dry_run: bool = False,
        ca_bundle: str | None = None,
        allow_http_hosts: list[str] | None = None,
        client_factory: Any = None,
        monotonic: Callable[[], float] | None = None,
        cooldown_failure_threshold: int = COOLDOWN_FAILURE_THRESHOLD,
        cooldown_seconds: float = COOLDOWN_SECONDS,
    ) -> None:
        """`client_factory` is the seam tests use to inject a stub openai client
        — by default it is the real `openai.OpenAI`. `allow_http_hosts` is the
        explicit, opt-in set of internal hosts permitted to use plain http;
        empty by default so https is required everywhere unless deliberately
        relaxed for a vetted loopback/private endpoint.

        `monotonic` is the injected clock for the per-process cooldown (defaults
        to `time.monotonic`); tests pass a controllable one so the cooldown
        window can be driven without sleeping. The shell mints time; the
        cooldown bookkeeping below stays deterministic given the clock."""
        self._config = config
        self._dry_run = dry_run
        self._ca_bundle = ca_bundle
        self._allow_http_hosts = frozenset(allow_http_hosts or ())
        self._client_factory = client_factory
        self._client = None  # built lazily
        self._resolved_api_key: str | None = None  # for exact-string redaction
        # Per-process cooldown state (Unit 14): a consecutive-failure counter and
        # a monotonic deadline before which new calls short-circuit.
        self._monotonic = monotonic if monotonic is not None else time.monotonic
        self._cooldown_failure_threshold = cooldown_failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._consecutive_failures = 0
        self._cooldown_until: float | None = None

    @property
    def model(self) -> str:
        return self._config.llm.model

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client

        llm = self._config.llm
        base_url = llm.base_url
        if not base_url:
            # No endpoint configured -> a missing local dependency (exit 3).
            raise DependencyError(
                "LLM base_url not configured: set llm.base_url (must end with /v1) in config."
            )

        # Transport safety BEFORE we touch the key — fail fast, no secret needed.
        # (_validate_base_url also enforces the '/v1' suffix.)
        _validate_base_url(base_url, llm.allowed_hosts, self._allow_http_hosts)

        # Resolve the secret last (raises DependencyError if absent). Never log.
        api_key = resolve_api_key(self._config)
        # Remember it ONLY to redact the EXACT string from any error message a
        # provider echoes back (belt-and-suspenders over the generic redact()).
        self._resolved_api_key = api_key

        factory = self._client_factory
        if factory is None:
            from openai import OpenAI

            factory = OpenAI

        kwargs = dict(
            base_url=base_url,
            api_key=api_key,
            timeout=llm.timeout_seconds,
            max_retries=llm.max_retries,
        )
        http_client = _build_http_client(self._ca_bundle)
        if http_client is not None:
            kwargs["http_client"] = http_client

        self._client = factory(**kwargs)
        return self._client

    def chat(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> ChatResult:
        """One constrained Chat Completions call. Returns a ChatResult.

        In dry_run mode the API is NOT called and a marked stub is returned
        (no tokens spent). Lowest-common-denominator params only (model,
        messages, max_tokens, temperature) — we do NOT depend on
        response_format / tools / streaming."""
        if temperature < 0 or temperature > MAX_TEMPERATURE:
            raise InputValidationError(
                f"temperature {temperature} out of range [0, {MAX_TEMPERATURE}] "
                "for constrained rewrite"
            )

        if self._dry_run:
            return ChatResult(
                text="[dry-run] LLM not actually executed — no tokens spent.",
                finish_reason=None,
                model=self.model,
                needs_revision=False,
                revision_reason=None,
                executed=False,
            )

        # Cooldown gate (Unit 14): if a sustained outage tripped the cooldown and
        # the window has not elapsed, short-circuit WITHOUT touching the endpoint
        # — re-hammering a dead provider on every re-run buys nothing.
        self._raise_if_in_cooldown()

        client = self._ensure_client()
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        try:
            resp = client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as e:  # noqa: BLE001 - narrowed below
            exc = self._as_external_error(e)
            # Only a real provider/transport failure counts toward the cooldown;
            # our own DependencyError/InputValidationError (config/usage bugs) are
            # not transient outages and must not engage it.
            if isinstance(exc, ExternalServiceError):
                self._record_failure()
            raise exc

        # A clean return path is a success regardless of finish_reason: the
        # provider responded, so the consecutive-failure streak resets.
        self._record_success()
        return self._interpret(resp)

    def _raise_if_in_cooldown(self) -> None:
        """Raise ExternalServiceError immediately if we are inside the cooldown
        window, making NO client call. Cleared once the window elapses."""
        if self._cooldown_until is None:
            return
        if self._monotonic() < self._cooldown_until:
            raise ExternalServiceError(
                "LLM call skipped: in per-process cooldown after "
                f"{self._consecutive_failures} consecutive failures "
                "(provider repeatedly unavailable; not re-hitting the endpoint)"
            )
        # Window elapsed: clear it and allow the next call through (the streak
        # stays counted; the next failure can re-arm the cooldown immediately).
        self._cooldown_until = None

    def _record_failure(self) -> None:
        """Count one consecutive ExternalServiceError; arm the cooldown once the
        threshold is reached."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._cooldown_failure_threshold:
            self._cooldown_until = self._monotonic() + self._cooldown_seconds

    def _record_success(self) -> None:
        """A successful call resets the streak so one transient blip never trips
        the cooldown."""
        self._consecutive_failures = 0
        self._cooldown_until = None

    def _interpret(self, resp: Any) -> ChatResult:
        """Read finish_reason + content. Only 'stop' with non-empty content is
        clean; anything else -> needs_revision with a specific reason."""
        choices = getattr(resp, "choices", None) or []
        if not choices:
            return ChatResult(
                text="",
                finish_reason=None,
                model=self.model,
                needs_revision=True,
                revision_reason="empty",
            )
        choice = choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None) if message is not None else None
        text = content or ""

        if finish_reason != CLEAN_FINISH_REASON:
            # Distinguish a PROVIDER CONTENT BLOCK from a cut-off completion so a
            # reviewer can tell "the model was censored" from "the output ran out
            # of tokens" — they call for different operator action. Both still
            # route to NEEDS_REVISION; only the reason label differs.
            prefix = "filtered" if finish_reason == "content_filter" else "truncated"
            return ChatResult(
                text=text,
                finish_reason=finish_reason,
                model=self.model,
                needs_revision=True,
                revision_reason=f"{prefix}:{finish_reason}",
            )
        if not text.strip():
            return ChatResult(
                text="",
                finish_reason=finish_reason,
                model=self.model,
                needs_revision=True,
                revision_reason="empty",
            )
        return ChatResult(
            text=text,
            finish_reason=finish_reason,
            model=self.model,
            needs_revision=False,
            revision_reason=None,
        )

    def _as_external_error(self, e: Exception) -> Exception:
        """Map openai/network failures to ExternalServiceError (exit 4) with a
        redacted message. DependencyError/InputValidationError raised by us pass
        through unchanged."""
        if isinstance(e, (DependencyError, InputValidationError)):
            return e
        # Redact in case any provider error string echoes a header/key. First
        # mask the EXACT resolved api_key (a provider may echo it verbatim, e.g.
        # "Incorrect API key provided: sk-..."), THEN the generic shapes.
        raw = f"{type(e).__name__}: {e}"
        if self._resolved_api_key:
            raw = raw.replace(self._resolved_api_key, "***REDACTED***")
        safe = redact(raw)
        logger.warning("LLM call failed: %s", safe)
        return ExternalServiceError(f"LLM call failed ({safe})")
