"""U5: ffprobe numeric robustness — pure parsing, no real ffmpeg needed.

These exercise the numeric coercion helpers and the blackdetect/silencedetect
stderr parsing against hostile/corrupt values (NaN, inf, negative, float-encoded
ints, malformed-but-regex-matching numbers). They monkeypatch _run/_require so
they do NOT require the ffmpeg binary (unlike test_ffprobe.py)."""

from __future__ import annotations

import subprocess

from lcp.adapters.media import ffprobe


def _fake_run(stderr: str):
    def _run(argv, timeout):  # signature matches ffprobe._run
        return subprocess.CompletedProcess(argv, 0, "", stderr)

    return _run


# --- numeric coercion --------------------------------------------------------


def test_to_float_rejects_non_finite_and_non_positive():
    assert ffprobe._to_float(float("nan")) is None
    assert ffprobe._to_float(float("inf")) is None
    assert ffprobe._to_float("nan") is None
    assert ffprobe._to_float(-5) is None
    assert ffprobe._to_float(0) is None
    assert ffprobe._to_float("abc") is None
    assert ffprobe._to_float(None) is None


def test_to_float_accepts_positive_finite():
    assert ffprobe._to_float("1.5") == 1.5
    assert ffprobe._to_float(2) == 2.0


def test_to_int_tolerates_float_encoded():
    assert ffprobe._to_int("1920.0") == 1920
    assert ffprobe._to_int("1080") == 1080
    assert ffprobe._to_int(1920) == 1920
    assert ffprobe._to_int(3.0) == 3


def test_to_int_rejects_non_finite_and_garbage():
    assert ffprobe._to_int("nan") is None
    assert ffprobe._to_int(float("inf")) is None
    assert ffprobe._to_int("abc") is None
    assert ffprobe._to_int(None) is None


# --- blackdetect / silencedetect stderr parsing ------------------------------


def test_detect_black_skips_malformed_interval(monkeypatch):
    monkeypatch.setattr(ffprobe, "_require", lambda _name: "ffmpeg")
    stderr = (
        "black_start:1.2.3 black_end:2.0\n"  # malformed start -> must be skipped
        "black_start:5.0 black_end:6.0\n"  # valid
    )
    monkeypatch.setattr(ffprobe, "_run", _fake_run(stderr))
    intervals = ffprobe.detect_black_segments("x.mp4")
    assert intervals == [(5.0, 6.0)]  # bad one skipped, gate did not crash


def test_detect_silence_skips_malformed_start(monkeypatch):
    monkeypatch.setattr(ffprobe, "_require", lambda _name: "ffmpeg")
    stderr = (
        "silence_start: 1.2.3\n"  # malformed -> skipped
        "silence_end: 2.0\n"  # has no valid pending start -> ignored
        "silence_start: 5.0\n"
        "silence_end: 6.0\n"  # valid pair
    )
    monkeypatch.setattr(ffprobe, "_run", _fake_run(stderr))
    intervals = ffprobe.detect_silence("x.mp4")
    assert intervals == [(5.0, 6.0)]
