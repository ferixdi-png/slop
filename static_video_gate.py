"""Cheap local visual-motion gate for Reels.

Instagram may expose image posts, quote cards and single-photo Reels as MP4 files.
Those must be rejected before Gemini so they neither consume model budget nor
pollute the final TOP. A bundled ffmpeg fallback makes the gate independent of the
Render host image.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
from dataclasses import dataclass

from media_duration import measure_video_duration


_SHOWINFO_RE = re.compile(r"showinfo.*?\bn:\s*\d+", re.IGNORECASE)


@dataclass(frozen=True)
class MotionGateResult:
    checked: bool
    is_static_image_video: bool
    duration_sec: float
    expected_samples: int
    retained_motion_frames: int
    retained_ratio: float
    reason: str


def ffmpeg_executable() -> str:
    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
        bundled = str(get_ffmpeg_exe() or "").strip()
        if bundled:
            return bundled
    except Exception:
        pass
    return ""


def inspect_visual_motion(path: str, sample_fps: float = 3.0) -> MotionGateResult:
    """Detect a still-image/card/slideshow MP4 without decoding frames in Python.

    ffmpeg downsizes the clip, samples a few frames per second and lets mpdecimate
    remove near-duplicate frames. Real people/camera motion keeps many samples;
    a single image or almost-static card collapses to roughly one frame.
    """
    duration = float(measure_video_duration(path, fallback=0) or 0)
    if duration <= 0:
        return MotionGateResult(False, False, 0.0, 0, 0, 1.0, "duration unavailable; fail-open")

    ffmpeg = ffmpeg_executable()
    if not ffmpeg:
        return MotionGateResult(False, False, duration, 0, 0, 1.0, "ffmpeg unavailable; fail-open")

    fps = max(1.0, min(float(sample_fps or 3.0), 4.0))
    expected = max(1, int(math.ceil(duration * fps)))
    vf = (
        f"fps={fps:.3f},"
        "scale=160:-2:flags=area,"
        "format=yuv420p,"
        "mpdecimate=hi=768:lo=320:frac=0.20,"
        "showinfo"
    )
    try:
        proc = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "info",
                "-i", path, "-an", "-vf", vf, "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            timeout=18,
            check=False,
        )
    except Exception as exc:
        return MotionGateResult(False, False, duration, expected, 0, 1.0, f"ffmpeg unavailable: {exc}")

    if proc.returncode != 0:
        tail = (proc.stderr or "")[-240:].replace("\n", " ")
        return MotionGateResult(False, False, duration, expected, 0, 1.0, f"ffmpeg failed; fail-open: {tail}")

    kept = len(_SHOWINFO_RE.findall(proc.stderr or ""))
    if kept <= 0:
        return MotionGateResult(False, False, duration, expected, 0, 1.0, "no motion samples parsed; fail-open")

    ratio = kept / max(1, expected)

    # For a normal 5–10s clip a true static card collapses to ~1 frame. A tiny
    # handful of retained frames also catches sparse image slides. Real talking
    # faces, handheld footage and actual action remain comfortably above the gate.
    threshold_frames = max(1, int(math.floor(expected * 0.12)))
    static_like = expected >= 6 and kept <= threshold_frames

    return MotionGateResult(
        True,
        bool(static_like),
        duration,
        expected,
        kept,
        round(ratio, 4),
        (
            f"static-image gate: retained {kept}/{expected} sampled frames ({ratio:.1%})"
            if static_like
            else f"motion gate passed: retained {kept}/{expected} sampled frames ({ratio:.1%})"
        ),
    )
