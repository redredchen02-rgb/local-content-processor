"""ffprobe/ffmpeg adapter tests.

A tiny H.264 test video is synthesized once per session with ffmpeg (testsrc).
ffmpeg/ffprobe 8.1 are present in this environment, so we do not skip; the only
skip-guard is for a genuinely absent binary.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from lcp.core.errors import DependencyError, ExternalServiceError
from lcp.adapters.media import ffprobe
from lcp.runtime_hardening import minimal_env

_HAVE_FFMPEG = shutil.which("ffmpeg") is not None
_HAVE_FFPROBE = shutil.which("ffprobe") is not None

pytestmark = pytest.mark.skipif(
    not (_HAVE_FFMPEG and _HAVE_FFPROBE),
    reason="ffmpeg/ffprobe not installed",
)


def _ffmpeg_gen(argv_tail: list[str]) -> None:
    subprocess.run(
        [shutil.which("ffmpeg"), "-nostdin", "-y", "-hide_banner", "-loglevel",
         "error", *argv_tail],
        check=True,
        env=minimal_env(),
        timeout=60,
    )


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory):
    """1s 1280x720 30fps H.264 testsrc with a sine audio track."""
    d = tmp_path_factory.mktemp("media")
    out = d / "sample.mp4"
    _ffmpeg_gen(
        [
            "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-shortest", str(out),
        ]
    )
    return out


@pytest.fixture(scope="session")
def black_video(tmp_path_factory):
    """2s clip: 1s of visible testsrc then 1s of solid black."""
    d = tmp_path_factory.mktemp("media_black")
    out = d / "black.mp4"
    _ffmpeg_gen(
        [
            "-f", "lavfi", "-i",
            "testsrc=size=640x480:rate=25:duration=1",
            "-f", "lavfi", "-i",
            "color=c=black:size=640x480:rate=25:duration=1",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out),
        ]
    )
    return out


@pytest.fixture(scope="session")
def silent_video(tmp_path_factory):
    """1s clip with a silent audio track."""
    d = tmp_path_factory.mktemp("media_silent")
    out = d / "silent.mp4"
    _ffmpeg_gen(
        [
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=1",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-shortest", str(out),
        ]
    )
    return out


# --- happy path: probe --------------------------------------------------------


def test_probe_returns_core_facts(sample_video):
    info = ffprobe.probe(sample_video)
    assert info.codec == "h264"
    assert info.width == 1280
    assert info.height == 720
    assert info.fps is not None and abs(info.fps - 30.0) < 0.5
    assert info.duration_s is not None and info.duration_s > 0
    assert info.bitrate_mbps is not None and info.bitrate_mbps > 0
    assert info.has_audio is True


def test_probe_feeds_pure_rules(sample_video):
    from lcp.core.rules import asset_rules

    info = ffprobe.probe(sample_video)
    d = asset_rules.judge_video(
        codec=info.codec,
        fps=info.fps,
        bitrate_mbps=info.bitrate_mbps,
        width=info.width,
        height=info.height,
        min_bitrate_mbps=0.01,  # testsrc is low bitrate; floor lowered for test
    )
    assert d.ok


# --- rational fps parsing (pure-ish helper) -----------------------------------


def test_parse_rational_fps_ntsc():
    assert abs(ffprobe._parse_rational_fps("30000/1001") - 29.97) < 0.01


def test_parse_rational_fps_zero_denominator_guarded():
    assert ffprobe._parse_rational_fps("0/0") is None
    assert ffprobe._parse_rational_fps("30/0") is None


def test_parse_rational_fps_plain_number():
    assert ffprobe._parse_rational_fps("25") == 25.0


def test_parse_rational_fps_empty():
    assert ffprobe._parse_rational_fps(None) is None
    assert ffprobe._parse_rational_fps("") is None


def test_first_stream_picks_by_codec_type():
    streams = [
        {"codec_type": "audio", "codec_name": "aac"},
        {"codec_type": "video", "codec_name": "h264"},
    ]
    # must not assume index 0 is video
    assert ffprobe._first_stream(streams, "video")["codec_name"] == "h264"


# --- black / silence detection ------------------------------------------------


def test_blackdetect_finds_interval(black_video):
    intervals = ffprobe.detect_black_segments(black_video)
    assert intervals, "expected at least one black interval"
    # the black half starts ~1.0s in
    starts = [s for s, _ in intervals]
    assert any(s >= 0.8 for s in starts)


def test_silencedetect_finds_silence(silent_video):
    intervals = ffprobe.detect_silence(silent_video, noise_db=-20.0)
    assert intervals, "expected a silence interval on a silent track"


# --- error paths --------------------------------------------------------------


def test_missing_binary_raises_dependency_error(monkeypatch):
    monkeypatch.setattr(ffprobe.shutil, "which", lambda _name: None)
    with pytest.raises(DependencyError) as exc:
        ffprobe.probe("whatever.mp4")
    assert exc.value.exit_code == 3


def test_probe_nonexistent_file_raises_external(tmp_path):
    with pytest.raises(ExternalServiceError):
        ffprobe.probe(tmp_path / "does-not-exist.mp4")


def test_timeout_kills_process_group_no_zombie(monkeypatch):
    """A hanging child must be killed via its process group on timeout, leaving
    no zombie. We point ffprobe at `sleep` and use a 1s timeout."""
    sleep_bin = shutil.which("sleep")
    assert sleep_bin

    # Replace _require so any binary lookup returns the sleeping command,
    # and feed _run an argv that simply sleeps far longer than the timeout.
    monkeypatch.setattr(ffprobe, "_require", lambda _name: sleep_bin)

    with pytest.raises(ExternalServiceError) as exc:
        # probe builds its own argv, but the binary is `sleep`; sleep ignores the
        # flags and just... won't sleep. So call _run directly for a real hang.
        ffprobe._run([sleep_bin, "30"], timeout=1)
    assert "timed out" in str(exc.value).lower()

    # No zombie: reaping our own children should report none waiting.
    try:
        pid, _status = os.waitpid(-1, os.WNOHANG)
        # pid==0 means no child changed state; any positive pid would be a
        # lingering child we failed to reap.
        assert pid == 0
    except ChildProcessError:
        pass  # no child processes at all -> also fine
