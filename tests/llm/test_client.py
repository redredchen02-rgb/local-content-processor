"""Unit 7a — LlmClient tests. No real network: the openai client is injected via
`client_factory`. Covers transport security (R40), finish_reason gating, failure
mapping (exit 3/4), dry-run (no API call), and secret hygiene."""

from __future__ import annotations

import types

import pytest

from lcp.core.config import Config, LlmConfig
from lcp.core.errors import (
    EXIT_DEPENDENCY,
    EXIT_EXTERNAL,
    EXIT_INPUT,
    DependencyError,
    ExternalServiceError,
    InputValidationError,
)
from lcp.adapters.llm.client import ChatResult, LlmClient

SECRET = "sk-supersecret-key-do-not-log-1234567890"


# --------------------------------------------------------------------------
# stub openai client
# --------------------------------------------------------------------------

def _choice(content, finish_reason):
    message = types.SimpleNamespace(content=content)
    return types.SimpleNamespace(message=message, finish_reason=finish_reason)


def _response(content="hello world", finish_reason="stop", choices=None):
    if choices is None:
        choices = [_choice(content, finish_reason)]
    return types.SimpleNamespace(choices=choices)


class StubCompletions:
    def __init__(self, response=None, raises=None, recorder=None):
        self._response = response if response is not None else _response()
        self._raises = raises
        self._recorder = recorder if recorder is not None else {}

    def create(self, **kwargs):
        self._recorder["called"] = True
        self._recorder["kwargs"] = kwargs
        if self._raises is not None:
            raise self._raises
        return self._response


class StubOpenAI:
    """Mimics openai.OpenAI: records init kwargs, exposes .chat.completions."""

    last_init_kwargs: dict = {}

    def __init__(self, response=None, raises=None):
        self._response = response
        self._raises = raises
        self.recorder: dict = {}
        self.chat = types.SimpleNamespace(
            completions=StubCompletions(
                response=response, raises=raises, recorder=self.recorder
            )
        )

    def factory(self, **kwargs):
        StubOpenAI.last_init_kwargs = kwargs
        self.init_kwargs = kwargs
        return self


def _config(**llm_overrides) -> Config:
    base = dict(
        base_url="https://llm.example.com/v1",
        model="company-model",
        allowed_hosts=["llm.example.com"],
        timeout_seconds=42,
        max_retries=5,
    )
    base.update(llm_overrides)
    return Config(llm=LlmConfig(**base))


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setenv("LCP_LLM_API_KEY", SECRET)
    # Make sure keyring lookups don't interfere / hang.
    import lcp.core.config as cfg

    monkeypatch.setattr(cfg, "KEYRING_SERVICE", "lcp-test-service")
    return SECRET


# --------------------------------------------------------------------------
# happy path + finish_reason gate
# --------------------------------------------------------------------------

def test_clean_stop_returns_text(with_key):
    stub = StubOpenAI(response=_response("rewritten body", "stop"))
    client = LlmClient(_config(), client_factory=stub.factory)
    res = client.chat(system="rules", user="data")
    assert isinstance(res, ChatResult)
    assert res.text == "rewritten body"
    assert res.finish_reason == "stop"
    assert res.needs_revision is False
    assert res.executed is True
    assert stub.recorder["called"] is True


def test_passes_lowest_common_denominator_params(with_key):
    stub = StubOpenAI(response=_response("ok", "stop"))
    client = LlmClient(_config(), client_factory=stub.factory)
    client.chat(system="rules", user="data", max_tokens=128, temperature=0.1)
    kw = stub.recorder["kwargs"]
    assert kw["model"] == "company-model"
    assert kw["max_tokens"] == 128
    assert kw["temperature"] == 0.1
    assert [m["role"] for m in kw["messages"]] == ["system", "user"]
    # We must NOT depend on response_format / tools.
    assert "response_format" not in kw
    assert "tools" not in kw


def test_finish_reason_length_needs_revision(with_key):
    stub = StubOpenAI(response=_response("truncated...", "length"))
    client = LlmClient(_config(), client_factory=stub.factory)
    res = client.chat(system="r", user="d")
    assert res.needs_revision is True
    assert res.revision_reason == "truncated:length"


def test_finish_reason_content_filter_needs_revision(with_key):
    stub = StubOpenAI(response=_response("blocked", "content_filter"))
    client = LlmClient(_config(), client_factory=stub.factory)
    res = client.chat(system="r", user="d")
    assert res.needs_revision is True
    assert res.revision_reason == "truncated:content_filter"


def test_empty_content_needs_revision(with_key):
    stub = StubOpenAI(response=_response("", "stop"))
    client = LlmClient(_config(), client_factory=stub.factory)
    res = client.chat(system="r", user="d")
    assert res.needs_revision is True
    assert res.revision_reason == "empty"


def test_none_content_needs_revision(with_key):
    stub = StubOpenAI(response=_response(None, "stop"))
    client = LlmClient(_config(), client_factory=stub.factory)
    res = client.chat(system="r", user="d")
    assert res.needs_revision is True
    assert res.revision_reason == "empty"


def test_no_choices_needs_revision(with_key):
    stub = StubOpenAI(response=_response(choices=[]))
    client = LlmClient(_config(), client_factory=stub.factory)
    res = client.chat(system="r", user="d")
    assert res.needs_revision is True
    assert res.revision_reason == "empty"


# --------------------------------------------------------------------------
# dependency errors (exit 3)
# --------------------------------------------------------------------------

def test_missing_base_url_dependency_error(with_key):
    cfg = _config(base_url="")
    client = LlmClient(cfg, client_factory=StubOpenAI().factory)
    with pytest.raises(DependencyError) as ei:
        client.chat(system="r", user="d")
    assert ei.value.exit_code == EXIT_DEPENDENCY


def test_missing_api_key_dependency_error(monkeypatch):
    monkeypatch.delenv("LCP_LLM_API_KEY", raising=False)
    import lcp.core.config as cfg

    monkeypatch.setattr(cfg, "KEYRING_SERVICE", "lcp-test-no-key")
    # Ensure keyring returns nothing.
    try:
        import keyring

        monkeypatch.setattr(keyring, "get_password", lambda *a, **k: None)
    except Exception:
        pass
    client = LlmClient(_config(), client_factory=StubOpenAI().factory)
    with pytest.raises(DependencyError) as ei:
        client.chat(system="r", user="d")
    assert ei.value.exit_code == EXIT_DEPENDENCY
    # The error message must contain NO secret.
    assert SECRET not in str(ei.value)


# --------------------------------------------------------------------------
# external service errors (exit 4)
# --------------------------------------------------------------------------

def _make_openai_error(cls):
    """Construct an openai exception without a real HTTP request/response."""
    import httpx

    if cls.__name__ == "APITimeoutError":
        from openai import APITimeoutError

        return APITimeoutError(request=httpx.Request("POST", "https://x/v1"))
    if cls.__name__ == "RateLimitError":
        from openai import RateLimitError

        resp = httpx.Response(429, request=httpx.Request("POST", "https://x/v1"))
        return RateLimitError("rate limited", response=resp, body=None)
    if cls.__name__ == "APIStatusError":
        from openai import APIStatusError

        resp = httpx.Response(503, request=httpx.Request("POST", "https://x/v1"))
        return APIStatusError("server error", response=resp, body=None)
    raise AssertionError(cls)


@pytest.mark.parametrize("name", ["APITimeoutError", "RateLimitError", "APIStatusError"])
def test_external_errors_map_to_external_service_error(with_key, name):
    import openai

    exc = _make_openai_error(getattr(openai, name))
    stub = StubOpenAI(raises=exc)
    client = LlmClient(_config(), client_factory=stub.factory)
    with pytest.raises(ExternalServiceError) as ei:
        client.chat(system="r", user="d")
    assert ei.value.exit_code == EXIT_EXTERNAL


def test_external_error_message_redacts_secret(with_key):
    # An error whose text leaks an authorization header must be redacted.
    leaky = RuntimeError("connection failed with authorization=Bearer leakytoken123")
    stub = StubOpenAI(raises=leaky)
    client = LlmClient(_config(), client_factory=stub.factory)
    with pytest.raises(ExternalServiceError) as ei:
        client.chat(system="r", user="d")
    assert "leakytoken123" not in str(ei.value)
    assert "REDACTED" in str(ei.value)


# --------------------------------------------------------------------------
# transport security (R40)
# --------------------------------------------------------------------------

def test_http_non_loopback_rejected(with_key):
    cfg = _config(base_url="http://llm.example.com/v1")
    client = LlmClient(cfg, client_factory=StubOpenAI().factory)
    with pytest.raises(InputValidationError) as ei:
        client.chat(system="r", user="d")
    assert ei.value.exit_code == EXIT_INPUT


def test_host_not_in_allowed_hosts_rejected(with_key):
    cfg = _config(base_url="https://evil.example.org/v1", allowed_hosts=["llm.example.com"])
    client = LlmClient(cfg, client_factory=StubOpenAI().factory)
    with pytest.raises(InputValidationError):
        client.chat(system="r", user="d")


def test_base_url_must_end_with_v1(with_key):
    cfg = _config(base_url="https://llm.example.com/api")
    client = LlmClient(cfg, client_factory=StubOpenAI().factory)
    with pytest.raises(InputValidationError):
        client.chat(system="r", user="d")


def test_loopback_http_allowed_only_when_allowlisted(with_key):
    # 127.0.0.1 in allowed_hosts + explicit http allowlist -> permitted.
    cfg = _config(base_url="http://127.0.0.1:8000/v1", allowed_hosts=["127.0.0.1"])
    stub = StubOpenAI(response=_response("ok", "stop"))
    client = LlmClient(
        cfg, client_factory=stub.factory, allow_http_hosts=["127.0.0.1"]
    )
    res = client.chat(system="r", user="d")
    assert res.text == "ok"


def test_loopback_http_rejected_without_explicit_http_allowlist(with_key):
    cfg = _config(base_url="http://127.0.0.1:8000/v1", allowed_hosts=["127.0.0.1"])
    client = LlmClient(cfg, client_factory=StubOpenAI().factory)  # no allow_http_hosts
    with pytest.raises(InputValidationError):
        client.chat(system="r", user="d")


def test_public_host_in_http_allowlist_still_rejected(with_key):
    # Even if a real public host is added to the http allowlist by mistake, it
    # is rejected because it is not loopback/private.
    cfg = _config(base_url="http://llm.example.com/v1", allowed_hosts=["llm.example.com"])
    client = LlmClient(
        cfg, client_factory=StubOpenAI().factory, allow_http_hosts=["llm.example.com"]
    )
    with pytest.raises(InputValidationError):
        client.chat(system="r", user="d")


def test_client_init_passes_timeout_and_retries_never_verify_false(with_key):
    stub = StubOpenAI(response=_response("ok", "stop"))
    client = LlmClient(_config(), client_factory=stub.factory)
    client.chat(system="r", user="d")
    kw = StubOpenAI.last_init_kwargs
    assert kw["timeout"] == 42
    assert kw["max_retries"] == 5
    assert kw["base_url"] == "https://llm.example.com/v1"
    assert kw["api_key"] == SECRET
    # verify=False must NEVER appear anywhere.
    assert "verify" not in kw


def test_ca_bundle_builds_verifying_http_client_not_verify_false(with_key):
    # A valid PEM bundle stands in for a private-CA bundle supplied via config.
    import certifi

    bundle = certifi.where()
    stub = StubOpenAI(response=_response("ok", "stop"))
    client = LlmClient(
        _config(), client_factory=stub.factory, ca_bundle=bundle
    )
    client.chat(system="r", user="d")
    kw = StubOpenAI.last_init_kwargs
    assert "http_client" in kw  # custom CA -> custom httpx client supplied
    import httpx

    assert isinstance(kw["http_client"], httpx.Client)
    # The bundle path must NOT translate into disabled verification.
    assert "verify" not in kw


# --------------------------------------------------------------------------
# dry-run: API NOT called
# --------------------------------------------------------------------------

def test_dry_run_does_not_call_api(with_key):
    stub = StubOpenAI(response=_response("should not be used", "stop"))
    client = LlmClient(_config(), dry_run=True, client_factory=stub.factory)
    res = client.chat(system="r", user="d")
    assert res.executed is False
    assert "not actually executed" in res.text.lower()
    assert stub.recorder.get("called") is None  # create() never invoked


def test_dry_run_needs_no_api_key(monkeypatch):
    monkeypatch.delenv("LCP_LLM_API_KEY", raising=False)
    import lcp.core.config as cfg

    monkeypatch.setattr(cfg, "KEYRING_SERVICE", "lcp-test-dryrun")
    try:
        import keyring

        monkeypatch.setattr(keyring, "get_password", lambda *a, **k: None)
    except Exception:
        pass
    # dry_run must not even resolve the key.
    client = LlmClient(_config(), dry_run=True)
    res = client.chat(system="r", user="d")
    assert res.executed is False


# --------------------------------------------------------------------------
# temperature bounds
# --------------------------------------------------------------------------

def test_temperature_above_ceiling_rejected(with_key):
    client = LlmClient(_config(), client_factory=StubOpenAI().factory)
    with pytest.raises(InputValidationError):
        client.chat(system="r", user="d", temperature=0.9)
