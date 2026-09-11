"""The HTTP layer: a thin shell over the library, the preview and the queue.

Everything here is translation. Paths come in from the browser and go through
the library, settings come in as JSON and go out as a RenderOptions, and job
events come back the other way as SSE. No rendering logic lives at this level.
"""

from __future__ import annotations

import asyncio
import json
import queue as queuelib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..probe import FFmpegMissingError, check_alignment
from ..render.browser import available_templates
from ..render.pipeline import MODES, RenderOptions
from ..srt import SrtParseError, parse_srt
from ..telemetry import TelemetryOptions, build_track
from ..units import (
    ALTITUDE_UNITS,
    DISTANCE_UNITS,
    SPEED_UNITS,
    VSPEED_UNITS,
    UnitPrefs,
    format_duration,
)
from .jobs import JobQueue, Runner
from .library import Library, LibraryError
from .preview import DEFAULT_WIDTH, PreviewError, PreviewService

__all__ = ["RenderSettings", "create_app"]

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _version() -> str:
    try:
        return package_version("dji-overlay")
    except PackageNotFoundError:  # running from a source tree without an install
        return "0.0.0"


@dataclass
class RenderSettings:
    """Everything the UI can change about a render.

    A dataclass rather than a model so it binds to query parameters for the
    preview and to a JSON body for a job, without writing it out twice.
    """

    mode: Literal["burn", "alpha"] = "burn"
    template: str = "dji_goggles"
    altitude_unit: str = "ft"
    speed_unit: str = "mph"
    vspeed_unit: str = "mph"
    distance_unit: str = "ft"
    show_msl: bool = True
    home: str | None = None
    """'lat,lon' to override the auto-detected home point."""
    opacity: float = 1.0
    encoder: str = "auto"
    crf: int = 18
    preset: str = "medium"
    alpha_codec: Literal["prores", "png"] = "prores"
    srt_offset: int = 0
    refresh_hz: float = 10.0
    start: float = 0.0
    duration: float | None = None

    def to_options(self) -> RenderOptions:
        try:
            units = UnitPrefs(
                altitude=self.altitude_unit,
                speed=self.speed_unit,
                vspeed=self.vspeed_unit,
                distance=self.distance_unit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        if self.mode not in MODES:
            raise HTTPException(status_code=422, detail=f"Unknown mode {self.mode!r}")
        if not 0.1 <= self.opacity <= 1.0:
            raise HTTPException(status_code=422, detail="Opacity must be between 0.1 and 1")

        return RenderOptions(
            mode=self.mode,
            template=self.template,
            units=units,
            telemetry=TelemetryOptions(home=_parse_home(self.home)),
            opacity=self.opacity,
            show_msl=self.show_msl,
            encoder=self.encoder,
            crf=self.crf,
            preset=self.preset,
            alpha_codec=self.alpha_codec,
            srt_offset=self.srt_offset,
            hud_refresh_hz=self.refresh_hz,
            start=self.start,
            duration=self.duration,
        )


def _parse_home(home: str | None) -> tuple[float, float] | None:
    if not home or not home.strip():
        return None
    try:
        latitude, longitude = (float(part) for part in home.split(","))
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="Home expects 'lat,lon' in decimal degrees"
        ) from exc
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise HTTPException(status_code=422, detail="Home is not a valid coordinate")
    return latitude, longitude


class JobRequest(BaseModel):
    path: str
    out: str | None = None
    """Output path relative to the media root; defaults to the CLI's naming."""
    settings: RenderSettings = Field(default_factory=RenderSettings)


def create_app(
    media_root: str | Path,
    *,
    preview_width: int = DEFAULT_WIDTH,
    runner: Runner | None = None,
    previews: PreviewService | None = None,
) -> FastAPI:
    """Build the application around one media directory."""
    library = Library(media_root)
    jobs = JobQueue(runner=runner)
    preview_service = previews if previews is not None else PreviewService(preview_width)
    tracks: dict[tuple, Any] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        jobs.shutdown()
        preview_service.close()

    app = FastAPI(title="DJI-Overlay", version=_version(), lifespan=lifespan)
    app.state.library = library
    app.state.jobs = jobs
    app.state.previews = preview_service

    @app.exception_handler(LibraryError)
    async def _library_error(_: Request, exc: LibraryError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(SrtParseError)
    async def _srt_error(_: Request, exc: SrtParseError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(FFmpegMissingError)
    async def _ffmpeg_error(_: Request, exc: FFmpegMissingError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(PreviewError)
    async def _preview_error(_: Request, exc: PreviewError) -> JSONResponse:
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    # -- metadata -------------------------------------------------------------

    @app.get("/api/config")
    def config() -> dict:
        return {
            "version": _version(),
            "media_root": str(library.root),
            "templates": available_templates(),
            "modes": list(MODES),
            "alpha_codecs": ["prores", "png"],
            "units": {
                "altitude": list(ALTITUDE_UNITS),
                "speed": list(SPEED_UNITS),
                "vspeed": list(VSPEED_UNITS),
                "distance": list(DISTANCE_UNITS),
            },
            "defaults": asdict(RenderSettings()),
            "preview_width": preview_width,
        }

    @app.get("/api/library")
    def library_listing() -> dict:
        clips = library.clips()
        entries = []
        for clip in clips:
            try:
                info = library.info(library.resolve(clip.path))
            except Exception:  # noqa: BLE001 - an unreadable file still gets listed
                info = None
            entries.append(clip.as_dict(info))
        return {"root": str(library.root), "clips": entries}

    @app.get("/api/flight")
    def flight(path: str, home: str | None = None) -> dict:
        """What the SRT says about this clip, for the panel above the preview."""
        video = library.video(path)
        srt = library.sidecar(video)
        if srt is None:
            raise HTTPException(
                status_code=404,
                detail=f"No .SRT next to {video.name}. DJI writes one per clip; "
                "without it there is nothing to overlay.",
            )
        info = library.info(video)
        track = _track(srt, _parse_home(home))
        first = track.samples[0]
        return {
            "path": path,
            "srt": library.relative(srt),
            "samples": len(track),
            "duration": round(track.duration, 3),
            "fps": round(track.fps, 3) if track.fps else None,
            "recorded": first.timestamp.isoformat() if first.timestamp else None,
            "home": list(track.home) if track.home else None,
            "home_index": track.home_index,
            "gps_rate": round(track.gps_rate, 2) if track.gps_rate else None,
            "alt_rate": round(track.alt_rate, 2) if track.alt_rate else None,
            "max_rel_alt": track.max_rel_alt,
            "max_speed": track.max_speed,
            "max_distance": track.max_distance,
            "path_length": track.path_length,
            "flight_time": format_duration(track.duration),
            "video": {
                "resolution": info.resolution,
                "fps": round(info.fps, 3),
                "duration": round(info.duration, 3),
                "frames": info.frame_count,
                "codec": info.codec,
                "has_audio": info.has_audio,
                "is_log": info.is_log,
            },
            "warnings": check_alignment(info, len(track), track.fps),
        }

    # -- preview --------------------------------------------------------------

    @app.get("/api/preview")
    def preview(
        path: str, settings: Annotated[RenderSettings, Depends()], t: float = 0.0
    ) -> Response:
        video = library.video(path)
        srt = library.sidecar(video)
        if srt is None:
            raise HTTPException(status_code=404, detail=f"No .SRT next to {video.name}")
        info = library.info(video)
        at = max(0.0, min(t, max(0.0, info.duration - 1 / max(info.fps, 1))))
        image = preview_service.frame(video, srt, info, settings.to_options(), at)
        return Response(
            content=image,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    # -- jobs -----------------------------------------------------------------

    @app.get("/api/jobs")
    def job_list() -> dict:
        return {"jobs": jobs.snapshot_all()}

    @app.post("/api/jobs", status_code=202)
    def create_job(request: JobRequest) -> dict:
        video = library.video(request.path)
        srt = library.sidecar(video)
        if srt is None:
            raise HTTPException(status_code=404, detail=f"No .SRT next to {video.name}")

        options = request.settings.to_options()
        if request.out:
            out_path = library.resolve(request.out)
        else:
            out_path = library.default_output(
                video, options.mode, options.alpha_codec
            )
        out_path.parent.mkdir(parents=True, exist_ok=True)

        job = jobs.submit(
            clip=request.path,
            name=video.name,
            output=library.relative(out_path),
            video_path=video,
            srt_path=srt,
            out_path=out_path,
            options=options,
            settings=asdict(request.settings),
        )
        return job.snapshot()

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict:
        job = jobs.cancel(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"No job {job_id}")
        return job.snapshot()

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        channel = jobs.subscribe()

        async def stream() -> AsyncIterator[str]:
            try:
                yield _sse({"type": "snapshot", "jobs": jobs.snapshot_all()})
                while not await request.is_disconnected():
                    event = await asyncio.to_thread(_next_event, channel)
                    yield _sse(event) if event is not None else ": keepalive\n\n"
            finally:
                jobs.unsubscribe(channel)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # -- the page -------------------------------------------------------------

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    def _track(srt: Path, home: tuple[float, float] | None):
        key = (str(srt), srt.stat().st_mtime_ns, home)
        track = tracks.get(key)
        if track is None:
            track = build_track(parse_srt(srt), TelemetryOptions(home=home))
            tracks.clear()  # one clip is being worked on at a time
            tracks[key] = track
        return track

    return app


def _next_event(channel: queuelib.Queue, timeout: float = 15.0) -> dict | None:
    try:
        return channel.get(timeout=timeout)
    except queuelib.Empty:
        return None


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"
