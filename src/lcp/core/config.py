"""Config loading: YAML file + env, with secrets sourced from the OS keyring.

api_key is NEVER read from the config file or committed — keyring first, then
the LCP_LLM_API_KEY env var as a dev fallback (plan R19, R39)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .errors import DependencyError, InputValidationError

KEYRING_SERVICE = "local-content-processor"


class StorageConfig(BaseModel):
    base_dir: str = "./data"


class CrawlerConfig(BaseModel):
    allow_domains: list[str] = Field(default_factory=list)
    respect_robots_txt: bool = True
    rate_limit_seconds: float = 2.0
    timeout_seconds: int = 30
    max_assets_per_job: int = 100


class MediaConfig(BaseModel):
    image_width: int = 800
    image_quality: int = 90
    cover_width: int = 1300
    cover_height: int = 640
    video_codec: str = "h264"
    video_fps: int = 30
    min_video_bitrate_mbps: float = 1.5
    max_video_size_mb: int = 500


class ContentConfig(BaseModel):
    title_min_chars: int = 25
    title_max_chars: int = 35
    tag_min_count: int = 3
    tag_max_count: int = 5
    uncertainty_terms: list[str] = Field(
        default_factory=lambda: ["網傳", "疑似", "被曝", "據傳"]
    )


class LlmConfig(BaseModel):
    """base_url + api_key (keyring). Non-loopback hosts require https (R40)."""

    base_url: str = ""
    keyring_username: str = "llm"
    model: str = ""
    timeout_seconds: int = 60
    max_retries: int = 3
    allowed_hosts: list[str] = Field(default_factory=list)
    # R40 escape hatch, config-driven (defaults keep https-only, public-CA).
    # ca_bundle: path to a private-CA bundle (extra trusted roots). This still
    # verifies certs fully — there is NO verify=False path anywhere.
    ca_bundle: str | None = None
    # allow_http_hosts: explicit loopback/private hosts permitted over plain
    # http (an internal endpoint), never the public internet.
    allow_http_hosts: list[str] = Field(default_factory=list)


class PublisherConfig(BaseModel):
    reviewers: list[str] = Field(default_factory=list)
    publish_enabled_by_default: bool = False
    require_human_approval: bool = True


class Config(BaseModel):
    storage: StorageConfig = Field(default_factory=StorageConfig)
    crawler: CrawlerConfig = Field(default_factory=CrawlerConfig)
    media: MediaConfig = Field(default_factory=MediaConfig)
    content: ContentConfig = Field(default_factory=ContentConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    publisher: PublisherConfig = Field(default_factory=PublisherConfig)
    categories: dict[str, list[str]] = Field(default_factory=dict)

    def llm_api_key(self) -> str:
        """Resolve the LLM api_key from keyring, then env. Never from file."""
        try:
            import keyring

            secret = keyring.get_password(KEYRING_SERVICE, self.llm.keyring_username)
            if secret:
                return secret
        except Exception:
            pass
        env = os.environ.get("LCP_LLM_API_KEY")
        if env:
            return env
        raise DependencyError(
            "LLM api_key not configured: set it in the OS keyring "
            f"(service={KEYRING_SERVICE!r}, user={self.llm.keyring_username!r}) "
            "or the LCP_LLM_API_KEY env var."
        )

    def has_api_key(self) -> bool:
        """True iff an api_key is resolvable (keyring or env) — WITHOUT revealing
        it. For UI status only; never returns or logs the secret itself."""
        try:
            self.llm_api_key()
            return True
        except DependencyError:
            return False


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


def validate_llm_base_url(base_url: str) -> str:
    """Validate an LLM base_url's SHAPE and return its host (lowercased).

    Enforces http/https + a host + the trailing '/v1' the openai SDK needs, and
    only allows plain http for a LOOPBACK endpoint ('localhost' or a 127/::1
    literal — the local-LLM case). Plain http to a private (RFC1918) or
    link-local host is REFUSED: link-local 169.254.169.254 is the cloud
    instance-metadata SSRF target, and a private-range http endpoint must be
    opted in deliberately via config.llm.allow_http_hosts, never the GUI. Does
    NOT check the host allowlist (the caller adds the returned host to
    allowed_hosts); the LlmClient is the transport-enforcement point at call
    time."""
    import ipaddress
    from urllib.parse import urlsplit

    s = (base_url or "").strip()
    if not s:
        raise InputValidationError("base_url is empty")
    parts = urlsplit(s)
    scheme = parts.scheme.lower()
    host = parts.hostname
    if scheme not in ("http", "https"):
        raise InputValidationError(
            f"base_url scheme must be http/https (got {scheme!r})"
        )
    if not host:
        raise InputValidationError(f"base_url has no host: {base_url!r}")
    if not s.rstrip("/").endswith("/v1"):
        raise InputValidationError(
            "base_url must end with '/v1' (the openai SDK appends paths to it)"
        )
    if scheme == "http" and host.lower() != "localhost":
        try:
            addr = ipaddress.ip_address(host.strip("[]"))
            internal = addr.is_loopback  # loopback ONLY (not private/link-local)
        except ValueError:
            internal = False
        if not internal:
            raise InputValidationError(
                f"plain http is only allowed for a loopback endpoint "
                f"(got {host!r}); use https"
            )
    return host


def _merge_list(llm: dict, field: str, value: str) -> None:
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
    raw: dict = {}
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
