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
# Inline secret-ish assignments, e.g. api_key=sk-123 / "token": "abc".
_SECRET_VALUE_RE = re.compile(
    r"((?:api[_-]?key|authorization|secret|token|password|bearer)"
    r"\s*[=:]\s*['\"]?)((?:Bearer\s+|Basic\s+)?[^\s'\",;]+)",
    re.IGNORECASE,
)

_MASK = "***REDACTED***"


def redact(text: str) -> str:
    """Mask secret-looking assignments inside a free-form string."""
    return _SECRET_VALUE_RE.sub(lambda m: m.group(1) + _MASK, text)


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
