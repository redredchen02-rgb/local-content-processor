import logging
import os

from lcp.runtime_hardening import (
    minimal_env,
    redact,
    set_restrictive_umask,
    SecretRedactingFilter,
)


def test_redact_masks_secret_assignments():
    out = redact("calling api_key=sk-abc123 and token: 'xyz789'")
    assert "sk-abc123" not in out
    assert "xyz789" not in out
    assert "REDACTED" in out


def test_redact_leaves_plain_text():
    assert redact("hello world, job 20260616-001 done") == (
        "hello world, job 20260616-001 done"
    )


def test_redact_masks_json_form():
    # P2 regression: {"api_key": "sk-..."} JSON shape must be masked.
    out = redact('{"api_key": "sk-abcdef0123456789ABCDEF", "model": "x"}')
    assert "sk-abcdef0123456789ABCDEF" not in out
    assert "REDACTED" in out
    assert '"model": "x"' in out  # non-secret fields survive


def test_redact_masks_provider_error_with_standalone_token():
    # P2 regression: "Incorrect API key provided: sk-..." — the token must be
    # masked even though "api key" is not a key=value assignment.
    out = redact("Incorrect API key provided: sk-liveKEY0123456789abcdef")
    assert "sk-liveKEY0123456789abcdef" not in out
    assert "REDACTED" in out


def test_redact_masks_space_separated_authorization_bearer():
    # P2 regression: "Authorization Bearer abc" (space-separated, no =/:).
    out = redact("request had Authorization Bearer abc123secrettoken value")
    assert "abc123secrettoken" not in out
    assert "REDACTED" in out


def test_redact_masks_jwt_shape():
    jwt = "eyJhbGciOiJI.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4f"
    out = redact(f"token issued: {jwt}")
    assert jwt not in out
    assert "REDACTED" in out


def test_log_filter_masks_message():
    rec = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "auth Authorization: Bearer leakme", None, None
    )
    SecretRedactingFilter().filter(rec)
    assert "leakme" not in rec.getMessage()


def test_minimal_env_excludes_secrets(monkeypatch):
    monkeypatch.setenv("LCP_LLM_API_KEY", "super-secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = minimal_env()
    assert "LCP_LLM_API_KEY" not in env
    assert env.get("PATH") == "/usr/bin"


def test_minimal_env_extra_merged():
    env = minimal_env({"FOO": "bar"})
    assert env["FOO"] == "bar"


def test_umask_sets_restrictive(tmp_path):
    set_restrictive_umask()
    f = tmp_path / "secret.txt"
    f.write_text("x", encoding="utf-8")
    mode = os.stat(f).st_mode & 0o777
    # group/other must have no permissions under umask 0o077
    assert mode & 0o077 == 0
