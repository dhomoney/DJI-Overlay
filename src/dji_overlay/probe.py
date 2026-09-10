"""ffprobe wrapper and SRT/video alignment checks."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

__all__ = ["FFmpegMissingError", "VideoInfo", "check_alignment", "probe"]


class FFmpegMissingError(RuntimeError):
    """ffmpeg or ffprobe is not installed or not on PATH."""


@dataclass(slots=True)
class VideoInfo:
    path: Path
    width: int
    height: int
    fps: float
    duration: float
    frame_count: int
    codec: str
    pix_fmt: str
    has_audio: bool
    is_log: bool
    """True when the footage looks like D-Log/HLG, where burning in a bright HUD
    will not look the way it does on a Rec.709 preview."""

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"


def _require(tool: str) -> str:
    path = shutil.which(tool)
    if path is None:
        raise FFmpegMissingError(
            f"{tool} was not found on PATH. Install ffmpeg, or use the Docker "
            f"image, which bundles it."
        )
    return path


def probe(path: str | Path) -> VideoInfo:
    """Read stream metadata for a video file."""
    path = Path(path)
    ffprobe = _require("ffprobe")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-print_format", "json", "-show_format",
         "-show_streams", str(path)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {result.stderr.strip()}")

    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and s.get("disposition", {}).get("attached_pic", 0) == 0), None)
    if video is None:
        raise RuntimeError(f"No video stream found in {path}")

    fps = float(Fraction(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0"))
    duration = float(video.get("duration") or data.get("format", {}).get("duration") or 0.0)
    frame_count = int(video.get("nb_frames") or 0) or round(duration * fps)

    transfer = (video.get("color_transfer") or "").lower()
    primaries = (video.get("color_primaries") or "").lower()
    is_log = any(
        marker in transfer or marker in primaries
        for marker in ("log", "arib-std-b67", "smpte2084", "bt2020")
    )

    return VideoInfo(
        path=path,
        width=int(video["width"]),
        height=int(video["height"]),
        fps=fps,
        duration=duration,
        frame_count=frame_count,
        codec=video.get("codec_name", "?"),
        pix_fmt=video.get("pix_fmt", "?"),
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
        is_log=is_log,
    )


def check_alignment(info: VideoInfo, sample_count: int, srt_fps: float | None) -> list[str]:
    """Warn when the SRT does not line up with the video.

    DJI writes one SRT block per frame, so a mismatch means the video was
    trimmed, joined or re-timed after recording -- in which case every readout
    lands on the wrong frame. That failure is silent and produces a plausible
    looking but wrong overlay, so it is worth being loud about.
    """
    warnings: list[str] = []

    if info.frame_count and sample_count:
        drift = abs(info.frame_count - sample_count)
        if drift > max(2, info.frame_count * 0.001):
            warnings.append(
                f"The video has {info.frame_count:,} frames but the SRT has "
                f"{sample_count:,} samples (a difference of {drift:,}). The clip was "
                f"probably trimmed or re-encoded after recording; telemetry will be "
                f"offset. Use --srt-offset to correct it."
            )

    if srt_fps and info.fps and abs(srt_fps - info.fps) > 0.5:
        warnings.append(
            f"The SRT implies {srt_fps:.2f} fps but the video is {info.fps:.2f} fps. "
            f"Telemetry will drift over the length of the clip."
        )

    if info.is_log:
        warnings.append(
            "This footage looks like log or HDR. A HUD burned in now will be graded "
            "along with the image; consider the transparent overlay export instead."
        )

    return warnings
