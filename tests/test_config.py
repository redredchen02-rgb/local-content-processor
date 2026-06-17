"""Pure config schema (no I/O — that lives in test_config_io.py)."""

from lcp.core.config import Config


def test_config_defaults_are_pure_schema():
    """Config() yields the documented defaults without touching disk/env."""
    cfg = Config()
    assert cfg.media.image_width == 800
    assert cfg.media.cover_width == 1300 and cfg.media.cover_height == 640
    assert cfg.publisher.require_human_approval is True
    assert cfg.llm.keyring_username == "llm"
    assert cfg.crawler.respect_robots_txt is True
