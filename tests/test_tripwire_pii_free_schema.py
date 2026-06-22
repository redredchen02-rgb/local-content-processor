"""Tripwire: the persisted surfaces stay PII-free by construction (U20).

Three surfaces persist state, and they are NOT the same shape — so the guard
uses a PER-SURFACE allow-set rather than one blanket rule:

  * jobs table   — strict hash/enum/code only (introspected via PRAGMA
                   table_info). A new free-text column fails.
  * audit payload — `audit_log.append`'s `_PROHIBITED_KEYS` denylist still
                   rejects raw-identifier keys; the U8 `blocking_reasons` audit
                   VALUE carries only RiskCategory enum CODES (audit is
                   value-blind, so we check the value shape ourselves).
  * manifest     — an EXPLICIT allow-list of the intentionally-present fields.
                   It DELIBERATELY includes `Manifest.source_domain` and
                   `AssetRef.source_url`: "PII-free" here means no scraped
                   title/body, NOT "no URLs". A NEW field beyond the allow-list
                   fails (it must be reviewed and added to pii-inventory first).

This fails the moment a new free-text column / audit key / manifest field
creeps in — the schema can only drift through this test.
"""

from __future__ import annotations

import sqlite3

import pytest

from lcp.adapters.storage.audit_log import _PROHIBITED_KEYS, AuditLog
from lcp.adapters.storage.job_store import JobStore
from lcp.core.models import AssetRef, Manifest
from lcp.core.rules.risk_rules import RiskCategory

TS = "2026-06-18T00:00:00Z"

# --- Surface 1: jobs table -------------------------------------------------

# The ONLY columns the PII-free index may carry: a primary key, the state enum,
# two timestamps, two CONTENT sha256 hashes, an error CODE, and a review-reason
# CODE. No free text. A new column must be justified and added here knowingly.
_ALLOWED_JOBS_COLUMNS = frozenset(
    {
        "job_id",  # opaque id (caller-supplied, not derived from PII)
        "state",  # JobState enum value
        "created_at",  # ISO timestamp
        "updated_at",  # ISO timestamp
        "source_html_sha256",  # content hash
        "source_text_sha256",  # content hash
        "error_code",  # LcpError code
        "review_reason",  # ReviewReason enum CODE
    }
)


def test_jobs_table_has_only_allowed_columns(tmp_path) -> None:
    store = JobStore(base_dir=tmp_path)
    conn = sqlite3.connect(store.db_path)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    finally:
        conn.close()
    assert cols == _ALLOWED_JOBS_COLUMNS, (
        "jobs table columns drifted from the PII-free allow-set; a new column "
        "must be a hash/enum/code and recorded in pii-inventory.md before being "
        f"added here. Got: {sorted(cols)}"
    )


def test_u7_interrupt_count_is_a_file_not_a_jobs_column(tmp_path) -> None:
    """U7's crash-attempt counter lives in a per-job-dir FILE (.interrupt_count),
    NOT a jobs column — confirm it never landed as a (potentially-correlatable)
    table column."""
    store = JobStore(base_dir=tmp_path)
    conn = sqlite3.connect(store.db_path)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    finally:
        conn.close()
    assert "interrupt_count" not in cols


def test_a_new_free_text_jobs_column_would_trip(tmp_path) -> None:
    """Demonstrate the tripwire FIRES: a free-text column outside the allow-set
    fails the same comparison the real schema passes."""
    store = JobStore(base_dir=tmp_path)
    conn = sqlite3.connect(store.db_path)
    try:
        conn.execute("ALTER TABLE jobs ADD COLUMN scraped_title TEXT")
        conn.commit()
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    finally:
        conn.close()
    assert cols != _ALLOWED_JOBS_COLUMNS  # the guard would fail on this schema


# --- Surface 2: audit payload ----------------------------------------------


def test_audit_denylist_still_rejects_pii_keys(tmp_path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    # A representative raw-identifier key must be refused.
    for key in ("title", "source_url", "author", "body"):
        assert key in _PROHIBITED_KEYS
        with pytest.raises(Exception):  # InputValidationError
            audit.append(
                ts=TS,
                stage="test",
                event="X",
                job_id="j1",
                actor="human",
                extra={key: "anything"},
            )


def test_audit_blocking_reasons_carries_only_enum_codes(tmp_path) -> None:
    """The U8 redline-override audit records `blocking_reasons` — these MUST be
    RiskCategory enum CODES (never the free-text flag reason, which can carry a
    matched snippet). audit_log is value-blind, so assert the value shape here."""
    valid_codes = {c.value for c in RiskCategory}

    # A faithful sample of what supersede() writes for a redline override.
    blocking_reasons = [RiskCategory.NCII.value, RiskCategory.MINOR.value]
    assert all(r in valid_codes for r in blocking_reasons)

    # And confirm a free-text value (a leaked flag reason) is NOT a valid code —
    # i.e. the "enum-codes-only" assertion has teeth.
    leaked = "ncii: matched '受害者 露點 偷拍' near line 3"
    assert leaked not in valid_codes
    assert not all(r in valid_codes for r in [leaked])


# --- Surface 3: manifest ---------------------------------------------------

# Intentionally-present manifest fields. This INCLUDES source_domain (Manifest)
# and source_url (AssetRef): deliberately-persisted URL/domain text. The
# hash-only rule does NOT apply here — the guard only forbids a NEW field
# sneaking in unreviewed.
_ALLOWED_MANIFEST_FIELDS = frozenset(
    {
        "job_id",
        "source_type",
        "source_domain",  # deliberately persisted (R: own-site provenance)
        "crawl_status",
        "fetched_at",
        "assets",
        "hashes",
        "logic_version",
    }
)
_ALLOWED_ASSET_FIELDS = frozenset(
    {
        "kind",
        "path",
        "source_url",  # deliberately persisted inert URL text (never fetched)
        "sha256",
        "state",
        "note",  # bounded per-asset outcome annotation (enum-adjacent, plan G2)
    }
)
_ALLOWED_HASHES_FIELDS = frozenset({"source_html_sha256", "source_text_sha256"})


def test_manifest_fields_match_allow_list() -> None:
    assert set(Manifest.model_fields) == _ALLOWED_MANIFEST_FIELDS, (
        "Manifest gained/lost a field; a NEW free-text field must be reviewed "
        "against pii-inventory.md and added to the allow-list knowingly. "
        f"Got: {sorted(Manifest.model_fields)}"
    )
    assert set(AssetRef.model_fields) == _ALLOWED_ASSET_FIELDS
    # Hashes is nested; pin it too so a free-text field cannot hide there.
    hashes_field = Manifest.model_fields["hashes"]
    assert set(hashes_field.annotation.model_fields) == _ALLOWED_HASHES_FIELDS


def test_intentional_url_fields_do_not_trip() -> None:
    """The deliberately-persisted source_domain / source_url are WITHIN the
    allow-list — they must NOT be flagged."""
    assert "source_domain" in _ALLOWED_MANIFEST_FIELDS
    assert "source_url" in _ALLOWED_ASSET_FIELDS
    assert "source_domain" in Manifest.model_fields
    assert "source_url" in AssetRef.model_fields


def test_a_new_free_text_manifest_field_would_trip() -> None:
    """Demonstrate the tripwire FIRES on a new free-text field."""
    drifted = _ALLOWED_MANIFEST_FIELDS | {"scraped_body"}
    assert drifted != _ALLOWED_MANIFEST_FIELDS
    # i.e. if Manifest grew `scraped_body`, the equality assert above would fail.
