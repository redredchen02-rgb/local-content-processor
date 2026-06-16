"""OS hardening baseline (plan R44, POSIX target: macOS/Linux).

Call apply_hardening() once at startup BEFORE writing any file or spawning any
subprocess, so umask is inherited by children and core dumps are disabled."""

from __future__ import annotations

import logging
import os
import re

# Keys whose values must be masked in logs / audit.
_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|authorization|secret|token|password|bearer)", re.IGNORECASE
)
# Inline secret-ish assignments. Handles three delimiter shapes after the key:
#   - `key = v` / `key: v`            (=/: with optional surrounding ws)
#   - `"key": "v"` / {"api_key":"v"}  (JSON form: optional quote+ws before the
#                                       delimiter, optional opening quote on the value)
#   - `key v`                         (space-separated, e.g. Authorization Bearer abc /
#                                       "Incorrect API key provided: sk-...")
# Group 1 = the key + delimiter + any opening quote (kept verbatim); group 2 =
# the secret value (masked). The value swallows an optional Bearer/Basic prefix.
_SECRET_VALUE_RE = re.compile(
    r"((?:api[_-]?key|authorization|secret|token|password|bearer)"
    r"['\"]?\s*(?:[=:]\s*)?['\"]?)"
    r"((?:Bearer\s+|Basic\s+)?[^\s'\",;}]+)",
    re.IGNORECASE,
)

# Standalone high-entropy token shapes that must be masked even without a key
# label nearby (provider errors often echo just the token):
#   - OpenAI-style keys: sk-..., sk-proj-..., sk-ant-... (>=16 tail chars)
#   - JWT-ish: three base64url segments starting with the eyJ header
_STANDALONE_TOKEN_RES = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
)

_MASK = "***REDACTED***"


def redact(text: str) -> str:
    """Mask secret-looking values inside a free-form string.

    Two passes: (1) key-labelled values (api_key=…, "token": "…", Authorization
    Bearer …), (2) standalone high-entropy token shapes (sk-…, JWTs) that leak
    without a key label (e.g. provider 'Incorrect API key provided: sk-…')."""
    out = _SECRET_VALUE_RE.sub(lambda m: m.group(1) + _MASK, text)
    for pat in _STANDALONE_TOKEN_RES:
        out = pat.sub(_MASK, out)
    return out


class SecretRedactingFilter(logging.Filter):
    """Logging filter that masks secrets in the formatted message + args."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        record.msg = redact(msg)
        record.args = ()
        return True


def install_log_redaction() -> None:
    f = SecretRedactingFilter()
    root = logging.getLogger()
    root.addFilter(f)
    for h in root.handlers:
        h.addFilter(f)


def disable_core_dumps() -> None:
    """RLIMIT_CORE=0 so a crash dump cannot leak in-memory PII/secrets."""
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, ValueError, OSError):
        pass  # not available on this platform (e.g. Windows)


def set_restrictive_umask() -> None:
    """0o077 -> new files 0600, dirs 0700. Set before any write/spawn."""
    try:
        os.umask(0o077)
    except OSError:
        pass


def minimal_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """A scrubbed environment for subprocesses parsing untrusted media, so they
    cannot inherit secrets from the parent process env."""
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SystemRoot")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    if extra:
        env.update(extra)
    return env


def apply_hardening() -> None:
    set_restrictive_umask()
    disable_core_dumps()
    install_log_redaction()
