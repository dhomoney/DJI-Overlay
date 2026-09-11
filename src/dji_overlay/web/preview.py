"""Single-frame previews: one still from the clip with the HUD drawn over it.

This is what makes the web UI worth having. Units, opacity and the home point
all change how the overlay reads against real footage, and finding that out
after a twenty minute 4K render is no way to work.

Two things keep a preview interactive:

* it is rendered at preview size rather than 4K, and the template scales from
  the frame height, so the layout is the one the final render will produce;
* the extracted still is cached on disk, so dragging a unit dropdown re-composites
  an image that is already there instead of seeking the file again.

Playwright's synchronous API binds its objects to the thread that created them
and refuses to run inside an event loop, so every preview is funnelled through
one dedicated worker thread that owns Chromium for the life of the server.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from ..probe import VideoInfo
from ..render.browser import HudRenderer
from ..render.hud import frame_values, widest_values
from ..render.pipeline import RenderOptions
from ..srt import parse_srt
from ..telemetry import Sample, Track, build_track

__all__ = ["PreviewError", "PreviewService"]

DEFAULT_WIDTH = 1280
MAX_RENDERERS = 2
"""Chromium instances kept alive. One per distinct preview size is plenty."""


class PreviewError(RuntimeError):
    """A preview could not be produced."""


@dataclass(frozen=True, slots=True)
class _Size:
    width: int
    height: int


class PreviewService:
    """Renders preview frames on a thread that owns its own Chromium."""

    def __init__(self, width: int = DEFAULT_WIDTH, quality: int = 4) -> None:
        self.width = width
        self.quality = quality
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dji-preview")
        self._renderers: dict[tuple[int, int, str], HudRenderer] = {}
        self._tracks: dict[tuple[str, int, tuple | None], Track] = {}
        self._still_dir = Path(tempfile.mkdtemp(prefix="dji-overlay-preview-"))
        self._stills: list[Path] = []
        self._lock = threading.Lock()
        self._closed = False

    # -- public ---------------------------------------------------------------

    def frame(
        self,
        video: Path,
        srt: Path,
        info: VideoInfo,
        options: RenderOptions,
        at: float,
    ) -> bytes:
        """JPEG bytes of the clip at ``at`` seconds with the HUD composited on."""
        if self._closed:
            raise PreviewError("The preview service is shutting down")
        return self._pool.submit(self._frame, video, srt, info, options, at).result()

    def close(self) -> None:
        """Tear down Chromium on the thread that opened it, then the thread."""
        if self._closed:
            return
        self._closed = True
        # Shutdown is best effort: a Chromium that has already died is not
        # a reason to fail a clean exit.
        with suppress(Exception):
            self._pool.submit(self._close_renderers).result(timeout=10)
        self._pool.shutdown(wait=False)
        shutil.rmtree(self._still_dir, ignore_errors=True)

    # -- worker thread --------------------------------------------------------

    def _frame(
        self, video: Path, srt: Path, info: VideoInfo, options: RenderOptions, at: float
    ) -> bytes:
        size = self._preview_size(info)
        track = self._track(srt, options)
        sample = _sample_at(track, at, options.srt_offset, info.fps or track.fps or 30.0)

        renderer = self._renderer(size, options.template)
        if renderer.opacity != options.opacity:
            renderer.configure(opacity=options.opacity)
        region = renderer.measure(widest_values(options.units))
        hud = renderer.render(
            frame_values(sample, track, options.units, show_msl=options.show_msl)
        )

        still = self._still(video, at, size)
        return _composite(still, hud, region.x, region.y, self.quality)

    def _preview_size(self, info: VideoInfo) -> _Size:
        width = min(self.width, info.width)
        height = round(width * info.height / info.width)
        # ffmpeg's yuv encoders want even dimensions, and so does the overlay.
        return _Size(width - (width % 2), height - (height % 2))

    def _renderer(self, size: _Size, template: str) -> HudRenderer:
        key = (size.width, size.height, template)
        renderer = self._renderers.get(key)
        if renderer is None:
            while len(self._renderers) >= MAX_RENDERERS:
                oldest = self._renderers.pop(next(iter(self._renderers)))
                oldest.__exit__(None, None, None)
            renderer = HudRenderer(size.width, size.height, template=template)
            renderer.__enter__()
            self._renderers[key] = renderer
        return renderer

    def _track(self, srt: Path, options: RenderOptions) -> Track:
        stat = srt.stat()
        key = (str(srt), int(stat.st_mtime), options.telemetry.home)
        track = self._tracks.get(key)
        if track is None:
            track = build_track(parse_srt(srt), options.telemetry)
            self._tracks = {key: track}  # one clip at a time; tracks are large
        return track

    def _still(self, video: Path, at: float, size: _Size) -> Path:
        """Extract (and keep) one frame of the source at preview size."""
        stamp = round(at, 2)
        digest = hashlib.sha1(
            f"{video}:{video.stat().st_mtime_ns}:{stamp}:{size.width}".encode()
        ).hexdigest()[:16]
        path = self._still_dir / f"{digest}.png"
        if path.exists():
            return path

        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(0.0, stamp):.3f}", "-i", str(video),
            "-frames:v", "1", "-vf", f"scale={size.width}:{size.height}",
            "-y", str(path),
        ]
        result = subprocess.run(command, capture_output=True, check=False)
        if result.returncode != 0 or not path.exists():
            raise PreviewError(
                f"Could not read a frame at {stamp:.2f}s: "
                f"{result.stderr.decode(errors='replace').strip()}"
            )

        self._stills.append(path)
        while len(self._stills) > 32:
            self._stills.pop(0).unlink(missing_ok=True)
        return path

    def _close_renderers(self) -> None:
        for renderer in self._renderers.values():
            renderer.__exit__(None, None, None)
        self._renderers.clear()


def _sample_at(track: Track, at: float, offset: int, fps: float) -> Sample:
    """The telemetry sample shown on the frame at ``at`` seconds.

    DJI writes one SRT block per frame, so the frame number is the sample
    index. Using the same mapping the render uses -- offset included -- is what
    stops the preview disagreeing with the finished file.
    """
    index = round(at * fps) + offset
    return track.samples[max(0, min(index, len(track.samples) - 1))]


def _composite(still: Path, hud: bytes, x: int, y: int, quality: int) -> bytes:
    """Overlay the HUD band on the still and return a JPEG."""
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(still),
        "-f", "image2pipe", "-i", "pipe:0",
        "-filter_complex", f"[0:v][1:v]overlay=x={x}:y={y}:format=auto",
        "-frames:v", "1", "-f", "mjpeg", "-q:v", str(quality), "pipe:1",
    ]
    result = subprocess.run(command, input=hud, capture_output=True, check=False)
    if result.returncode != 0 or not result.stdout:
        raise PreviewError(
            f"Compositing the preview failed: "
            f"{result.stderr.decode(errors='replace').strip()}"
        )
    return result.stdout
