"""Config schema (pure core): pydantic models + the LLM base_url validator.

No I/O lives here — loading, keyring secrets, and atomic settings writes are in
`adapters/storage/config_io.py`. This module is pure business schema + the
SSRF-shape `validate_llm_base_url` judgement, so it stays inside the functional
core's "no I/O" redline.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .errors import InputValidationError


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
    # Acceptance band forwarded to asset_rules.judge_video. Defaults mirror the
    # rule's own DEFAULT_MIN/MAX_VIDEO_FPS so behavior is unchanged until tuned;
    # previously the gate ignored config and always used those rule defaults.
    min_video_fps: float = 24.0
    max_video_fps: float = 61.0
    min_video_bitrate_mbps: float = 1.5
    max_video_size_mb: int = 500  # MiB (1024-based); a larger video parks


class WatermarkConfig(BaseModel):
    """Official-watermark ADD settings (plan Unit 1). A brand mark, not a claim
    of authorship; never persists a secret. Logo assets are pre-sized per
    surface (body ~800px vs cover 1300x640)."""

    enabled: bool = False
    mode: str = "text"  # "logo" | "text"
    text: str = ""  # text mode: the mark string
    logo_body_path: str | None = None  # logo mode: pre-sized body asset
    logo_cover_path: str | None = None  # logo mode: pre-sized cover asset
    font_path: str | None = None  # text mode: truetype; None -> Pillow default
    font_size: int = 28
    position: str = "bottom-right"  # top/bottom-left/right | center
    opacity: float = 0.6  # 0..1, clamped
    margin: int = 16
    color: tuple[int, int, int] = (255, 255, 255)


class ContentConfig(BaseModel):
    title_min_chars: int = 25
    title_max_chars: int = 35
    tag_min_count: int = 3
    tag_max_count: int = 5
    uncertainty_terms: list[str] = Field(default_factory=lambda: ["網傳", "疑似", "被曝", "據傳"])
    # Lint tunables projected into LintConfig by build_lint_config. An empty list
    # / 0 means "use the rule's calibrated default" (DEFAULT_HYPE_WORDS /
    # DEFAULT_MIN_COPY_CHARS), so behavior is unchanged until an operator sets
    # them. hype_words: clickbait/hype tags the linter rejects; min_copy_chars:
    # the paragraph-length floor for the copied-too-much (plagiarism) check.
    hype_words: list[str] = Field(default_factory=list)
    min_copy_chars: int = 0
    # Field-level lint tunables (Unit 1). 0 = unset → use LintConfig default.
    intro_min_chars: int = 0
    intro_max_chars: int = 0
    event_body_min_chars: int = 0
    event_body_max_chars: int = 0
    summary_warn_chars: int = 0
    summary_error_chars: int = 0
    faq_min_count: int = 0
    faq_max_count: int = 0
    quick_facts_min_count: int = 0
    quick_facts_max_count: int = 0


class LlmConfig(BaseModel):
    """base_url + api_key (keyring). Non-loopback hosts require https (R40)."""

    base_url: str = ""
    keyring_username: str = "llm"
    model: str = ""
    timeout_seconds: int = 30
    max_retries: int = 1
    allowed_hosts: list[str] = Field(default_factory=list)
    # R40 escape hatch, config-driven (defaults keep https-only, public-CA).
    # ca_bundle: path to a private-CA bundle (extra trusted roots). This still
    # verifies certs fully — there is NO verify=False path anywhere.
    ca_bundle: str | None = None
    # allow_http_hosts: explicit loopback/private hosts permitted over plain
    # http (an internal endpoint), never the public internet.
    allow_http_hosts: list[str] = Field(default_factory=list)


class PublisherConfig(BaseModel):
    publish_enabled_by_default: bool = False
    require_human_approval: bool = True


class NotificationConfig(BaseModel):
    """Telegram group notification settings (SOP U3).

    enabled=False by default — operator must explicitly configure and enable.
    Bot token is a credential: stored in OS keyring only, never in this file.
    chat_id is a public channel/group ID (not a secret); safe in config.yaml."""

    enabled: bool = False
    telegram_chat_id: str = ""


class Config(BaseModel):
    storage: StorageConfig = Field(default_factory=StorageConfig)
    crawler: CrawlerConfig = Field(default_factory=CrawlerConfig)
    media: MediaConfig = Field(default_factory=MediaConfig)
    watermark: WatermarkConfig = Field(default_factory=WatermarkConfig)
    content: ContentConfig = Field(default_factory=ContentConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    publisher: PublisherConfig = Field(default_factory=PublisherConfig)
    notification: NotificationConfig = Field(default_factory=NotificationConfig)
    categories: dict[str, list[str]] = Field(default_factory=dict)
    # Per-栏目 operator prompt templates (plan Unit 3). A checked object: each is
    # linted (template_lint) and rendered into the DEVELOPER task slot via a
    # str.format_map allowlist — NEVER into the hardcoded SYSTEM message.
    templates: dict[str, str] = Field(default_factory=dict)


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
        raise InputValidationError(f"base_url scheme must be http/https (got {scheme!r})")
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
                f"plain http is only allowed for a loopback endpoint (got {host!r}); use https"
            )
    return host
