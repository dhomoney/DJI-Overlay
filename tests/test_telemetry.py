"""Telemetry tests, focused on the ways derived motion goes wrong."""

import math
from datetime import datetime
from itertools import pairwise
from pathlib import Path

import pytest

from dji_overlay.srt import SrtRecord, parse_srt
from dji_overlay.telemetry import TelemetryOptions, build_track, haversine

FIXTURE = Path(__file__).parent / "fixtures" / "neo2_landing.srt"
FPS = 30000 / 1001


def synth_records(n=300, speed_mps=5.0, gps_hz=8.7, quantum=1e-6, alt=50.0, alt_hz=1.0):
    """A drone flying due east at a constant speed, sampled the way DJI samples.

    Coordinates advance only ``gps_hz`` times a second and are rounded to six
    decimal places, then repeated across every frame -- exactly the staircase
    that makes naive differencing explode.
    """
    lat0, lon0 = 45.0, -93.0
    m_per_deg_lon = 111412.84 * math.cos(math.radians(lat0))
    records = []
    last_lon, last_gps_t = lon0, 0.0
    last_alt, last_alt_t = alt, 0.0
    for i in range(n):
        t = i / FPS
        if t - last_gps_t >= 1 / gps_hz:
            true_lon = lon0 + (speed_mps * t) / m_per_deg_lon
            last_lon = round(true_lon / quantum) * quantum
            last_gps_t = t
        if t - last_alt_t >= 1 / alt_hz:
            last_alt = round((alt + 0.0) * 10) / 10
            last_alt_t = t
        records.append(
            SrtRecord(
                index=i + 1, frame=i + 1, start=t, end=(i + 1) / FPS,
                timestamp=datetime(2026, 8, 31, 15, 0, 0),
                latitude=lat0, longitude=last_lon,
                rel_alt=last_alt, abs_alt=last_alt + 200,
            )
        )
    return records


def test_smoothing_recovers_true_speed_from_staircase_data():
    track = build_track(synth_records(speed_mps=5.0))
    mid = [s.speed for s in track.samples[45:-45]]  # ignore window warm-up at the edges
    assert sum(mid) / len(mid) == pytest.approx(5.0, abs=0.4)


def test_naive_differencing_would_have_produced_nonsense():
    """Guards the reason smoothing exists: raw frame-to-frame deltas spike wildly."""
    records = synth_records(speed_mps=5.0)
    raw_peak = max(
        haversine(a.latitude, a.longitude, b.latitude, b.longitude) * FPS
        for a, b in pairwise(records)
    )
    assert raw_peak > 20.0  # a 5 m/s flight appears to hit 20+ m/s unsmoothed
    assert build_track(records).max_speed < 8.0  # smoothed stays believable


def test_hovering_holds_heading_instead_of_spinning():
    records = synth_records(speed_mps=0.0)
    headings = {s.heading for s in build_track(records).samples if s.heading is not None}
    assert len(headings) <= 1


def test_heading_points_east_when_flying_east():
    track = build_track(synth_records(speed_mps=5.0))
    assert track.samples[len(track.samples) // 2].heading == pytest.approx(90.0, abs=8.0)


def test_home_is_the_lowest_point_not_the_first_fix():
    """The reference clip starts mid-air at 1.4 m and only reaches 0 m on landing."""
    track = build_track(parse_srt(FIXTURE))
    assert track.home is not None
    assert track.home_index is not None
    assert track.samples[track.home_index].rel_alt == pytest.approx(0.0, abs=0.5)
    assert track.home_index > 0
    assert track.samples[-1].distance == pytest.approx(0.0, abs=5.0)


def test_explicit_home_overrides_detection():
    records = parse_srt(FIXTURE)
    track = build_track(records, TelemetryOptions(home=(45.01, -93.0)))
    assert track.home == (45.01, -93.0)
    assert track.samples[0].distance > 500  # ~1.1 km north of the flight


def test_fixture_reports_the_real_sampling_rates():
    track = build_track(parse_srt(FIXTURE))
    assert track.fps == pytest.approx(29.97, abs=0.1)
    assert track.gps_rate < 15  # nowhere near the 30 Hz frame rate
    assert track.alt_rate < 5


def test_missing_gps_frames_are_flagged_but_do_not_break_the_maths():
    records = synth_records()
    for r in records[100:130]:
        r.latitude = r.longitude = None
    track = build_track(records)
    assert track.samples[110].gps_valid is False
    assert track.samples[110].latitude is None
    assert track.samples[110].speed is not None  # interpolated for continuity


def test_at_time_finds_the_nearest_sample():
    track = build_track(parse_srt(FIXTURE))
    assert track.at_time(-5).index == 1
    assert track.at_time(9999).index == len(track)
    assert track.at_time(4.0).time == pytest.approx(4.0, abs=0.05)
