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
