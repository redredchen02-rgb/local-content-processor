"""SECURITY-CRITICAL tests, written test-FIRST (plan execution note + R40).

SSRF defence is the highest-leverage net guard. We test:
- scheme allowlist (http/https only),
- DNS-resolved IP `is_global` classification (NOT a hostname string blacklist),
- pinned literal IP returned for the connection (defends DNS-rebinding/TOCTOU),
- decimal/octal/IPv6-encoded internal IPs rejected,
- redirect re-validation hook rejects redirects to non-global IPs,
- path-traversal: resolve()+is_relative_to, reject `..`, absolute escape, and
  symlinks pointing outside base.

These use an injectable resolver so we never hit real DNS in tests.
"""

from __future__ import annotations

import os

import pytest

from lcp.adapters.crawler import net_guard
from lcp.core.errors import InputValidationError


def _resolver(mapping):
    """Return a fake resolver: hostname -> list[str] of literal IPs."""

    def resolve(host: str) -> list[str]:
        if host not in mapping:
            raise InputValidationError(f"DNS failure for {host}")
        return list(mapping[host])

    return resolve


# --------------------------------------------------------------------------
# scheme allowlist
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "ftp://example.com/x",
    "file:///etc/passwd",
    "gopher://example.com/x",
    "data:text/plain;base64,AAAA",
    "javascript:alert(1)",
    "://example.com",
    "example.com/no-scheme",
])
def test_non_http_schemes_rejected(url):
    with pytest.raises(InputValidationError):
        net_guard.validate_url(url, resolver=_resolver({"example.com": ["93.184.216.34"]}))


def test_http_and_https_allowed():
    r = _resolver({"example.com": ["93.184.216.34"]})
    for url in ("http://example.com/a", "https://example.com/a"):
        t = net_guard.validate_url(url, resolver=r)
        assert t.scheme in ("http", "https")


# --------------------------------------------------------------------------
# IP classification (pure)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("ip", [
    "127.0.0.1",         # loopback
    "10.0.0.5",          # private A
    "172.16.0.1",        # private B
    "192.168.1.1",       # private C
    "169.254.169.254",   # link-local / cloud metadata
    "0.0.0.0",           # this-host / unspecified
    "100.64.0.1",        # CGNAT
    "::1",               # IPv6 loopback
    "fe80::1",           # IPv6 link-local
    "fc00::1",           # IPv6 unique-local
])
def test_non_global_ips_rejected(ip):
    with pytest.raises(InputValidationError):
        net_guard.assert_global_ip(ip)


@pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8", "2606:2800:220:1:248:1893:25c8:1946"])
def test_global_ips_accepted(ip):
    net_guard.assert_global_ip(ip)  # no raise


# --------------------------------------------------------------------------
# SSRF via DNS resolution to internal IPs
# --------------------------------------------------------------------------

@pytest.mark.parametrize("ip", [
    "127.0.0.1",
    "10.1.2.3",
    "172.16.5.5",
    "192.168.0.1",
    "169.254.169.254",
    "::1",
])
def test_url_resolving_to_internal_ip_rejected(ip):
    r = _resolver({"evil.example": [ip]})
    with pytest.raises(InputValidationError):
        net_guard.validate_url("http://evil.example/x", resolver=r)


def test_url_with_any_internal_resolved_ip_rejected():
    # Even ONE internal IP among the A/AAAA set must reject (multi-record SSRF).
    r = _resolver({"mixed.example": ["93.184.216.34", "10.0.0.1"]})
    with pytest.raises(InputValidationError):
        net_guard.validate_url("http://mixed.example/x", resolver=r)


# --------------------------------------------------------------------------
# decimal / octal / IPv6-encoded internal IPs in the URL host itself
# --------------------------------------------------------------------------

@pytest.mark.parametrize("host", [
    "2130706433",        # decimal 127.0.0.1
    "0x7f000001",        # hex 127.0.0.1
    "0177.0.0.1",        # dotted-octal 127.0.0.1
    "0x7f.0.0.1",        # dotted-hex 127.0.0.1
    "127.1",             # short-form 127.0.0.1
    "[::1]",             # IPv6 loopback literal
    "[::ffff:127.0.0.1]",  # IPv4-mapped loopback
])
def test_encoded_internal_ip_literals_rejected(host):
    # Literal-IP hosts must be classified directly (no DNS), and internal ones
    # rejected regardless of encoding.
    r = _resolver({})  # must NOT be consulted for literal IPs
    with pytest.raises(InputValidationError):
        net_guard.validate_url(f"http://{host}/x", resolver=r)


def test_nip_io_style_internal_resolution_rejected():
    # *.nip.io resolves the embedded IP; here it would resolve to 10.0.0.1.
    r = _resolver({"10.0.0.1.nip.io": ["10.0.0.1"]})
    with pytest.raises(InputValidationError):
        net_guard.validate_url("http://10.0.0.1.nip.io/x", resolver=r)


# --------------------------------------------------------------------------
# pinned IP defends rebinding / TOCTOU
# --------------------------------------------------------------------------

def test_validated_target_pins_literal_ip_and_keeps_hostname():
    r = _resolver({"example.com": ["93.184.216.34"]})
    t = net_guard.validate_url("http://example.com/path?q=1", resolver=r)
    # The connection IP is pinned to the validated literal address...
    assert t.pinned_ip == "93.184.216.34"
    # ...while the original hostname is preserved for the Host header.
    assert t.host == "example.com"
    assert t.scheme == "http"


def test_literal_global_ip_url_pins_itself():
    r = _resolver({})
    t = net_guard.validate_url("https://93.184.216.34/x", resolver=r)
    assert t.pinned_ip == "93.184.216.34"
    assert t.host == "93.184.216.34"


# --------------------------------------------------------------------------
# redirect re-validation hook
# --------------------------------------------------------------------------

def test_redirect_to_internal_ip_rejected():
    r = _resolver({"intranet.example": ["10.0.0.7"]})
    with pytest.raises(InputValidationError):
        net_guard.revalidate_redirect("http://intranet.example/admin", resolver=r)


def test_redirect_to_global_ip_allowed():
    r = _resolver({"example.com": ["93.184.216.34"]})
    t = net_guard.revalidate_redirect("https://example.com/ok", resolver=r)
    assert t.pinned_ip == "93.184.216.34"


# --------------------------------------------------------------------------
# the REAL, ACTIVE defence: validate_url rejects internal IPs at validate time
# (pinned-IP-at-connect is a documented residual; this is what actually works)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url,resolved", [
    ("http://169.254.169.254/latest/meta-data/", None),  # literal metadata IP
    ("http://127.0.0.1/secret", None),                   # literal loopback
    ("http://10.0.0.1/internal", None),                  # literal private
    ("http://intranet.example/admin", "10.0.0.7"),       # resolves internal
])
def test_validate_url_rejects_internal_targets(url, resolved):
    """The active SSRF defence — reject any target whose (literal or resolved) IP
    is non-global — must hold regardless of the unwired pinned-IP residual."""
    mapping = {} if resolved is None else {"intranet.example": [resolved]}
    with pytest.raises(InputValidationError):
        net_guard.validate_url(url, resolver=_resolver(mapping))


# --------------------------------------------------------------------------
# path traversal: safe_join
# --------------------------------------------------------------------------

def test_safe_join_happy(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    (base / "sub").mkdir()
    p = net_guard.safe_join(base, "sub/file.txt")
    assert p == (base / "sub" / "file.txt").resolve()


@pytest.mark.parametrize("evil", [
    "../escape.txt",
    "../../etc/passwd",
    "sub/../../escape.txt",
    "/etc/passwd",            # absolute escape
    "/",
])
def test_safe_join_rejects_traversal(tmp_path, evil):
    base = tmp_path / "base"
    base.mkdir()
    with pytest.raises(InputValidationError):
        net_guard.safe_join(base, evil)


def test_safe_join_rejects_symlink_escape(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    # symlink inside base pointing outside base
    link = base / "link"
    os.symlink(outside, link)
    with pytest.raises(InputValidationError):
        net_guard.safe_join(base, "link/secret.txt")


def test_safe_join_allows_symlink_inside_base(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    (base / "real").mkdir()
    (base / "real" / "f.txt").write_text("ok")
    os.symlink(base / "real", base / "link")
    p = net_guard.safe_join(base, "link/f.txt")
    assert p.read_text() == "ok"
