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
from pathlib import Path
from typing import Any

import yaml

from ...core.config import Config
from ...core.errors import DependencyError, InputValidationError
from ._fs import atomic_write_0600 as _atomic_write_0600

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
    except Exception as e:  # noqa: BLE001 - pydantic validation boundary conversion
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
    except Exception:  # noqa: BLE001 - keyring fallback; env var is the backup
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
    except Exception as e:  # noqa: BLE001 - keyring backend boundary conversion
        raise DependencyError(
            "could not store api_key in the OS keyring "
            f"({type(e).__name__}); set the LCP_LLM_API_KEY env var instead."
        ) from e


_TG_BOT_USER = "tg_bot"


def resolve_tg_bot_token() -> str:
    """Resolve the Telegram bot token from keyring, then env. Never from file.

    Keyring: service=KEYRING_SERVICE, user='tg_bot'.
    Env fallback: LCP_TG_BOT_TOKEN (dev/CI convenience)."""
    try:
        import keyring

        secret = keyring.get_password(KEYRING_SERVICE, _TG_BOT_USER)
        if secret:
            return secret
    except Exception:  # noqa: BLE001 - keyring fallback; env var is the backup
        pass
    env = os.environ.get("LCP_TG_BOT_TOKEN")
    if env:
        return env
    raise DependencyError(
        "Telegram bot token not configured: set it in the OS keyring "
        f"(service={KEYRING_SERVICE!r}, user={_TG_BOT_USER!r}) "
        "or the LCP_TG_BOT_TOKEN env var."
    )


def has_tg_bot_token() -> bool:
    """True iff a TG bot token is resolvable — WITHOUT revealing it."""
    try:
        resolve_tg_bot_token()
        return True
    except DependencyError:
        return False


def set_tg_bot_token(secret: str) -> None:
    """Store the Telegram bot token in the OS keyring.

    The token is NEVER written to a config file. Raises InputValidationError
    on empty input; DependencyError if no keyring backend is usable."""
    if not secret or not secret.strip():
        raise InputValidationError("Telegram bot token is empty")
    try:
        import keyring

        keyring.set_password(KEYRING_SERVICE, _TG_BOT_USER, secret.strip())
    except Exception as e:  # noqa: BLE001 - keyring backend boundary conversion
        raise DependencyError(
            f"could not store Telegram bot token in the OS keyring ({type(e).__name__}); "
            "set the LCP_TG_BOT_TOKEN env var instead."
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
    _atomic_write_0600(p, text)
    return p


def find_config_example() -> Path:
    """Locate ``config.example.yaml``: prefer the CWD (the documented setup runs
    from the repo root), else the repo root relative to this installed package."""
    cwd = Path("config.example.yaml")
    if cwd.exists():
        return cwd
    return Path(__file__).resolve().parents[4] / "config.example.yaml"


def init_workspace(
    *,
    config_path: str | os.PathLike[str],
    example_path: str | os.PathLike[str],
    site_index_path: str | os.PathLike[str],
) -> dict[str, bool]:
    """Scaffold a runnable workspace (plan Unit 4 — fixes blocker B1).

    * Write ``config.yaml`` from the example if absent — mode **0600**, NEVER
      clobbering an existing config. ``config.example.yaml`` ships 0644, so a
      plain copy would leave a world-readable config holding base_url/hosts.
    * Seed an EMPTY ``site_index.jsonl`` if absent — an empty *existing* index
      counts as available (HIGH reliability), so a fresh clean job is judged
      UNIQUE rather than parked UNCERTAIN at the dedup honesty gate.

    Idempotent: returns ``{'config_created': bool, 'index_created': bool}``."""
    created = {"config_created": False, "index_created": False}
    cfg = Path(config_path)
    if not cfg.exists():
        example = Path(example_path)
        if not example.exists():
            raise InputValidationError(f"config example not found: {example}")
        _atomic_write_0600(cfg, example.read_text(encoding="utf-8"))
        created["config_created"] = True
    idx = Path(site_index_path)
    if not idx.exists():
        _atomic_write_0600(idx, "")
        created["index_created"] = True
    return created
