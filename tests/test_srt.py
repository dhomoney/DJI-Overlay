"""Parser tests: the modern per-frame dialect and the legacy Mavic-era one."""

from pathlib import Path

import pytest

from dji_overlay.srt import SrtParseError, parse_srt, parse_srt_text

FIXTURE = Path(__file__).parent / "fixtures" / "neo2_landing.srt"

LEGACY = """1
00:00:00,000 --> 00:00:00,033
<font size="36">FrameCnt : 1, DiffTime : 33ms
2023-04-02 11:20:31,123
[iso : 100] [shutter : 1/1000.0] [fnum : 280] [ev : 0] [ct : 5500]
[GPS (-93.0000,45.0000,14)] [H : 32.5m] [D : 120.4m] [H.S : 4.5m/s] [V.S : -1.2m/s]</font>
"""

NO_LOCK = """1
00:00:00,000 --> 00:00:00,033
<font size="28">FrameCnt: 1, DiffTime: 33ms
2026-08-31 15:14:01.055
[iso: 400] [latitude: 0.000000] [longitude: 0.000000] [rel_alt: 0.000 abs_alt: 0.000]</font>
"""


def test_parses_every_block_of_the_fixture():
    records = parse_srt(FIXTURE)
    assert len(records) == 240
    assert [r.index for r in records] == list(range(1, 241))
    assert all(r.has_gps for r in records)


def test_modern_fields():
    first = parse_srt(FIXTURE)[0]
    assert first.frame == 1
    assert first.start == 0.0
    assert first.diff_time_ms == 33
    assert first.timestamp is not None and first.timestamp.year == 2026
    assert first.iso == 400
    assert first.fnum == 2.2
    assert first.shutter == pytest.approx(1 / 800)
    assert first.shutter_raw == "1/800.0"
    assert first.color_mode == "default"


def test_rel_alt_and_abs_alt_split_from_one_bracket():
    """They share a single [rel_alt: x abs_alt: y] tag, separated only by a space."""
    first = parse_srt(FIXTURE)[0]
    assert first.rel_alt == 1.4
    assert first.abs_alt == 217.401
    assert first.rel_alt != first.abs_alt


def test_comma_separated_pairs_do_not_swallow_each_other():
    """[shift x: 0.00, y: 0.00] must yield two keys, not one run-on value."""
    raw = parse_srt(FIXTURE)[0].raw
    assert raw["shift_x"] == "0.00"
    assert raw["y"] == "0.00"


def test_quaternion_values_survive_as_lists():
    """[pp_current: a, b, c, d] has no interior keys, so the whole list is the value."""
    raw = parse_srt(FIXTURE)[0].raw
    assert len(raw["pp_current"].split(",")) == 4


def test_legacy_dialect():
    record = parse_srt_text(LEGACY)[0]
    assert record.latitude == 45.0
    assert record.longitude == -93.0
    assert record.rel_alt == 32.5      # from [H : 32.5m], unit suffix stripped
    assert record.distance == 120.4    # from [D : ...]
    assert record.speed == 4.5         # from [H.S : ...]
    assert record.vspeed == -1.2       # from [V.S : ...]
    assert record.raw["satellites"] == "14"


def test_zero_zero_is_treated_as_no_lock():
    record = parse_srt_text(NO_LOCK)[0]
    assert record.latitude is None
    assert record.longitude is None
    assert record.has_gps is False


def test_crlf_and_bom_are_tolerated():
    text = "﻿" + LEGACY.replace("\n", "\r\n")
    assert len(parse_srt_text(text)) == 1


def test_non_srt_input_is_rejected():
    with pytest.raises(SrtParseError):
        parse_srt_text("this is not a subtitle file")
