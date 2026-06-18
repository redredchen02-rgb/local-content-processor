"""Config I/O adapter: YAML load + operator settings writes + keyring secrets.

The imperative shell for `core/config.py` (which stays pure: pydantic models +
`validate_llm_base_url`). All disk/env/keyring access lives here so the core
honors its "no I/O" redline.

api_key is NEVER read from the config file or committed — keyring first, then the
LCP_LLM_API_KEY env var as a dev fallback (plan R19, R39). `resolve_api_key`
returns the secret so the LlmClient can bind it for exact-string redaction.

There is intentionally NO module-level I/O here: importing this module must not
read files, the keyring, or env (so an early import can never run before
`apply_hardening()` sets the umask). All access is inside functions.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from ...core.config import Config
from ...core.errors import DependencyError, InputValidationError

KEYRING_SERVICE = "local-content-processor"


def load_config(path: str | os.PathLike[str] | None) -> Config:
    """Load config from a YAML path. Missing path -> defaults. Unknown/invalid
    fields raise InputValidationError (exit 2)."""
    if path is None:
        return Config()
    p = Path(path)
    if not p.exists():
        raise InputValidationError(f"config file not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise InputValidationError(f"config YAML parse error: {e}") from e
    if not isinstance(raw, dict):
        raise InputValidationError("config root must be a mapping")
    try:
        return Config.model_validate(raw)
    except Exception as e:
        raise InputValidationError(f"config validation error: {e}") from e


def resolve_api_key(config: Config) -> str:
    """Resolve the LLM api_key from keyring, then env. Never from file.

    Returns the secret so the caller (LlmClient) can bind it for exact-string
    redaction; the DependencyError message deliberately omits any backend error
    body so a secret echoed in it can never leak."""
    try:
        import keyring

        secret = keyring.get_password(KEYRING_SERVICE, config.llm.keyring_username)
        if secret:
            return secret
    except Exception:
        pass
    env = os.environ.get("LCP_LLM_API_KEY")
    if env:
        return env
    raise DependencyError(
        "LLM api_key not configured: set it in the OS keyring "
        f"(service={KEYRING_SERVICE!r}, user={config.llm.keyring_username!r}) "
        "or the LCP_LLM_API_KEY env var."
    )


def has_api_key(config: Config) -> bool:
    """True iff an api_key is resolvable (keyring or env) — WITHOUT revealing it.
    For UI status only; never returns or logs the secret itself."""
    try:
        resolve_api_key(config)
        return True
    except DependencyError:
        return False


# --------------------------------------------------------------------------
# Operator-driven settings writes (used by the GUI/CLI settings panel)
#
# RULE: base_url + model + allowed_hosts go to the YAML config file; the api_key
# goes ONLY to the OS keyring. NOTHING here ever writes the api_key to a file.
# --------------------------------------------------------------------------


def set_llm_api_key(secret: str, *, username: str = "llm") -> None:
    """Store the LLM api_key in the OS keyring (service KEYRING_SERVICE).

    The key is NEVER written to a config file (plan R19/R39). Raises
    InputValidationError on an empty secret; DependencyError if no keyring
    backend is usable (the message deliberately omits the backend error body so a
    secret echoed in it can never leak)."""
    if not secret or not secret.strip():
        raise InputValidationError("api_key is empty")
    try:
        import keyring

        keyring.set_password(KEYRING_SERVICE, username, secret.strip())
    except Exception as e:
        raise DependencyError(
            "could not store api_key in the OS keyring "
            f"({type(e).__name__}); set the LCP_LLM_API_KEY env var instead."
        ) from e


def _merge_list(llm: dict[str, Any], field: str, value: str) -> None:
    """Append `value` to the list at llm[field] (creating it), no duplicates."""
    items = llm.get(field)
    if not isinstance(items, list):
        items = []
    if value not in items:
        items.append(value)
    llm[field] = items


def update_llm_config_file(
    path: str | os.PathLike[str],
    *,
    base_url: str | None = None,
    model: str | None = None,
    allowed_hosts_add: str | None = None,
    allow_http_hosts_add: str | None = None,
) -> Path:
    """Merge LLM settings into the YAML config at `path`, preserving every other
    section. Writes atomically (unique temp file + os.replace) with 0600 perms.
    NEVER writes an api_key — any stray ``llm.api_key`` key is stripped
    defensively. `allow_http_hosts_add` opts a loopback host into plain-http at
    call time (only the GUI's loopback path passes it). Returns the written
    path."""
    p = Path(path)
    raw: dict[str, Any] = {}
    if p.exists():
        loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise InputValidationError(f"config root must be a mapping: {p}")
        raw = loaded

    llm = raw.get("llm")
    if not isinstance(llm, dict):
        llm = {}
        raw["llm"] = llm
    if base_url is not None:
        llm["base_url"] = base_url
    if model is not None:
        llm["model"] = model
    if allowed_hosts_add:
        _merge_list(llm, "allowed_hosts", allowed_hosts_add)
    if allow_http_hosts_add:
        _merge_list(llm, "allow_http_hosts", allow_http_hosts_add)
    # Belt-and-suspenders: a secret must never be persisted to the file.
    llm.pop("api_key", None)

    text = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Unique temp file (mkstemp creates it 0600, O_EXCL) so concurrent/aborted
    # writers never share or leave a torn `config.yaml.tmp`.
    fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, p)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return p
