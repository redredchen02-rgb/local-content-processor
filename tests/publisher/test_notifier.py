"""SOP U3: Telegram notification (fire-and-forget, operator-triggered).

Tests cover: happy path, network failure (audited/ExternalServiceError), config
disabled, dry-run, non-REVIEW_PENDING state guard, cover absent fallback, and
the webserver notification-enabled meta tag injection."""

from __future__ import annotations

import urllib.error
from pathlib import Path

import pytest

from lcp.adapters.publisher import notifier
from lcp.adapters.publisher.notifier import (
    EVENT_NOTIFICATION_FAILED,
    EVENT_NOTIFICATION_SENT,
    send_notification,
)
from lcp.adapters.storage.audit_log import AuditLog
from lcp.adapters.storage.job_store import JobStore
from lcp.core.config import NotificationConfig
from lcp.core.errors import DependencyError, ExternalServiceError, InputValidationError
from lcp.core.state import JobState

TS = "2026-06-23T00:00:00Z"
TOKEN = "test-bot-token-123"
CHAT_ID = "-1001234567890"


def _enabled_config(chat_id: str = CHAT_ID) -> NotificationConfig:
    return NotificationConfig(enabled=True, telegram_chat_id=chat_id)


def _job_at_review_pending(store: JobStore, job_id: str) -> None:
    from lcp.adapters.processor._persist import persist_gate_state

    store.create_job(job_id, created_at=TS)
    store.set_state(job_id, JobState.CRAWLED, updated_at=TS)
    persist_gate_state(store, job_id, JobState.PROCESSED, updated_at=TS)
    store.set_state(job_id, JobState.REVIEW_PENDING, updated_at=TS)


@pytest.fixture()
def store(tmp_path):
    return JobStore(base_dir=str(tmp_path))


@pytest.fixture()
def audit(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")


@pytest.fixture()
def review_dir(tmp_path):
    rd = tmp_path / "review_packet"
    rd.mkdir()
    return rd


# --- happy path ---------------------------------------------------------------


def test_happy_path_sends_photo_and_writes_audit(store, audit, review_dir, monkeypatch):
    _job_at_review_pending(store, "j1")
    cover = review_dir / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xff" + b"\x00" * 10)  # minimal fake JPEG

    calls = []

    def _fake_urlopen(req, timeout):
        calls.append(req)

    monkeypatch.setattr(notifier, "urllib", _make_urllib_mock(_fake_urlopen))

    send_notification(
        "j1", review_dir, "測試標題", _enabled_config(), audit, store,
        bot_token=TOKEN, ts=TS,
    )

    assert len(calls) == 1
    events = _read_events(audit)
    assert any(e.get("event") == EVENT_NOTIFICATION_SENT for e in events)
    sent_ev = next(e for e in events if e.get("event") == EVENT_NOTIFICATION_SENT)
    assert sent_ev["extra"]["has_photo"] is True
    assert sent_ev["extra"]["chat_id"] == CHAT_ID
    # token must NOT appear in audit
    raw = (audit.path).read_text(encoding="utf-8")
    assert TOKEN not in raw
    # job state is unchanged
    assert store.get_job("j1").state == JobState.REVIEW_PENDING


def test_happy_path_text_fallback_when_cover_absent(store, audit, review_dir, monkeypatch):
    _job_at_review_pending(store, "j2")
    calls = []

    def _fake_urlopen(req, timeout):
        calls.append(req)

    monkeypatch.setattr(notifier, "urllib", _make_urllib_mock(_fake_urlopen))

    send_notification(
        "j2", review_dir, "無封面標題", _enabled_config(), audit, store,
        bot_token=TOKEN, ts=TS,
    )

    assert len(calls) == 1
    events = _read_events(audit)
    sent_ev = next(e for e in events if e.get("event") == EVENT_NOTIFICATION_SENT)
    assert sent_ev["extra"]["has_photo"] is False


# --- failure paths ------------------------------------------------------------


def test_network_failure_writes_failed_audit_and_raises(store, audit, review_dir, monkeypatch):
    _job_at_review_pending(store, "j3")

    def _failing_urlopen(req, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(notifier, "urllib", _make_urllib_mock(_failing_urlopen))

    with pytest.raises(ExternalServiceError):
        send_notification(
            "j3", review_dir, "title", _enabled_config(), audit, store,
            bot_token=TOKEN, ts=TS,
        )

    events = _read_events(audit)
    assert any(e.get("event") == EVENT_NOTIFICATION_FAILED for e in events)
    # Job state unchanged despite failure
    assert store.get_job("j3").state == JobState.REVIEW_PENDING


def test_config_disabled_raises_before_network(store, audit, review_dir, monkeypatch):
    _job_at_review_pending(store, "j4")
    calls = []

    def _fake_urlopen(req, timeout):
        calls.append(req)

    monkeypatch.setattr(notifier, "urllib", _make_urllib_mock(_fake_urlopen))

    with pytest.raises(InputValidationError, match="disabled"):
        send_notification(
            "j4", review_dir, "title", NotificationConfig(enabled=False), audit, store,
            bot_token=TOKEN, ts=TS,
        )

    assert calls == []


def test_missing_chat_id_raises_input_error(store, audit, review_dir, monkeypatch):
    _job_at_review_pending(store, "j5")
    with pytest.raises(InputValidationError, match="chat_id"):
        send_notification(
            "j5", review_dir, "title",
            NotificationConfig(enabled=True, telegram_chat_id=""),
            audit, store, bot_token=TOKEN, ts=TS,
        )


def test_empty_token_raises_dependency_error(store, audit, review_dir):
    _job_at_review_pending(store, "j6")
    with pytest.raises(DependencyError):
        send_notification(
            "j6", review_dir, "title", _enabled_config(), audit, store,
            bot_token="", ts=TS,
        )


def test_non_review_pending_state_raises(store, audit, review_dir):
    store.create_job("j7", created_at=TS)
    store.set_state("j7", JobState.CRAWLED, updated_at=TS)
    # Leave at CRAWLED — not REVIEW_PENDING
    with pytest.raises(InputValidationError, match="REVIEW_PENDING"):
        send_notification(
            "j7", review_dir, "title", _enabled_config(), audit, store,
            bot_token=TOKEN, ts=TS,
        )


# --- dry-run ------------------------------------------------------------------


def test_dry_run_returns_without_api_call_or_audit(store, audit, review_dir, monkeypatch):
    _job_at_review_pending(store, "j8")
    cover = review_dir / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xff" + b"\x00" * 10)
    calls = []

    def _fake_urlopen(req, timeout):
        calls.append(req)

    monkeypatch.setattr(notifier, "urllib", _make_urllib_mock(_fake_urlopen))

    send_notification(
        "j8", review_dir, "title", _enabled_config(), audit, store,
        bot_token=TOKEN, ts=TS, dry_run=True,
    )

    assert calls == []
    events = _read_events(audit)
    assert not any(
        e.get("event") in (EVENT_NOTIFICATION_SENT, EVENT_NOTIFICATION_FAILED)
        for e in events
    )


# --- webserver meta tag -------------------------------------------------------


def test_webserver_injects_notification_enabled_true(tmp_path):
    import threading

    import yaml

    from lcp import webserver
    from lcp.gui import Api

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump({
            "storage": {"base_dir": str(tmp_path)},
            "notification": {"enabled": True, "telegram_chat_id": "-123"},
        }),
        encoding="utf-8",
    )
    api = Api(config_path=str(cfg))
    token = "test-token-xyz"
    srv = webserver.build_server(api, token=token, port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        conn.request("GET", "/", headers={"Host": f"127.0.0.1:{srv.server_address[1]}"})
        resp = conn.getresponse()
        body = resp.read().decode()
        assert 'content="true"' in body
        assert webserver.NOTIFICATION_PLACEHOLDER not in body
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)


def test_webserver_injects_notification_enabled_false(tmp_path):
    import threading

    import yaml

    from lcp import webserver
    from lcp.gui import Api

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump({
            "storage": {"base_dir": str(tmp_path)},
            "notification": {"enabled": False},
        }),
        encoding="utf-8",
    )
    api = Api(config_path=str(cfg))
    token = "test-token-xyz"
    srv = webserver.build_server(api, token=token, port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        conn.request("GET", "/", headers={"Host": f"127.0.0.1:{srv.server_address[1]}"})
        resp = conn.getresponse()
        body = resp.read().decode()
        assert 'content="false"' in body
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)


# --- helpers ------------------------------------------------------------------


def _read_events(audit: AuditLog) -> list[dict]:
    import json

    if not audit.path.exists():
        return []
    return [json.loads(line) for line in audit.path.read_text().splitlines() if line.strip()]


class _MockUrllib:
    """Minimal urllib replacement for monkeypatching notifier.urllib."""

    def __init__(self, urlopen_fn):
        self.request = _MockUrllibRequest()
        self.error = urllib.error
        self._urlopen = urlopen_fn

    def __getattr__(self, name: str):
        import urllib as _urllib
        return getattr(_urllib, name)


class _MockUrllibRequest:
    class Request:
        def __init__(self, url, data=None, headers=None):
            self.url = url
            self.data = data
            self.headers = headers or {}


def _make_urllib_mock(urlopen_fn):
    """Build a mock urllib module whose urlopen calls urlopen_fn."""
    import types

    import urllib.error
    import urllib.parse
    import urllib.request

    mock = types.ModuleType("urllib")
    mock.error = urllib.error
    mock.parse = urllib.parse

    mock_request = types.ModuleType("urllib.request")
    mock_request.Request = urllib.request.Request
    mock_request.urlopen = urlopen_fn
    mock.request = mock_request

    return mock
