"""Config loading: YAML file + env, with secrets sourced from the OS keyring.

api_key is NEVER read from the config file or committed — keyring first, then
the LCP_LLM_API_KEY env var as a dev fallback (plan R19, R39)."""

from __future__ import annotations

import os
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
