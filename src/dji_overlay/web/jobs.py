"""The render queue that sits behind the web UI.

Renders run on a single worker thread and report progress as events. The queue
knows nothing about HTTP: it publishes snapshots, and whoever is listening --
the SSE endpoint today, anything else later -- decides how to present them.
One worker is deliberate rather than a limitation: a render already saturates
the machine with a Chromium instance and an ffmpeg encode, and two at once
makes both slower without finishing anything sooner.
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..render.pipeline import RenderCancelled, RenderOptions, RenderResult
from ..render.pipeline import render as render_clip

__all__ = ["Job", "JobQueue", "JobState"]

Runner = Callable[..., RenderResult]


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_final(self) -> bool:
        return self in (JobState.DONE, JobState.FAILED, JobState.CANCELLED)


@dataclass(slots=True)
class Job:
    """One queued or finished render."""

    id: str
    clip: str
    """The source clip, relative to the media root."""
    name: str
    output: str
    """Where the result is being written, relative to the media root."""
    options: RenderOptions
    settings: dict[str, Any]
    """The settings as the browser sent them, echoed back so the UI can restore them."""
    video_path: Path
    srt_path: Path
    out_path: Path

    state: JobState = JobState.QUEUED
    done_frames: int = 0
    total_frames: int = 0
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    summary: dict[str, Any] | None = None
    _cancel: bool = False

    @property
    def elapsed(self) -> float:
        if self.started is None:
            return 0.0
        return (self.finished or time.time()) - self.started

    @property
    def eta(self) -> float | None:
        """Seconds remaining, from the rate so far. None until there is a rate."""
        if self.state is not JobState.RUNNING or not self.done_frames:
            return None
        rate = self.done_frames / max(self.elapsed, 1e-6)
        if rate <= 0:
            return None
        return max(0.0, (self.total_frames - self.done_frames) / rate)

    def snapshot(self) -> dict[str, Any]:
        percent = (
            100.0 * self.done_frames / self.total_frames if self.total_frames else 0.0
        )
        return {
            "id": self.id,
            "clip": self.clip,
            "name": self.name,
            "output": self.output,
            "state": self.state.value,
            "done_frames": self.done_frames,
            "total_frames": self.total_frames,
            "percent": round(percent, 1),
            "elapsed": round(self.elapsed, 1),
            "eta": round(self.eta, 1) if self.eta is not None else None,
            "created": self.created,
            "warnings": list(self.warnings),
            "error": self.error,
            "summary": self.summary,
            "settings": self.settings,
        }


class JobQueue:
    """A single-worker render queue with an event stream."""

    def __init__(self, runner: Runner | None = None, history: int = 50) -> None:
        self._runner = runner or render_clip
        self._history = history
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._pending: queue.Queue[str] = queue.Queue()
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()
        self._lock = threading.Lock()
        self._current: Job | None = None
        self._stopping = threading.Event()
        self._worker = threading.Thread(
            target=self._run, name="dji-overlay-renders", daemon=True
        )
        self._worker.start()

    # -- queue ----------------------------------------------------------------

    def submit(
        self,
        *,
        clip: str,
        name: str,
        output: str,
        video_path: Path,
        srt_path: Path,
        out_path: Path,
        options: RenderOptions,
        settings: dict[str, Any],
    ) -> Job:
        job = Job(
            id=uuid.uuid4().hex[:12],
            clip=clip,
            name=name,
            output=output,
            options=options,
            settings=settings,
            video_path=video_path,
            srt_path=srt_path,
            out_path=out_path,
        )
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._forget_old()
        self._pending.put(job.id)
        self._publish(job)
        return job

    def cancel(self, job_id: str) -> Job | None:
        """Ask a job to stop. A queued one never starts; a running one unwinds."""
        job = self._jobs.get(job_id)
        if job is None or job.state.is_final:
            return job
        job._cancel = True
        if job.state is JobState.QUEUED:
            job.state = JobState.CANCELLED
            job.finished = time.time()
            job.error = "Cancelled before it started"
            self._publish(job)
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        with self._lock:
            return [self._jobs[i] for i in self._order if i in self._jobs]

    def snapshot_all(self) -> list[dict[str, Any]]:
        return [job.snapshot() for job in reversed(self.all())]

    def shutdown(self, timeout: float = 5.0) -> None:
        self._stopping.set()
        if self._current is not None:
            self._current._cancel = True
        self._pending.put("")
        self._worker.join(timeout=timeout)

    # -- events ---------------------------------------------------------------

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        channel: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=256)
        with self._lock:
            self._subscribers.add(channel)
        return channel

    def unsubscribe(self, channel: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(channel)

    def _publish(self, job: Job) -> None:
        event = {"type": "job", "job": job.snapshot()}
        with self._lock:
            channels = list(self._subscribers)
        for channel in channels:
            try:
                channel.put_nowait(event)
            except queue.Full:
                # A listener that cannot keep up loses intermediate progress,
                # never the final state: the UI refetches on reconnect.
                pass

    # -- worker ---------------------------------------------------------------

    def _run(self) -> None:
        while not self._stopping.is_set():
            job_id = self._pending.get()
            if not job_id:
                continue
            job = self._jobs.get(job_id)
            if job is None or job.state is not JobState.QUEUED:
                continue
            self._execute(job)

    def _execute(self, job: Job) -> None:
        self._current = job
        job.state = JobState.RUNNING
        job.started = time.time()
        self._publish(job)

        last_published = 0.0

        def progress(done: int, total: int) -> None:
            nonlocal last_published
            job.done_frames, job.total_frames = done, total
            now = time.time()
            # Four updates a second is smooth to watch and cheap to send.
            if now - last_published >= 0.25:
                last_published = now
                self._publish(job)

        try:
            result = self._runner(
                job.video_path,
                job.srt_path,
                job.out_path,
                job.options,
                progress,
                cancelled=lambda: job._cancel,
            )
        except RenderCancelled as exc:
            job.state = JobState.CANCELLED
            job.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - the UI shows whatever went wrong
            job.state = JobState.FAILED
            job.error = f"{type(exc).__name__}: {exc}"
        else:
            job.state = JobState.DONE
            job.warnings = list(result.warnings)
            job.done_frames = job.total_frames = result.frames
            reuse = 1 - result.unique_hud_states / max(1, result.frames)
            job.summary = {
                "frames": result.frames,
                "hud_states": result.unique_hud_states,
                "reuse": round(reuse, 3),
                "band": f"{result.region.width}x{result.region.height}",
                "band_y": result.region.y,
            }
        finally:
            job.finished = time.time()
            self._current = None
            self._publish(job)

    def _forget_old(self) -> None:
        """Keep the job list bounded; finished jobs age out oldest first."""
        while len(self._order) > self._history:
            for index, job_id in enumerate(self._order):
                if self._jobs[job_id].state.is_final:
                    del self._order[index]
                    del self._jobs[job_id]
                    break
            else:
                return


def stream_events(
    channel: queue.Queue[dict[str, Any]], heartbeat: float = 15.0
) -> Iterator[dict[str, Any] | None]:
    """Yield events as they arrive, and None when it is time for a keepalive."""
    while True:
        try:
            yield channel.get(timeout=heartbeat)
        except queue.Empty:
            yield None
