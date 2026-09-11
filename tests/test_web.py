"""Tests for the web layer.

The library and the queue are tested directly; the HTTP surface is tested with
a stub renderer so the suite never launches Chromium for something that is
really about routing. The handful of tests that do need a real video and a real
browser are marked ``integration``.
"""

import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from dji_overlay.render.pipeline import RenderCancelled, RenderOptions
from dji_overlay.web.app import RenderSettings, create_app
from dji_overlay.web.jobs import JobQueue, JobState
from dji_overlay.web.library import Library, LibraryError

FIXTURE = Path(__file__).parent / "fixtures" / "neo2_landing.srt"


@pytest.fixture
def media(tmp_path):
    """A media directory shaped like a card pulled out of a drone."""
    root = tmp_path / "media"
    (root / "2026-08-31").mkdir(parents=True)
    (root / "DJI_0001.MP4").write_bytes(b"not really a video")
    shutil.copy(FIXTURE, root / "DJI_0001.SRT")
    (root / "DJI_0002.MP4").write_bytes(b"no telemetry next to this one")
    (root / "DJI_0001_hud.mp4").write_bytes(b"a previous render")
    (root / "2026-08-31" / "DJI_0009.MP4").write_bytes(b"nested")
    shutil.copy(FIXTURE, root / "2026-08-31" / "DJI_0009.SRT")
    return root


class StubPreviews:
    """Stands in for Chromium: records the call, returns a JPEG header."""

    def __init__(self):
        self.calls = []

    def frame(self, video, srt, info, options, at):
        self.calls.append({"video": video, "srt": srt, "options": options, "at": at})
        return b"\xff\xd8\xff\xe0stub"

    def close(self):
        pass


class StubRunner:
    """Stands in for the render pipeline."""

    def __init__(self, frames=300, fail=None, block=None):
        self.frames = frames
        self.fail = fail
        self.block = block
        self.calls = []

    def __call__(self, video, srt, out, options, progress=None, cancelled=None):
        self.calls.append((Path(video), Path(srt), Path(out), options))
        if self.fail is not None:
            raise self.fail
        for done in range(0, self.frames, 15):
            if cancelled is not None and cancelled():
                raise RenderCancelled(f"Cancelled after {done:,} of {self.frames:,} frames")
            if progress is not None:
                progress(done, self.frames)
            if self.block is not None:
                self.block.wait(0.01)
        from dji_overlay.probe import VideoInfo
        from dji_overlay.render.browser import CaptureRegion
        from dji_overlay.render.pipeline import RenderResult

        Path(out).write_bytes(b"rendered")
        return RenderResult(
            output=Path(out),
            frames=self.frames,
            unique_hud_states=self.frames // 3,
            region=CaptureRegion(0, 800, 1920, 280),
            warnings=["the clip was trimmed after recording"],
            info=VideoInfo(Path(video), 1920, 1080, 30.0, 10.0, 300, "h264",
                           "yuv420p", False, False),
            track=None,
        )


@pytest.fixture
def client(media):
    previews = StubPreviews()
    runner = StubRunner()
    app = create_app(media, runner=runner, previews=previews)
    with TestClient(app) as test_client:
        test_client.previews = previews
        test_client.runner = runner
        yield test_client


# -- the library ----------------------------------------------------------


def test_library_lists_clips_with_their_sidecars(media):
    library = Library(media)
    clips = {clip.name: clip for clip in library.clips()}

    assert clips["DJI_0001.MP4"].srt == "DJI_0001.SRT"
    assert clips["DJI_0002.MP4"].srt is None
    assert clips["DJI_0009.MP4"].path == "2026-08-31/DJI_0009.MP4"


def test_a_previous_render_is_not_offered_as_source_footage(media):
    """_hud files are our own output; listing them invites rendering the HUD twice."""
    names = {clip.name for clip in Library(media).clips()}
    assert "DJI_0001_hud.mp4" not in names
    assert "DJI_0001.MP4" in names


def test_the_clip_knows_a_render_already_exists(media):
    clips = {clip.name: clip for clip in Library(media).clips()}
    assert clips["DJI_0001.MP4"].rendered is True
    assert clips["DJI_0002.MP4"].rendered is False


@pytest.mark.parametrize(
    "path",
    ["../secrets.txt", "/etc/passwd", "a/../../outside.mp4", "sub/../../../tmp/x.mp4"],
)
def test_paths_outside_the_media_directory_are_refused(media, path):
    with pytest.raises(LibraryError):
        Library(media).resolve(path)


def test_an_odd_looking_name_that_stays_inside_is_not_treated_as_an_escape(media):
    """'....' is a directory name, not two levels up; only real escapes are refused."""
    resolved = Library(media).resolve("..../DJI_0001.MP4")
    assert resolved.is_relative_to(media)


def test_a_symlink_pointing_out_of_the_library_is_refused(media, tmp_path):
    outside = tmp_path / "elsewhere.MP4"
    outside.write_bytes(b"not yours")
    (media / "escape.MP4").symlink_to(outside)

    with pytest.raises(LibraryError):
        Library(media).video("escape.MP4")


def test_only_video_files_can_be_rendered(media):
    with pytest.raises(LibraryError, match="not a video"):
        Library(media).video("DJI_0001.SRT")


def test_default_output_names_match_the_cli(media):
    library = Library(media)
    video = library.video("DJI_0001.MP4")
    assert library.default_output(video, "burn", "prores").name == "DJI_0001_hud.mp4"
    assert library.default_output(video, "alpha", "prores").name == "DJI_0001_overlay.mov"
    assert library.default_output(video, "alpha", "png").name == "DJI_0001_overlay_%06d.png"


# -- settings -------------------------------------------------------------


def test_settings_become_render_options():
    options = RenderSettings(
        mode="alpha", altitude_unit="m", speed_unit="km/h", vspeed_unit="ft/min",
        distance_unit="km", home="44.999989,-92.999988", opacity=0.8, refresh_hz=5,
    ).to_options()

    assert isinstance(options, RenderOptions)
    assert options.mode == "alpha"
    assert options.units.vspeed == "ft/min"
    assert options.telemetry.home == pytest.approx((44.999989, -92.999988))
    assert options.hud_refresh_hz == 5


@pytest.mark.parametrize(
    "settings",
    [
        {"altitude_unit": "furlongs"},
        {"home": "not,a,coordinate"},
        {"home": "91,0"},
        {"opacity": 4},
    ],
)
def test_impossible_settings_are_rejected(client, settings):
    response = client.post(
        "/api/jobs", json={"path": "DJI_0001.MP4", "settings": settings}
    )
    assert response.status_code == 422


# -- the API --------------------------------------------------------------


def test_config_describes_the_choices_the_ui_offers(client):
    body = client.get("/api/config").json()
    assert "dji_goggles" in body["templates"]
    assert body["units"]["distance"] == ["m", "km", "ft", "mi"]
    assert body["defaults"]["mode"] == "burn"


def test_library_endpoint_marks_clips_without_telemetry(client):
    clips = {clip["name"]: clip for clip in client.get("/api/library").json()["clips"]}
    assert clips["DJI_0001.MP4"]["ready"] is True
    assert clips["DJI_0002.MP4"]["ready"] is False


def test_rendering_a_clip_with_no_srt_is_a_clear_404(client):
    response = client.post("/api/jobs", json={"path": "DJI_0002.MP4"})
    assert response.status_code == 404
    assert ".SRT" in response.json()["detail"]


def test_requesting_a_file_outside_the_library_is_a_404(client):
    assert client.get("/api/preview?path=../../etc/passwd&t=1").status_code == 404
    assert client.post("/api/jobs", json={"path": "/etc/passwd"}).status_code == 404


def test_a_job_runs_and_reports_what_it_wrote(client, media):
    created = client.post(
        "/api/jobs",
        json={"path": "DJI_0001.MP4", "settings": {"altitude_unit": "m"}},
    )
    assert created.status_code == 202
    job_id = created.json()["id"]
    assert created.json()["output"] == "DJI_0001_hud.mp4"

    job = _wait_for(client, job_id, "done")
    assert job["summary"]["frames"] == 300
    assert job["warnings"] == ["the clip was trimmed after recording"]
    assert (media / "DJI_0001_hud.mp4").exists()

    video, srt, out, options = client.runner.calls[0]
    assert video == media / "DJI_0001.MP4"
    assert srt == media / "DJI_0001.SRT"
    assert out == media / "DJI_0001_hud.mp4"
    assert options.units.altitude == "m"


def test_a_failing_render_surfaces_the_error(media):
    runner = StubRunner(fail=RuntimeError("ffmpeg exited with 1"))
    app = create_app(media, runner=runner, previews=StubPreviews())
    with TestClient(app) as client:
        job_id = client.post("/api/jobs", json={"path": "DJI_0001.MP4"}).json()["id"]
        job = _wait_for(client, job_id, "failed")
        assert "ffmpeg exited with 1" in job["error"]


def test_preview_passes_the_settings_through_and_clamps_the_time(client, monkeypatch):
    from dji_overlay.probe import VideoInfo

    monkeypatch.setattr(
        "dji_overlay.web.library.Library.info",
        lambda self, video: VideoInfo(video, 1920, 1080, 30.0, 12.0, 360, "h264",
                                      "yuv420p", False, False),
    )
    response = client.get(
        "/api/preview",
        params={"path": "DJI_0001.MP4", "t": 99, "speed_unit": "kn", "opacity": 0.5},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"

    call = client.previews.calls[-1]
    assert call["at"] == pytest.approx(12.0 - 1 / 30)    # clamped to the last frame
    assert call["options"].units.speed == "kn"
    assert call["options"].opacity == 0.5


# -- the queue ------------------------------------------------------------


def test_a_queued_job_that_is_cancelled_never_starts(media):
    gate = threading.Event()
    runner = StubRunner(block=gate)
    queue = JobQueue(runner=runner)
    try:
        first = _submit(queue, media, "first.mp4")
        second = _submit(queue, media, "second.mp4")
        queue.cancel(second.id)
        gate.set()

        assert second.state is JobState.CANCELLED
        _await_state(first, JobState.DONE)
        assert len(runner.calls) == 1
    finally:
        queue.shutdown()


def test_cancelling_a_running_job_stops_it(media):
    gate = threading.Event()
    queue = JobQueue(runner=StubRunner(frames=100_000, block=gate))
    try:
        job = _submit(queue, media, "long.mp4")
        _await_state(job, JobState.RUNNING)
        queue.cancel(job.id)
        gate.set()
        _await_state(job, JobState.CANCELLED)
        assert "Cancelled after" in job.error
    finally:
        queue.shutdown()


def test_subscribers_see_every_state_change(media):
    queue = JobQueue(runner=StubRunner(frames=30))
    channel = queue.subscribe()
    try:
        job = _submit(queue, media, "watched.mp4")
        _await_state(job, JobState.DONE)
        states = []
        while not channel.empty():
            states.append(channel.get_nowait()["job"]["state"])
        assert states[0] == "queued"
        assert "running" in states
        assert states[-1] == "done"
    finally:
        queue.unsubscribe(channel)
        queue.shutdown()


# -- with a real video and a real browser ---------------------------------


@pytest.fixture
def real_media(tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    root = tmp_path / "media"
    root.mkdir()
    video = root / "DJI_0001.MP4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=2",
         "-pix_fmt", "yuv420p", str(video)],
        check=True,
    )
    shutil.copy(FIXTURE, root / "DJI_0001.SRT")
    return root


@pytest.mark.integration
def test_flight_summary_reports_the_track_and_the_video(real_media):
    app = create_app(real_media, runner=StubRunner(), previews=StubPreviews())
    with TestClient(app) as client:
        body = client.get("/api/flight", params={"path": "DJI_0001.MP4"}).json()

    assert body["video"]["resolution"] == "1280x720"
    assert body["samples"] > 0
    assert body["home"] is not None
    # The fixture SRT is far longer than this two second clip, so the mismatch
    # between video frames and telemetry samples has to be reported.
    assert any("trimmed" in warning for warning in body["warnings"])


@pytest.mark.integration
def test_the_preview_endpoint_returns_a_composited_jpeg(real_media):
    pytest.importorskip("playwright.sync_api")
    app = create_app(real_media, preview_width=640)
    with TestClient(app) as client:
        response = client.get(
            "/api/preview",
            params={"path": "DJI_0001.MP4", "t": 1.0, "altitude_unit": "ft"},
        )

    assert response.status_code == 200
    assert response.content.startswith(b"\xff\xd8")     # a JPEG
    assert len(response.content) > 5_000                # and not an empty one


# -- helpers ---------------------------------------------------------------


def _submit(queue, media, name):
    return queue.submit(
        clip="DJI_0001.MP4",
        name="DJI_0001.MP4",
        output=name,
        video_path=media / "DJI_0001.MP4",
        srt_path=media / "DJI_0001.SRT",
        out_path=media / name,
        options=RenderOptions(),
        settings={},
    )


def _await_state(job, state, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if job.state is state:
            return job
        time.sleep(0.01)
    raise AssertionError(f"{job.id} is {job.state}, expected {state}")


def _wait_for(client, job_id, state, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs = {job["id"]: job for job in client.get("/api/jobs").json()["jobs"]}
        if jobs[job_id]["state"] == state:
            return jobs[job_id]
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never reached {state}")
