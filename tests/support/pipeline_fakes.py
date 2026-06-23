"""Reusable pipeline fakes for the durable e2e + live-LLM tests (plan D6).

The happy-path e2e must drive the REAL Stage-2 gate chain (risk -> media ->
dedup -> assemble -> copywriter -> lint -> ground) to PROCESSED with a
*substantive* draft — never via persist_gate_state. Only the two true externals
are faked: Stage 1 (a no-network fake crawler) and the LLM (a deterministic
dual-mode client that answers BOTH the assemble and the copywriter calls).

Fixture discipline (plan: grounding ⨯ copied-too-much): the SOURCE paragraphs
are all <40 chars, so verbatim grounding never trips the copied-too-much lint;
every generated claim is a verbatim source substring so grounding is
deterministic without an over-clean source.
"""

from __future__ import annotations

from pathlib import Path

from lcp import pipeline as pl
from lcp.adapters.crawler.base import STATUS_CRAWLED, RawJobBundle, SourceSpec
from lcp.adapters.crawler.bundle import build_manifest
from lcp.adapters.llm.client import ChatResult
from lcp.adapters.storage.audit_log import AuditLog
from lcp.adapters.storage.job_store import JobStore
from lcp.adapters.storage.manifest import write_manifest
from lcp.core.config import Config, ContentConfig
from lcp.core.models import SourceType

# Neutral, redline-free source; every paragraph is <40 chars so a verbatim
# grounded body never trips the copied-too-much lint (min_copy_chars=40).
SOURCE = (
    "華山文創園區本週末舉辦美食市集。\n"
    "現場有上百個攤位提供各式小吃與飲料。\n"
    "主辦單位預估將吸引大量人潮前往參觀。"
)

# A 25–35-char title (lint requires [25,35]); not grounded-checked, only length.
TITLE = "台北華山文創園區本週末舉辦大型美食市集盛大登場人潮擠爆"

# The assemble body: uses the INTRO:/EVENT: two-prefix protocol (Unit 2).
# Both values are verbatim source substrings (grounded); each <40 chars.
BODY = "INTRO: 華山文創園區本週末舉辦美食市集。\nEVENT: 現場有上百個攤位提供各式小吃與飲料。"

# The copywriter structural-copy payload (line-prefix protocol). Every claim is
# a verbatim source substring so grounding passes deterministically; tags are
# plain objective words (no hype); no CAPTION -> image_sections stays empty,
# which is fine for a text-only bundle (image_sections is conditional, D9).
COPY = (
    "QUICKFACT: 現場有上百個攤位提供各式小吃與飲料\n"
    "SUMMARY: 主辦單位預估將吸引大量人潮前往參觀\n"
    "FAQ_Q: 有哪些活動\n"
    "FAQ_A: 現場有上百個攤位提供各式小吃與飲料\n"
    "TAG: 美食\n"
    "TAG: 市集\n"
    "TAG: 華山\n"
)


class DualModeChatClient:
    """A fake LlmClient that answers BOTH Stage-2 LLM calls deterministically.

    `assemble` and `generate_structural_copy` both call ``.chat(system=..., ...)``.
    The copywriter's system prompt contains "structural copy"; the assembler's
    does not — so we route on that to return the right payload for each call.
    """

    model = "fake-model"

    def __init__(self, *, body: str = BODY, copy: str = COPY):
        self._body = body
        self._copy = copy

    def chat(self, *, system: str = "", **kwargs: object) -> ChatResult:
        text = self._copy if "structural copy" in system else self._body
        return ChatResult(
            text=text,
            finish_reason="stop",
            model=self.model,
            needs_revision=False,
            revision_reason=None,
            executed=True,
        )


class FakeCrawler:
    """No-network Stage-1 crawler: writes source.txt + a manifest (assets=[]).

    Mirrors the FakeCrawler in test_pipeline_batch.py — proves the seam without a
    subprocess or network (the real subprocess crawl has its own smoke test)."""

    def __init__(self, source_text: str = SOURCE):
        self.source_text = source_text

    def crawl(self, spec: SourceSpec) -> RawJobBundle:
        raw = spec.job_dir / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        (raw / "source.txt").write_text(self.source_text, encoding="utf-8")
        manifest = build_manifest(
            job_id=spec.job_id,
            source_type=SourceType.LOCAL_DIR,
            source_domain=None,
            fetched_at=None,
            assets=[],
            source_html=None,
            source_text=self.source_text,
            crawl_status=STATUS_CRAWLED,
        )
        write_manifest(spec.job_dir, manifest, create_only=True)
        return RawJobBundle(
            job_id=spec.job_id,
            raw_dir=raw,
            manifest=manifest,
            job_status=STATUS_CRAWLED,
        )


def spec_for(store: JobStore, job_id: str) -> SourceSpec:
    return SourceSpec(
        job_id=job_id,
        source_type=SourceType.LOCAL_DIR,
        job_dir=store.job_dir(job_id),
        local_dir=Path("/unused"),
    )


def seed_clean_index(store: JobStore) -> None:
    """Present-but-empty site index -> dedup returns UNIQUE (HIGH reliability),
    not UNCERTAIN. The operator gets this via `lcp init`; tests seed it here."""
    (store.base_dir / "site_index.jsonl").write_text("", encoding="utf-8")


# A ContentConfig with loose Unit-1 field-length constraints so e2e tests that
# exercise the full gate chain (risk/dedup/grounding/copy) are not broken by the
# new intro/event_body/faq/quick_facts length rules. The fake LLM (DualMode-
# ChatClient) generates minimal content for determinism; testing the strict
# defaults is the job of tests/rules/test_lint_rules.py.
LOOSE_CONTENT_CONFIG = ContentConfig(
    intro_min_chars=1,
    intro_max_chars=9999,
    event_body_min_chars=1,
    event_body_max_chars=9999,
    summary_warn_chars=9998,
    summary_error_chars=9999,
    faq_min_count=1,
    faq_max_count=99,
    quick_facts_min_count=1,
    quick_facts_max_count=99,
)


def build_pipeline(
    store: JobStore,
    audit: AuditLog,
    *,
    config: Config | None = None,
    llm_client: object | None = None,
    source: str = SOURCE,
) -> pl.Pipeline:
    """A Pipeline wired with the no-network crawler + a deterministic LLM."""
    if config is None:
        config = Config(content=LOOSE_CONTENT_CONFIG)
    elif config.content == ContentConfig():
        # Caller passed a Config with default ContentConfig (only overriding
        # other sections like publisher) — apply loose constraints so the fake
        # short LLM output still passes lint.
        config = config.model_copy(update={"content": LOOSE_CONTENT_CONFIG})
    return pl.Pipeline(
        config,
        store,
        audit,
        crawler=FakeCrawler(source),
        llm_client=llm_client if llm_client is not None else DualModeChatClient(),
    )
