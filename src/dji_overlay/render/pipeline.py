"""Drives Chromium and ffmpeg to produce the finished video."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ..probe import VideoInfo, check_alignment, probe
from ..srt import parse_srt
from ..telemetry import Sample, TelemetryOptions, Track, build_track
from ..units import UnitPrefs
from .browser import CaptureRegion, HudRenderer
from .hud import frame_values, widest_values

__all__ = [
    "MODES",
    "RenderCancelled",
    "RenderOptions",
    "RenderResult",
    "detect_encoder",
    "render",
]

MODES = ("burn", "alpha")


class RenderCancelled(RuntimeError):
    """Raised when a caller asked for the render to stop part way through."""


@dataclass(slots=True)
class RenderOptions:
    mode: str = "burn"
    """'burn' composites onto the video; 'alpha' writes a transparent track."""
    template: str = "dji_goggles"
    units: UnitPrefs = field(default_factory=UnitPrefs)
    telemetry: TelemetryOptions = field(default_factory=TelemetryOptions)
    opacity: float = 1.0
    show_msl: bool = True
    encoder: str = "auto"
    crf: int = 18
    preset: str = "medium"
    srt_offset: int = 0
    """Frames to shift telemetry by, for footage that was trimmed after recording."""
    start: float = 0.0
    duration: float | None = None
    alpha_codec: str = "prores"
    """'prores' (.mov, ProRes 4444) or 'png' (a numbered image sequence)."""
    hud_refresh_hz: float = 10.0
    """How often the readouts change.

    Derived values vary continuously, so at 30 fps almost every frame would be a
    distinct HUD state and Chromium would have to draw all of them. Holding each
    reading for a fraction of a second collapses those into cache hits, and it
    matches the real hardware: DJI's goggles refresh a few times a second, not
    once per frame. Set to 0 to render every frame individually.
    """


@dataclass(slots=True)
class RenderResult:
    output: Path
    frames: int
    unique_hud_states: int
    region: CaptureRegion
    warnings: list[str]
    info: VideoInfo
    track: Track


def encoder_works(name: str, ffmpeg: str | None = None) -> bool:
    """Check that an encoder can actually run, not merely that it was compiled in.

    ``ffmpeg -encoders`` lists every encoder the binary was built with, which is
    not the same thing as one that works here. A container without GPU
    passthrough still advertises hevc_nvenc and then fails at the point of
    encoding with "Cannot load libcuda.so.1" -- after the whole render has been
    pushed through Chromium. A one-frame trial encode costs milliseconds and
    turns that into a clean fallback.
    """
    ffmpeg = ffmpeg or shutil.which("ffmpeg")
    if ffmpeg is None:
        return False
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=black:s=64x64:r=1:d=0.1",
         "-frames:v", "1", "-c:v", name, "-f", "null", "-"],
        capture_output=True, check=False,
    )
    return result.returncode == 0


def detect_encoder(preferred: str, video_codec: str) -> str:
    """Pick a video encoder, using NVENC when it is genuinely usable."""
    if preferred != "auto":
        return preferred
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return "libx264"
    # Match the source codec so a HEVC clip stays HEVC rather than silently
    # dropping to H.264 at the same CRF.
    wanted = "hevc" if video_codec in ("hevc", "h265") else "h264"
    software = "libx265" if wanted == "hevc" else "libx264"
    for candidate in (f"{wanted}_nvenc", software):
        if encoder_works(candidate, ffmpeg):
            return candidate
    return "libx264"


def _quality_args(encoder: str, crf: int, preset: str) -> list[str]:
    if encoder.endswith("_nvenc"):
        # NVENC has no CRF; -cq is the closest equivalent.
        return ["-preset", "p5", "-rc", "vbr", "-cq", str(crf), "-b:v", "0"]
    return ["-crf", str(crf), "-preset", preset]


def _sample_stream(
    track: Track, frames: int, first_sample: int, offset: int, hold: int = 1
) -> Iterator[Sample]:
    """One sample per output frame, holding the last one if telemetry runs short.

    ``hold`` repeats each sample for that many frames so the readouts settle
    instead of flickering, which also makes the render cache effective.
    """
    last = track.samples[-1]
    hold = max(1, hold)
    for i in range(frames):
        index = first_sample + (i // hold) * hold + offset
        if index < 0:
            yield track.samples[0]
        elif index < len(track.samples):
            yield track.samples[index]
        else:
            yield last


def _burn_command(
    info: VideoInfo, region: CaptureRegion, fps: float, options: RenderOptions,
    encoder: str, out: Path,
) -> list[str]:
    args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if options.start:
        args += ["-ss", str(options.start)]
    if options.duration is not None:
        args += ["-t", str(options.duration)]
    args += ["-i", str(info.path)]
    args += ["-f", "image2pipe", "-framerate", f"{fps:.6f}", "-i", "pipe:0"]
    args += [
        "-filter_complex",
        f"[0:v][1:v]overlay=x={region.x}:y={region.y}:format=auto[v]",
        "-map", "[v]",
        "-map", "0:a?",     # keep the camera's own audio if the file has any
        "-c:a", "copy",
        "-c:v", encoder,
        *_quality_args(encoder, options.crf, options.preset),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(out),
    ]
    return args


def _alpha_command(
    info: VideoInfo, region: CaptureRegion, fps: float, options: RenderOptions, out: Path,
) -> list[str]:
    # The band is padded back to full frame size so the overlay drops straight
    # onto a timeline in register with the footage.
    pad = (
        f"pad={info.width}:{info.height}:{region.x}:{region.y}:color=#00000000[v]"
    )
    args = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "image2pipe", "-framerate", f"{fps:.6f}", "-i", "pipe:0",
        "-filter_complex", f"[0:v]{pad}",
        "-map", "[v]",
    ]
    if options.alpha_codec == "png":
        args += ["-c:v", "png", str(out)]
    else:
        # ProRes 4444 is what MLT (and therefore kdenlive) decodes with alpha
        # intact; VP9 alpha in WebM does not survive the round trip reliably.
        args += [
            "-c:v", "prores_ks", "-profile:v", "4444",
            "-pix_fmt", "yuva444p10le", "-vendor", "apl0",
            "-alpha_bits", "16",
            str(out),
        ]
    return args


def render(
    video_path: str | Path,
    srt_path: str | Path,
    out_path: str | Path,
    options: RenderOptions | None = None,
    progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> RenderResult:
    """Render the overlay for one clip.

    ``cancelled`` is polled every so many frames; when it returns True the
    render stops, ffmpeg is torn down and the partial output is removed. A 4K
    clip takes minutes, so anything driving this from a UI needs a way out.
    """
    options = options or RenderOptions()
    if options.mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {options.mode!r}")

    video_path, srt_path, out_path = Path(video_path), Path(srt_path), Path(out_path)
    info = probe(video_path)
    records = parse_srt(srt_path)
    track = build_track(records, options.telemetry)
    warnings = check_alignment(info, len(track), track.fps)

    fps = info.fps or track.fps or 30.0
    first_sample = round(options.start * fps)
    if options.duration is not None:
        frames = min(round(options.duration * fps), info.frame_count - first_sample)
    else:
        frames = info.frame_count - first_sample
    frames = max(0, frames)

    out_path.parent.mkdir(parents=True, exist_ok=True)

    with HudRenderer(
        info.width, info.height, template=options.template, opacity=options.opacity
    ) as renderer:
        region = renderer.measure(widest_values(options.units))

        command = (
            _burn_command(info, region, fps, options,
                          detect_encoder(options.encoder, info.codec), out_path)
            if options.mode == "burn"
            else _alpha_command(info, region, fps, options, out_path)
        )

        process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        assert process.stdin is not None
        done = 0
        stopped = False
        try:
            hold = (
                max(1, round(fps / options.hud_refresh_hz))
                if options.hud_refresh_hz
                else 1
            )
            for i, sample in enumerate(
                _sample_stream(track, frames, first_sample, options.srt_offset, hold)
            ):
                done = i
                if i % 15 == 0:
                    if cancelled is not None and cancelled():
                        stopped = True
                        break
                    if progress is not None:
                        progress(i, frames)
                png = renderer.render(
                    frame_values(sample, track, options.units, show_msl=options.show_msl)
                )
                process.stdin.write(png)
            process.stdin.close()
        except BrokenPipeError:
            pass  # ffmpeg died; its stderr below explains why

        if stopped:
            process.terminate()
            process.wait()
            if process.stderr is not None:
                process.stderr.read()
            # A partial file is worse than none: it looks like a finished render.
            if "%" not in out_path.name:
                out_path.unlink(missing_ok=True)
            raise RenderCancelled(f"Cancelled after {done:,} of {frames:,} frames")

        stderr = process.stderr.read().decode(errors="replace") if process.stderr else ""
        code = process.wait()
        if code != 0:
            raise RuntimeError(f"ffmpeg exited with {code}:\n{stderr.strip()}")

        if progress is not None:
            progress(frames, frames)

        return RenderResult(
            output=out_path,
            frames=frames,
            unique_hud_states=renderer.cache_size,
            region=region,
            warnings=warnings,
            info=info,
            track=track,
        )
