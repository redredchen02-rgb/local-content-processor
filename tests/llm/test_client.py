"""Unit 7a — LlmClient tests. No real network: the openai client is injected via
`client_factory`. Covers transport security (R40), finish_reason gating, failure
mapping (exit 3/4), dry-run (no API call), and secret hygiene."""

from __future__ import annotations

import types

import pytest

from lcp.adapters.llm.client import ChatResult, LlmClient
from lcp.core.config import Config, LlmConfig
from lcp.core.errors import (
    EXIT_DEPENDENCY,
    EXIT_EXTERNAL,
    EXIT_INPUT,
    DependencyError,
    ExternalServiceError,
    InputValidationError,
)

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
            completions=StubCompletions(response=response, raises=raises, recorder=self.recorder)
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
    import lcp.adapters.storage.config_io as cfg

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


def test_finish_reason_content_filter_distinct_from_truncation(with_key):
    # Unit 15: content_filter is a PROVIDER BLOCK, not a cut-off completion. It
    # must carry a distinct reviewer-visible signal from a `length` truncation so
    # a human can tell "the model was censored" from "the output ran out of room".
    filtered = StubOpenAI(response=_response("blocked", "content_filter"))
    res_f = LlmClient(_config(), client_factory=filtered.factory).chat(system="r", user="d")
    assert res_f.needs_revision is True
    assert res_f.revision_reason == "filtered:content_filter"

    truncated = StubOpenAI(response=_response("cut off", "length"))
    res_t = LlmClient(_config(), client_factory=truncated.factory).chat(system="r", user="d")
    # The two reasons are distinct strings (not both "truncated:<reason>").
    assert res_t.revision_reason == "truncated:length"
    assert res_f.revision_reason != res_t.revision_reason


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
    import lcp.adapters.storage.config_io as cfg

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


def test_external_error_message_redacts_exact_resolved_key(with_key):
    # P2 regression: a provider error that echoes the EXACT resolved api_key
    # (e.g. "Incorrect API key provided: sk-...") must mask it.
    leaky = RuntimeError(f"Incorrect API key provided: {SECRET}")
    stub = StubOpenAI(raises=leaky)
    client = LlmClient(_config(), client_factory=stub.factory)
    with pytest.raises(ExternalServiceError) as ei:
        client.chat(system="r", user="d")
    assert SECRET not in str(ei.value)
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
    client = LlmClient(cfg, client_factory=stub.factory, allow_http_hosts=["127.0.0.1"])
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
    client = LlmClient(_config(), client_factory=stub.factory, ca_bundle=bundle)
    client.chat(system="r", user="d")
    kw = StubOpenAI.last_init_kwargs
    assert "http_client" in kw  # custom CA -> custom httpx client supplied
    import httpx

    assert isinstance(kw["http_client"], httpx.Client)
    # The bundle path must NOT translate into disabled verification.
    assert "verify" not in kw


# --------------------------------------------------------------------------
# R40 escape hatch is config-driven (U7a): ca_bundle + allow_http_hosts come
# from LlmConfig and reach the client the way the pipeline wires them.
# --------------------------------------------------------------------------


def _client_from_config(cfg, *, factory):
    """Build an LlmClient sourcing the escape hatch from config exactly like
    Pipeline does — proving the fields are wired, not just constructor params."""
    return LlmClient(
        cfg,
        client_factory=factory,
        ca_bundle=cfg.llm.ca_bundle,
        allow_http_hosts=cfg.llm.allow_http_hosts,
    )


def test_config_ca_bundle_reaches_client_builds_verifying_context(with_key):
    # A private-CA bundle path set in CONFIG must reach the client and produce a
    # verifying httpx client (real SSLContext), never verify=False.
    import certifi
    import httpx

    cfg = _config(ca_bundle=certifi.where())
    stub = StubOpenAI(response=_response("ok", "stop"))
    client = _client_from_config(cfg, factory=stub.factory)
    client.chat(system="r", user="d")
    kw = StubOpenAI.last_init_kwargs
    assert "http_client" in kw  # config ca_bundle -> custom httpx client
    assert isinstance(kw["http_client"], httpx.Client)
    assert "verify" not in kw  # NEVER disabled verification


def test_config_http_loopback_allowed_when_in_allow_http_hosts(with_key):
    # http base_url accepted ONLY when its host is in config.allow_http_hosts
    # AND is loopback/private.
    cfg = _config(
        base_url="http://127.0.0.1:8000/v1",
        allowed_hosts=["127.0.0.1"],
        allow_http_hosts=["127.0.0.1"],
    )
    stub = StubOpenAI(response=_response("ok", "stop"))
    client = _client_from_config(cfg, factory=stub.factory)
    res = client.chat(system="r", user="d")
    assert res.text == "ok"


def test_config_http_loopback_rejected_without_allow_http_hosts(with_key):
    # Same loopback host but NOT in config.allow_http_hosts -> rejected.
    cfg = _config(
        base_url="http://127.0.0.1:8000/v1",
        allowed_hosts=["127.0.0.1"],
        allow_http_hosts=[],
    )
    client = _client_from_config(cfg, factory=StubOpenAI().factory)
    with pytest.raises(InputValidationError):
        client.chat(system="r", user="d")


def test_config_http_non_loopback_rejected_even_in_allow_http_hosts(with_key):
    # A public host listed in config.allow_http_hosts is STILL rejected over
    # http because it is not loopback/private (https mandatory on the internet).
    cfg = _config(
        base_url="http://llm.example.com/v1",
        allowed_hosts=["llm.example.com"],
        allow_http_hosts=["llm.example.com"],
    )
    client = _client_from_config(cfg, factory=StubOpenAI().factory)
    with pytest.raises(InputValidationError):
        client.chat(system="r", user="d")


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
    import lcp.adapters.storage.config_io as cfg

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


# --------------------------------------------------------------------------
# per-process cooldown after repeated failures (Unit 14)
#
# The SDK already retries+jitters per call; the residual gap is no cross-job
# cooldown — a sustained-5xx provider is re-hammered on every job re-run. We
# inject a monotonic clock so these tests drive the window without sleeping.
# --------------------------------------------------------------------------


class FakeMonotonic:
    """A controllable monotonic clock for cooldown tests."""

    def __init__(self, start: float = 0.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class CountingCompletions:
    """Records how many times create() is actually invoked, and can raise on the
    first N calls then succeed (to model a transient vs. sustained outage)."""

    def __init__(self, *, raises, fail_count=None, response=None, recorder):
        self._raises = raises
        self._fail_count = fail_count  # None => always raise
        self._response = response if response is not None else _response("ok", "stop")
        self._recorder = recorder

    def create(self, **kwargs):
        self._recorder["calls"] += 1
        if self._raises is not None and (
            self._fail_count is None or self._recorder["calls"] <= self._fail_count
        ):
            raise self._raises
        return self._response


class CountingOpenAI:
    def __init__(self, *, raises=None, fail_count=None, response=None):
        self.recorder = {"calls": 0}
        self.chat = types.SimpleNamespace(
            completions=CountingCompletions(
                raises=raises,
                fail_count=fail_count,
                response=response,
                recorder=self.recorder,
            )
        )

    def factory(self, **kwargs):
        return self


def _server_error():
    import httpx
    from openai import APIStatusError

    resp = httpx.Response(503, request=httpx.Request("POST", "https://x/v1"))
    return APIStatusError("server error", response=resp, body=None)


def test_cooldown_short_circuits_after_threshold(with_key):
    # N consecutive ExternalServiceErrors trip the cooldown; the very next call
    # must short-circuit WITHOUT re-hitting the endpoint (no new create() call).
    clock = FakeMonotonic()
    stub = CountingOpenAI(raises=_server_error())  # always 503
    client = LlmClient(
        _config(),
        client_factory=stub.factory,
        monotonic=clock,
        cooldown_failure_threshold=3,
        cooldown_seconds=30.0,
    )
    for _ in range(3):
        with pytest.raises(ExternalServiceError):
            client.chat(system="r", user="d")
    assert stub.recorder["calls"] == 3  # all three actually hit the endpoint

    # 4th call, still inside the window -> short-circuit, NO new create() call.
    with pytest.raises(ExternalServiceError) as ei:
        client.chat(system="r", user="d")
    assert ei.value.exit_code == EXIT_EXTERNAL
    assert stub.recorder["calls"] == 3  # unchanged: endpoint NOT re-hit
    assert SECRET not in str(ei.value)


def test_single_transient_error_does_not_trip_cooldown(with_key):
    # A single transient failure that then succeeds must NOT trip the cooldown:
    # the next call goes through, and the success resets the counter.
    clock = FakeMonotonic()
    # raises once, succeeds thereafter
    stub = CountingOpenAI(raises=_server_error(), fail_count=1, response=_response("ok", "stop"))
    client = LlmClient(
        _config(),
        client_factory=stub.factory,
        monotonic=clock,
        cooldown_failure_threshold=3,
        cooldown_seconds=30.0,
    )
    with pytest.raises(ExternalServiceError):
        client.chat(system="r", user="d")  # call 1: transient failure
    res = client.chat(system="r", user="d")  # call 2: succeeds, no cooldown
    assert res.text == "ok"
    assert stub.recorder["calls"] == 2  # both reached the endpoint


def test_success_resets_consecutive_failure_counter(with_key):
    # Two failures, then a success, then two more failures should NOT trip a
    # threshold-3 cooldown — the success in the middle resets the counter.
    clock = FakeMonotonic()
    recorder = {"calls": 0}

    class Flaky:
        def __init__(self):
            self.script = ["fail", "fail", "ok", "fail", "fail"]

        def create(self, **kwargs):
            recorder["calls"] += 1
            outcome = self.script[recorder["calls"] - 1]
            if outcome == "fail":
                raise _server_error()
            return _response("ok", "stop")

    stub = types.SimpleNamespace(chat=types.SimpleNamespace(completions=Flaky()))
    client = LlmClient(
        _config(),
        client_factory=lambda **kw: stub,
        monotonic=clock,
        cooldown_failure_threshold=3,
        cooldown_seconds=30.0,
    )
    with pytest.raises(ExternalServiceError):
        client.chat(system="r", user="d")  # 1 fail
    with pytest.raises(ExternalServiceError):
        client.chat(system="r", user="d")  # 2 fail
    res = client.chat(system="r", user="d")  # success -> counter reset
    assert res.text == "ok"
    with pytest.raises(ExternalServiceError):
        client.chat(system="r", user="d")  # 1 fail (post-reset)
    with pytest.raises(ExternalServiceError):
        client.chat(system="r", user="d")  # 2 fail
    # All five reached the endpoint: the cooldown never tripped.
    assert recorder["calls"] == 5


def test_cooldown_expires_after_window(with_key):
    # Once the cooldown window elapses (advance the injected clock), a call is
    # attempted against the endpoint again.
    clock = FakeMonotonic()
    stub = CountingOpenAI(raises=_server_error())  # always 503
    client = LlmClient(
        _config(),
        client_factory=stub.factory,
        monotonic=clock,
        cooldown_failure_threshold=3,
        cooldown_seconds=30.0,
    )
    for _ in range(3):
        with pytest.raises(ExternalServiceError):
            client.chat(system="r", user="d")
    assert stub.recorder["calls"] == 3

    # Still inside the window -> short-circuit (no new call).
    with pytest.raises(ExternalServiceError):
        client.chat(system="r", user="d")
    assert stub.recorder["calls"] == 3

    # Advance past the window -> the endpoint is tried again.
    clock.advance(31.0)
    with pytest.raises(ExternalServiceError):
        client.chat(system="r", user="d")
    assert stub.recorder["calls"] == 4


def test_dry_run_never_trips_cooldown(with_key):
    # dry-run never calls the API, so it must never engage the cooldown path.
    clock = FakeMonotonic()
    client = LlmClient(
        _config(),
        dry_run=True,
        client_factory=StubOpenAI(raises=_server_error()).factory,
        monotonic=clock,
        cooldown_failure_threshold=1,
        cooldown_seconds=30.0,
    )
    for _ in range(5):
        res = client.chat(system="r", user="d")
        assert res.executed is False
