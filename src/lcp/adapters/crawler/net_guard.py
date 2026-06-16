"""SSRF + path-traversal guards (SECURITY CRITICAL — plan R40, OWASP SSRF).

ACTIVE defences (what is genuinely enforced today):
- Validate the DNS-RESOLVED IP's `ipaddress.ip_address(ip).is_global`, NOT a
  hostname string blacklist (a hostname blacklist is decoration). Applied at
  validate time to the top-level URL (crawl_runner preflight + subprocess) AND
  to every scraped media URL before download (scrapy_impl second-order guard).
- scheme allowlist (http/https only).
- decimal/octal/hex/IPv6-encoded internal IP literals are normalised through
  `ipaddress` and rejected by the same is_global check.
- For the Scrapy crawl path specifically, also: `allowed_domains`
  (OffsiteMiddleware drops off-allowlist requests) + `REDIRECT_ENABLED=False`
  (a 30x is not blindly followed).

HONEST RESIDUAL — pin-IP-at-connect is NOT wired for the Scrapy path
-------------------------------------------------------------------
`ValidatedTarget.pinned_ip` and :func:`revalidate_redirect` exist, but NOTHING
in the Scrapy path consumes them: Scrapy opens its OWN connection and RE-RESOLVES
DNS at connect time, so we cannot force it to connect to the literal IP we
validated. That means the DNS-rebinding / TOCTOU defence the pinned IP would give
is NOT currently active for Scrapy. An attacker who controls an allowlisted
domain's DNS could resolve it to a global IP at validate time and flip it to an
internal IP by connect time. This is a DOCUMENTED RESIDUAL RISK (recorded in
docs/security/pii-inventory.md), not a defence we pretend to have. Wiring a
pinned-IP custom Scrapy resolver into the subprocess is heavy and deferred.

`pinned_ip` / `revalidate_redirect` are therefore AVAILABLE FOR A FUTURE
pinned-connection transport (e.g. an httpx-based fetcher that connects to the
literal IP with a host-header override), but are NOT currently wired into the
live crawl path. Do not read their existence as an active rebinding defence.

Path guard: `safe_join(base, user_path)` = resolve() + is_relative_to(base);
rejects `..`, absolute escapes, and symlinks whose real target leaves base.

I/O note: DNS resolution is injected (`resolver=`) so the pure classification
logic (is_global, host parsing, path containment) is unit-testable without
network. The default resolver does real A+AAAA lookups via socket.getaddrinfo.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from ...core.errors import InputValidationError

ALLOWED_SCHEMES = frozenset({"http", "https"})

# A resolver maps a hostname to a list of literal IP strings (A + AAAA).
Resolver = Callable[[str], list[str]]


@dataclass(frozen=True)
class ValidatedTarget:
    """A network target that passed SSRF validation.

    `pinned_ip` is the literal IP a pinned-connection transport WOULD connect to
    (the rebinding defence). NOTE: the live Scrapy path does NOT consume it
    (Scrapy re-resolves DNS at connect time), so today `pinned_ip` is
    informational / available for a future pinned-connection transport, NOT an
    active rebinding defence (see module docstring + pii-inventory.md). `host` is
    the original hostname preserved for the Host header; `url` is the original."""

    url: str
    scheme: str
    host: str
    pinned_ip: str
    port: int | None


def default_resolver(host: str) -> list[str]:
    """Real A+AAAA resolution via getaddrinfo. Returns deduped IP strings."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise InputValidationError(f"DNS resolution failed for {host!r}: {e}") from e
    ips: list[str] = []
    for info in infos:
        ip = info[4][0]
        # Strip IPv6 scope id (e.g. fe80::1%en0).
        ip = ip.split("%", 1)[0]
        if ip not in ips:
            ips.append(ip)
    if not ips:
        raise InputValidationError(f"no addresses resolved for {host!r}")
    return ips


def _parse_octet(tok: str) -> int | None:
    """Parse one IPv4 octet token using inet_aton-style rules (0x = hex,
    leading 0 = octal, else decimal). Returns None if not purely numeric."""
    if tok == "":
        return None
    try:
        if tok.lower().startswith("0x"):
            return int(tok, 16)
        if len(tok) > 1 and tok.startswith("0"):
            return int(tok, 8)
        return int(tok, 10)
    except ValueError:
        return None


def _as_packed_ipv4(value: str) -> ipaddress.IPv4Address | None:
    """Catch permissive dotted/short IPv4 encodings that libc's inet_aton (and
    thus getaddrinfo) accepts but ipaddress rejects: dotted-octal, dotted-hex,
    and short forms like 127.1. Returns the equivalent IPv4Address, or None if
    `value` is not an all-numeric dotted/integer form (i.e. a real hostname)."""
    parts = value.split(".")
    if len(parts) > 4:
        return None
    nums: list[int] = []
    for tok in parts:
        n = _parse_octet(tok)
        if n is None:
            return None  # contains a non-numeric label -> hostname
        nums.append(n)
    # inet_aton: last part absorbs the remaining low-order bytes.
    if len(nums) == 0:
        return None
    leading, last = nums[:-1], nums[-1]
    if any(n < 0 or n > 0xFF for n in leading):
        return None
    max_last = 1 << (8 * (4 - len(leading)))
    if last < 0 or last >= max_last:
        return None
    packed = 0
    for n in leading:
        packed = (packed << 8) | n
    packed = (packed << (8 * (4 - len(leading)))) | last
    return ipaddress.IPv4Address(packed)


def _as_ip(value: str) -> ipaddress._BaseAddress | None:
    """Parse a host string as a literal IP across decimal/hex/octal/IPv6
    encodings. Returns None if it is a normal hostname (needs DNS)."""
    v = value.strip()
    # Bracketed IPv6 literal: [::1]
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1]
        v = v.split("%", 1)[0]
        try:
            return ipaddress.ip_address(v)
        except ValueError as e:
            raise InputValidationError(f"invalid IPv6 literal: {value!r}") from e
    # Plain dotted/colon literal that ipaddress accepts directly.
    try:
        return ipaddress.ip_address(v)
    except ValueError:
        pass
    # Bare integer (decimal/0x-hex/0-octal) encoding of an IPv4.
    try:
        as_int = int(v, 0)
    except ValueError:
        as_int = None
    if as_int is not None:
        if 0 <= as_int <= 0xFFFFFFFF:
            return ipaddress.ip_address(as_int)
        raise InputValidationError(f"out-of-range numeric host: {value!r}")
    # Permissive dotted forms (octal/hex/short) that libc would accept.
    return _as_packed_ipv4(v)


def assert_global_ip(ip: str) -> None:
    """Raise InputValidationError unless `ip` is a globally-routable address.

    `is_global` is False for loopback, private (RFC1918), link-local
    (incl. 169.254.169.254 cloud metadata), CGNAT (100.64/10), unspecified
    (0.0.0.0/::), unique-local (fc00::/7), etc. — exactly the SSRF-dangerous
    ranges."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError as e:
        raise InputValidationError(f"not an IP address: {ip!r}") from e
    # IPv4-mapped IPv6 (::ffff:127.0.0.1) must be judged on the embedded IPv4.
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if not addr.is_global:
        raise InputValidationError(
            f"refusing non-global IP {ip!r} (SSRF guard: loopback/private/"
            "link-local/metadata addresses are blocked)"
        )


def _validate(url: str, resolver: Resolver) -> ValidatedTarget:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise InputValidationError(
            f"scheme not allowed: {scheme!r} (only http/https)"
        )
    host = parts.hostname
    if not host:
        raise InputValidationError(f"URL has no host: {url!r}")
    port = parts.port

    literal = _as_ip(host)
    if literal is not None:
        # Literal IP host: classify directly, never DNS. Pin it as-is, but
        # normalise IPv4-mapped IPv6 to its dotted IPv4 for the pinned value.
        pinned = str(literal)
        if isinstance(literal, ipaddress.IPv6Address) and literal.ipv4_mapped:
            pinned = str(literal.ipv4_mapped)
        assert_global_ip(pinned)
        return ValidatedTarget(
            url=url, scheme=scheme, host=str(literal), pinned_ip=pinned, port=port
        )

    # Hostname: resolve, validate EVERY resolved IP, pin the first.
    ips = resolver(host)
    for ip in ips:
        assert_global_ip(ip)  # any non-global IP -> reject the whole host
    return ValidatedTarget(
        url=url, scheme=scheme, host=host, pinned_ip=ips[0], port=port
    )


def validate_url(
    url: str, *, resolver: Resolver | None = None
) -> ValidatedTarget:
    """Validate a URL for fetching. Raises InputValidationError on any SSRF
    risk. The ACTIVE defense is this validate-time check (scheme allowlist +
    per-resolved-IP is_global reject). The returned ValidatedTarget.pinned_ip is
    available for a future pinned-connection transport but is NOT consumed on the
    Scrapy path (which re-resolves DNS at connect time) — see the SSRF RESIDUAL
    note at the top of this module."""
    return _validate(url, resolver or default_resolver)


def revalidate_redirect(
    location: str, *, resolver: Resolver | None = None
) -> ValidatedTarget:
    """Re-validation hook for redirect targets (30x Location).

    AVAILABLE FOR A FUTURE pinned-connection transport — NOT currently wired into
    the Scrapy path, which instead closes redirects entirely via
    `REDIRECT_ENABLED=False` (so a 30x is never followed in the first place). If a
    future fetcher follows redirects itself, it MUST call this so a redirect to an
    internal IP is rejected just like the initial URL (plan R40 'close redirect
    following'). Kept + tested so the future wiring is a drop-in, not a rewrite."""
    return _validate(location, resolver or default_resolver)


# --------------------------------------------------------------------------
# path traversal
# --------------------------------------------------------------------------

def safe_join(base: str | os.PathLike[str], user_path: str | os.PathLike[str]) -> Path:
    """Join `user_path` under `base` safely, defeating path traversal.

    Uses Path.resolve() (which collapses `..` AND follows symlinks) then
    requires the result to be relative to base.resolve(). This rejects:
    - `..` escapes,
    - absolute-path escapes,
    - symlinks whose real target leaves base.
    Returns the resolved absolute Path inside base. Raises
    InputValidationError otherwise."""
    base_resolved = Path(base).resolve()
    candidate = (base_resolved / Path(user_path)).resolve()
    if candidate != base_resolved and not candidate.is_relative_to(base_resolved):
        raise InputValidationError(
            f"path escapes base directory: {user_path!r} not within {base!r}"
        )
    return candidate
