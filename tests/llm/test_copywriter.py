"""Unit 4: AI structural-copy generation (captions/FAQ/subheads/titles)."""

from __future__ import annotations

import types

import pytest

from lcp.adapters.llm import copywriter
from lcp.adapters.llm.client import LlmClient
from lcp.core.config import Config, LlmConfig
from lcp.core.draft import Draft

SECRET = "sk-test-key-1234567890"


def _choice(content, finish_reason="stop"):
    return types.SimpleNamespace(
        message=types.SimpleNamespace(content=content), finish_reason=finish_reason
    )


class _Stub:
    def __init__(self, content, finish_reason="stop"):
        self._resp = types.SimpleNamespace(choices=[_choice(content, finish_reason)])
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=lambda **k: self._resp)
        )

    def factory(self, **kwargs):
        return self


def _config():
    return Config(llm=LlmConfig(
        base_url="https://llm.example.com/v1", model="m",
        allowed_hosts=["llm.example.com"],
    ))


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setenv("LCP_LLM_API_KEY", SECRET)
    import lcp.core.config as cfg
    monkeypatch.setattr(cfg, "KEYRING_SERVICE", "lcp-test-copywriter")
    return SECRET


_OUTPUT = (
    "SUBHEAD: 事件起因\n"
    "CAPTION: 现场画面显示当事人离开\n"
    "FAQ_Q: 这件事什么时候发生\n"
    "FAQ_A: 据报道发生在上周\n"
    "TITLE: 某事件最新进展整理\n"
    "garbage line without prefix\n"
    "FAQ_Q: 孤儿问题没有答案\n"
)


def test_generates_all_structural_pieces(with_key):
    client = LlmClient(_config(), client_factory=_Stub(_OUTPUT).factory)
    res = copywriter.generate_structural_copy("some source text", client)
    assert res.executed
    assert res.subheads == ["事件起因"]
    assert res.captions == ["现场画面显示当事人离开"]
    assert res.title_candidates == ["某事件最新进展整理"]
    assert len(res.faq) == 1  # orphan question dropped
    assert res.faq[0].question == "这件事什么时候发生"
    assert res.needs_human_review is True


def test_dry_run_spends_nothing(monkeypatch):
    # dry-run client returns executed=False with no API key needed
    client = LlmClient(_config(), dry_run=True, client_factory=_Stub("x").factory)
    res = copywriter.generate_structural_copy("src", client)
    assert res.executed is False
    assert res.captions == [] and res.faq == []
    assert res.review_reason == "not_executed:dry_run"


def test_truncated_completion_is_needs_revision(with_key):
    client = LlmClient(_config(), client_factory=_Stub("SUBHEAD: x", "length").factory)
    res = copywriter.generate_structural_copy("src", client)
    assert res.needs_revision is True


def test_apply_copy_to_draft_enriches_without_mutation():
    draft = Draft(title="t", intro="i", event_body="b")
    res = copywriter.CopyResult(
        captions=["cap1"], subheads=["sub1"], title_candidates=["c1"],
    )
    out = copywriter.apply_copy_to_draft(draft, res, asset_refs=["images/a.jpg"])
    assert out.image_sections[0].caption == "cap1"
    assert out.image_sections[0].asset_ref == "images/a.jpg"
    assert out.subheads == ["sub1"]
    assert out.title_candidates == ["c1"]
    assert out.needs_human_review is True
    # input not mutated
    assert draft.image_sections == []
