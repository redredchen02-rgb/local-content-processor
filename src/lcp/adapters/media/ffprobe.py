"""ffprobe / ffmpeg adapter for untrusted media (I/O + subprocess).

Hardening rules (plan External References + Unit 5):
  * argv list, **never** ``shell=True``.
  * ``env=minimal_env()`` so the media parser cannot inherit our secrets.
  * ``-nostdin`` so a hung decoder never blocks on stdin.
  * ``start_new_session=True`` puts the child in its own process group; on
    timeout we ``os.killpg`` the whole group so no grandchild/zombie survives.
  * ``-t <sec>`` caps how much of a hostile file ffmpeg will chew through.
  * Missing ``ffprobe``/``ffmpeg`` binary -> :class:`DependencyError` (exit 3).

Returns plain data (dataclasses / lists); pass/needs_revision *decisions* are
made by :mod:`lcp.core.rules.asset_rules`, not here.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
from dataclasses import dataclass, field
from typing import Any

from lcp.core.errors import DependencyError, ExternalServiceError
from lcp.runtime_hardening import minimal_env

# Default cap (seconds) on how much of an untrusted file we let ffmpeg scan.
DEFAULT_ANALYZE_SECONDS = 60
# Wall-clock timeout for a probe/detect run.
DEFAULT_TIMEOUT_SECONDS = 30

_FFPROBE = "ffprobe"
_FFMPEG = "ffmpeg"

# ffmpeg blackdetect emits: black_start:1.0 black_end:2.0 black_duration:1.0
_BLACK_RE = re.compile(
    r"black_start:(?P<start>[\d.]+)\s+black_end:(?P<end>[\d.]+)"
)
# silencedetect emits silence_start / silence_end on separate lines.
_SILENCE_START_RE = re.compile(r"silence_start:\s*(?P<start>-?[\d.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(?P<end>-?[\d.]+)")


@dataclass
class VideoInfo:
    """Probed facts about a video file. Any field may be ``None`` if absent."""

    codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    duration_s: float | None = None
    bitrate_mbps: float | None = None
    has_audio: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


def _require(binary: str) -> str:
    path = shutil.which(binary)
    if path is None:
        raise DependencyError(
            f"required media tool {binary!r} not found on PATH (install ffmpeg)"
        )
    return path


def _run(argv: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """Run a hardened subprocess. On timeout, kill the whole process group so no
    child/grandchild lingers; re-raise as :class:`ExternalServiceError`."""
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=minimal_env(),
        start_new_session=True,  # own process group -> killpg works
    )
    try:
        out, err = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        raise ExternalServiceError(
            f"media tool timed out after {timeout}s: {argv[0]}"
        ) from None
    except BaseException:
        _kill_group(proc)
        raise


def _kill_group(proc: subprocess.Popen[str]) -> None:
    """Best-effort kill of the child's entire process group, then reap it."""
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def _parse_rational_fps(value: str | None) -> float | None:
    """Parse an ffprobe rational frame rate like ``"30000/1001"``. Guards a
    zero denominator and ``"0/0"`` (-> None)."""
    if not value:
        return None
    value = value.strip()
    try:
        if "/" in value:
            num_s, den_s = value.split("/", 1)
            num, den = float(num_s), float(den_s)
            if den == 0:
                return None
            if num == 0:
                return None
            return num / den
        f = float(value)
        return f if f > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def _first_stream(
    streams: list[dict[str, Any]], codec_type: str
) -> dict[str, Any] | None:
    """Return the first stream of the given codec_type (don't assume index 0
    is video)."""
    for s in streams:
        if s.get("codec_type") == codec_type:
            return s
    return None


def probe(
    file_path: str | os.PathLike[str],
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> VideoInfo:
    """Run ``ffprobe -show_format -show_streams`` and parse the JSON into a
    :class:`VideoInfo`. Tolerant of missing fields (uses ``.get`` everywhere)
    and of duration/bitrate living in either the format or the stream block."""
    binary = _require(_FFPROBE)
    argv = [
        binary,
        "-v",
        "error",
        "-hide_banner",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-i",
        os.fspath(file_path),
    ]
    result = _run(argv, timeout)
    if result.returncode != 0:
        raise ExternalServiceError(
            f"ffprobe failed (rc={result.returncode}): "
            f"{(result.stderr or '').strip()[:300]}"
        )
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as e:
        raise ExternalServiceError(f"ffprobe returned non-JSON output: {e}") from e

    fmt = data.get("format", {}) or {}
    streams = data.get("streams", []) or []
    video = _first_stream(streams, "video") or {}
    audio = _first_stream(streams, "audio")

    # fps: prefer avg_frame_rate, fall back to r_frame_rate.
    fps = _parse_rational_fps(video.get("avg_frame_rate")) or _parse_rational_fps(
        video.get("r_frame_rate")
    )

    # duration may be on the stream or the format block.
    duration = _to_float(video.get("duration")) or _to_float(fmt.get("duration"))

    # bitrate likewise; convert bits/s -> Mbps.
    bitrate_bps = _to_float(video.get("bit_rate")) or _to_float(fmt.get("bit_rate"))
    bitrate_mbps = (bitrate_bps / 1_000_000) if bitrate_bps else None

    return VideoInfo(
        codec=video.get("codec_name"),
        width=_to_int(video.get("width")),
        height=_to_int(video.get("height")),
        fps=fps,
        duration_s=duration,
        bitrate_mbps=bitrate_mbps,
        has_audio=audio is not None,
        raw=data,
    )


def detect_black_segments(
    file_path: str | os.PathLike[str],
    *,
    analyze_seconds: int = DEFAULT_ANALYZE_SECONDS,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    pic_th: float = 0.98,
) -> list[tuple[float, float]]:
    """Run ffmpeg ``blackdetect`` and parse ``(start, end)`` intervals from
    stderr. ``-t`` caps the scan; output goes to null."""
    binary = _require(_FFMPEG)
    argv = [
        binary,
        "-nostdin",
        "-hide_banner",
        "-t",
        str(analyze_seconds),
        "-i",
        os.fspath(file_path),
        "-vf",
        f"blackdetect=d=0.1:pic_th={pic_th}",
        "-an",
        "-f",
        "null",
        "-",
    ]
    result = _run(argv, timeout)
    intervals: list[tuple[float, float]] = []
    for m in _BLACK_RE.finditer(result.stderr or ""):
        intervals.append((float(m.group("start")), float(m.group("end"))))
    return intervals


def detect_silence(
    file_path: str | os.PathLike[str],
    *,
    analyze_seconds: int = DEFAULT_ANALYZE_SECONDS,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    noise_db: float = -30.0,
    min_silence: float = 0.5,
) -> list[tuple[float, float | None]]:
    """Run ffmpeg ``silencedetect`` and pair start/end markers from stderr.

    A trailing ``silence_start`` with no matching ``silence_end`` (silence runs
    to EOF) yields ``(start, None)``.
    """
    binary = _require(_FFMPEG)
    argv = [
        binary,
        "-nostdin",
        "-hide_banner",
        "-t",
        str(analyze_seconds),
        "-i",
        os.fspath(file_path),
        "-af",
        f"silencedetect=noise={noise_db}dB:d={min_silence}",
        "-vn",
        "-f",
        "null",
        "-",
    ]
    result = _run(argv, timeout)
    stderr = result.stderr or ""

    intervals: list[tuple[float, float | None]] = []
    pending_start: float | None = None
    for line in stderr.splitlines():
        ms = _SILENCE_START_RE.search(line)
        if ms:
            pending_start = float(ms.group("start"))
            continue
        me = _SILENCE_END_RE.search(line)
        if me and pending_start is not None:
            intervals.append((pending_start, float(me.group("end"))))
            pending_start = None
    if pending_start is not None:
        intervals.append((pending_start, None))
    return intervals


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f if f > 0 else (f if f != 0 else None)


def _to_int(value: object) -> int | None:
    if not isinstance(value, (str, int, float)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
