"""Tests for the rendering layer.

The frame-timing and formatting logic is tested directly; the tests that need a
real Chromium and ffmpeg are marked ``integration`` and skip when those are
absent, so the suite still runs on a bare checkout.
"""

from dataclasses import replace
from pathlib import Path

import pytest

from dji_overlay.render.hud import frame_values, widest_values
from dji_overlay.render.pipeline import (
    RenderOptions,
    _sample_stream,
    detect_encoder,
    encoder_works,
)
from dji_overlay.srt import parse_srt
from dji_overlay.telemetry import build_track
from dji_overlay.units import UnitPrefs, format_flight_clock

FIXTURE = Path(__file__).parent / "fixtures" / "neo2_landing.srt"


@pytest.fixture(scope="module")
def track():
    return build_track(parse_srt(FIXTURE))


@pytest.fixture
def imperial():
    return UnitPrefs(altitude="ft", speed="mph", vspeed="mph", distance="ft")


def test_readouts_are_compact_like_the_dji_hud(track, imperial):
    values = frame_values(track.samples[0], track, imperial)
    assert values["altitude"].endswith("ft")
    assert " " not in values["altitude"]      # DJI writes '40ft', not '40 ft'
    assert values["speed"].endswith("mph")
    assert " " not in values["speed"]


def test_heading_is_shown_in_degrees(track, imperial):
    sample = replace(track.samples[0], heading=136.4)
    assert frame_values(sample, track, imperial)["heading"] == "136°"


def test_heading_stays_blank_on_a_landing_where_course_is_undefined(track, imperial):
    """The fixture is a slow descent, so there is no meaningful course to show."""
    values = frame_values(track.samples[0], track, imperial)
    assert values["heading"] == imperial.placeholder


def test_msl_line_can_be_hidden(track, imperial):
    assert frame_values(track.samples[0], track, imperial)["altitude-msl"].endswith("MSL")
    hidden = frame_values(track.samples[0], track, imperial, show_msl=False)
    assert hidden["altitude-msl"] == ""


def test_missing_values_fall_back_to_the_placeholder(track):
    sample = replace(track.samples[0], speed=None, heading=None)
    values = frame_values(sample, track, UnitPrefs(placeholder="--"))
    assert values["speed"] == "--"
    assert values["heading"] == "--"


def test_flight_clock_uses_dji_notation():
    assert format_flight_clock(473) == "07'53\""
    assert format_flight_clock(45) == "00'45\""
    assert format_flight_clock(3723) == "1:02'03\""


def test_widest_values_cover_every_template_slot(track, imperial):
    """The capture region is measured with these, so they must fill every field."""
    real = frame_values(track.samples[0], track, imperial)
    assert set(widest_values(imperial)) == set(real)


def test_sample_stream_holds_each_reading(track):
    """A hold of 3 repeats each telemetry sample across three frames."""
    samples = list(_sample_stream(track, frames=9, first_sample=0, offset=0, hold=3))
    assert [s.index for s in samples] == [1, 1, 1, 4, 4, 4, 7, 7, 7]


def test_sample_stream_without_hold_advances_every_frame(track):
    samples = list(_sample_stream(track, frames=4, first_sample=0, offset=0, hold=1))
    assert [s.index for s in samples] == [1, 2, 3, 4]


def test_sample_stream_applies_the_offset(track):
    samples = list(_sample_stream(track, frames=3, first_sample=10, offset=5, hold=1))
    assert [s.index for s in samples] == [16, 17, 18]


def test_sample_stream_holds_the_last_sample_when_telemetry_runs_short(track):
    """A video longer than its SRT must still get a frame for every frame."""
    samples = list(_sample_stream(track, frames=5, first_sample=len(track) - 2,
                                  offset=0, hold=1))
    assert len(samples) == 5
    assert samples[-1].index == len(track)


def test_sample_stream_clamps_a_negative_offset(track):
    samples = list(_sample_stream(track, frames=3, first_sample=0, offset=-50, hold=1))
    assert all(s.index == 1 for s in samples)


def test_explicit_encoder_is_respected():
    assert detect_encoder("libx264", "hevc") == "libx264"
    assert detect_encoder("hevc_nvenc", "hevc") == "hevc_nvenc"


@pytest.mark.integration
def test_auto_encoder_keeps_the_source_codec_family():
    chosen = detect_encoder("auto", "hevc")
    assert "hevc" in chosen or "x265" in chosen


@pytest.mark.integration
def test_a_nonexistent_encoder_is_not_reported_as_working():
    assert encoder_works("definitely_not_an_encoder") is False


@pytest.mark.integration
def test_auto_detection_only_returns_an_encoder_that_runs():
    """Guards the container case: NVENC is listed but cannot load CUDA."""
    assert encoder_works(detect_encoder("auto", "hevc")) is True


def test_render_options_reject_an_unknown_mode():
    from dji_overlay.render.pipeline import render

    with pytest.raises(ValueError, match="mode must be one of"):
        render("a.mp4", "a.srt", "out.mp4", RenderOptions(mode="sideways"))


@pytest.mark.integration
def test_chromium_renders_a_transparent_hud(track, imperial):
    playwright = pytest.importorskip("playwright.sync_api")
    from dji_overlay.render.browser import HudRenderer

    try:
        with HudRenderer(1920, 1080) as renderer:
            region = renderer.measure(widest_values(imperial))
            png = renderer.render(frame_values(track.samples[0], track, imperial))
    except playwright.Error as exc:
        pytest.skip(f"Chromium unavailable: {exc}")

    assert png.startswith(b"\x89PNG")
    assert region.height < 1080          # only the HUD band is captured
    assert region.width == 1920

    # Rendering the same readouts twice must hit the cache, not Chromium.
    with HudRenderer(1920, 1080) as renderer:
        renderer.measure(widest_values(imperial))
        values = frame_values(track.samples[0], track, imperial)
        renderer.render(values)
        renderer.render(values)
        assert renderer.cache_size == 1
