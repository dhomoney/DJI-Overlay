"""Parser for the .SRT telemetry sidecar files DJI aircraft write alongside video.

DJI has shipped several dialects of this format. The two that matter:

*Modern* (Neo, Neo 2, Avata, Mini 3+, Air 3 ...), one block per video frame::

    1
    00:00:00,000 --> 00:00:00,033
    <font size="28">FrameCnt: 1, DiffTime: 33ms
    2026-08-31 15:14:01.055
    [iso: 400] [shutter: 1/800.0] [latitude: 44.999989] [longitude: -92.999988]
    [rel_alt: 32.500 abs_alt: 248.501] ...</font>

*Legacy* (Mavic/Phantom era), where the aircraft had already done the maths::

    [GPS (-92.9999,44.9999,15)] [H: 32.5m] [D: 120.4m] [H.S: 4.5m/s] [V.S: 0.0m/s]

Both reduce to the same :class:`SrtRecord`. Fields absent from a dialect stay
``None`` -- callers decide whether to derive them or hide the widget.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

__all__ = ["SrtRecord", "SrtParseError", "parse_srt", "parse_srt_text"]


class SrtParseError(ValueError):
    """Raised when a file does not look like a DJI telemetry SRT at all."""


# --- block-level scanning ----------------------------------------------------

_HTML_TAG = re.compile(r"</?font[^>]*>", re.IGNORECASE)
_TIMECODE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)
_FRAME_CNT = re.compile(r"(?:FrameCnt|SrtCnt)\s*:\s*(\d+)", re.IGNORECASE)
_DIFF_TIME = re.compile(r"DiffTime\s*:\s*(\d+)\s*ms", re.IGNORECASE)
_WALL_CLOCK = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:[.,](\d{1,6}))?"
)

# Bracketed tags: [iso: 400], [rel_alt: 32.5 abs_alt: 248.5], [shift x: 0.0, y: 0.0]
_TAG = re.compile(r"\[([^\]]+)\]")
# A key is letters/digits/underscore/dot and may contain interior spaces ("shift x").
# A value runs until the next "key:" (comma- or space-separated) or end of tag.
_PAIR = re.compile(
    r"([A-Za-z_][A-Za-z0-9_. ]*?)\s*:\s*"
    r"(.+?)"
    r"(?=\s*,?\s+[A-Za-z_][A-Za-z0-9_. ]*\s*:|\s*,\s*[A-Za-z_][A-Za-z0-9_. ]*\s*:|\s*$)"
)
# Legacy positional form: [GPS (lon,lat,sats)] or [GPS(lon,lat,sats)]
_LEGACY_GPS = re.compile(
    r"GPS\s*\(\s*([-\d.]+)\s*,\s*([-\d.]+)\s*(?:,\s*([-\d.]+)\s*)?\)", re.IGNORECASE
)

# Legacy short keys -> canonical names.
_KEY_ALIASES = {
    "h": "rel_alt",
    "d": "distance",
    "h.s": "speed",
    "v.s": "vspeed",
    "altitude": "rel_alt",
    "abs_altitude": "abs_alt",
}

_NUMERIC = re.compile(r"^[-+]?\d*\.?\d+")


def _to_float(raw: str) -> float | None:
    """Pull a float off the front of a value, tolerating unit suffixes ('32.5m')."""
    m = _NUMERIC.match(raw.strip())
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _parse_shutter(raw: str) -> float | None:
    """'1/800.0' -> 0.00125 seconds. Bare numbers are taken as seconds."""
    raw = raw.strip()
    if "/" in raw:
        num, _, den = raw.partition("/")
        n, d = _to_float(num), _to_float(den)
        if n is None or not d:
            return None
        return n / d
    return _to_float(raw)


@dataclass(slots=True)
class SrtRecord:
    """One telemetry sample, normally one video frame."""

    index: int
    """1-based position in the file."""
    frame: int | None
    """FrameCnt as reported by the aircraft, when present."""
    start: float
    """Seconds from the start of the clip (from the SRT timecode)."""
    end: float
    diff_time_ms: int | None = None
    timestamp: datetime | None = None
    """Aircraft wall-clock time, already in the pilot's local timezone (naive)."""

    latitude: float | None = None
    longitude: float | None = None
    rel_alt: float | None = None
    """Metres above the takeoff point (barometric)."""
    abs_alt: float | None = None
    """Metres above mean sea level."""

    # Present only in the legacy dialect, where the aircraft pre-computed them.
    speed: float | None = None
    vspeed: float | None = None
    distance: float | None = None

    iso: float | None = None
    shutter: float | None = None
    """Exposure time in seconds."""
    shutter_raw: str | None = None
    fnum: float | None = None
    ev: float | None = None
    color_temp: float | None = None
    color_mode: str | None = None
    focal_len: float | None = None

    raw: dict[str, str] | None = None
    """Every tag found, unparsed, so templates can reach uncommon fields."""

    @property
    def has_gps(self) -> bool:
        return self.latitude is not None and self.longitude is not None


def _timecode_seconds(m: re.Match[str], offset: int) -> float:
    h, mi, s, ms = (m.group(offset + i) for i in range(4))
    return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def _extract_tags(text: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    for tag in _TAG.findall(text):
        gps = _LEGACY_GPS.search(tag)
        if gps:
            tags["longitude"] = gps.group(1)
            tags["latitude"] = gps.group(2)
            if gps.group(3):
                tags["satellites"] = gps.group(3)
            continue
        for key, value in _PAIR.findall(tag):
            key = key.strip().lower().replace(" ", "_")
            key = _KEY_ALIASES.get(key, key)
            tags[key] = value.strip()
    return tags


def _build_record(index: int, block: str) -> SrtRecord | None:
    text = _HTML_TAG.sub("", block)

    tc = _TIMECODE.search(text)
    if not tc:
        return None
    start = _timecode_seconds(tc, 1)
    end = _timecode_seconds(tc, 5)

    frame_m = _FRAME_CNT.search(text)
    diff_m = _DIFF_TIME.search(text)
    clock_m = _WALL_CLOCK.search(text)

    timestamp = None
    if clock_m:
        y, mo, d, h, mi, s, frac = clock_m.groups()
        micro = int((frac or "0").ljust(6, "0")[:6])
        timestamp = datetime(
            int(y), int(mo), int(d), int(h), int(mi), int(s), micro
        )

    tags = _extract_tags(text)
    rec = SrtRecord(
        index=index,
        frame=int(frame_m.group(1)) if frame_m else None,
        start=start,
        end=end,
        diff_time_ms=int(diff_m.group(1)) if diff_m else None,
        timestamp=timestamp,
        latitude=_to_float(tags["latitude"]) if "latitude" in tags else None,
        longitude=_to_float(tags["longitude"]) if "longitude" in tags else None,
        rel_alt=_to_float(tags["rel_alt"]) if "rel_alt" in tags else None,
        abs_alt=_to_float(tags["abs_alt"]) if "abs_alt" in tags else None,
        speed=_to_float(tags["speed"]) if "speed" in tags else None,
        vspeed=_to_float(tags["vspeed"]) if "vspeed" in tags else None,
        distance=_to_float(tags["distance"]) if "distance" in tags else None,
        iso=_to_float(tags["iso"]) if "iso" in tags else None,
        shutter=_parse_shutter(tags["shutter"]) if "shutter" in tags else None,
        shutter_raw=tags.get("shutter"),
        fnum=_to_float(tags["fnum"]) if "fnum" in tags else None,
        ev=_to_float(tags["ev"]) if "ev" in tags else None,
        color_temp=_to_float(tags["ct"]) if "ct" in tags else None,
        color_mode=tags.get("color_md"),
        focal_len=_to_float(tags["focal_len"]) if "focal_len" in tags else None,
        raw=tags,
    )

    # A GPS fix of exactly 0,0 means "no lock", not the Gulf of Guinea.
    if rec.latitude == 0.0 and rec.longitude == 0.0:
        rec.latitude = rec.longitude = None
    return rec


def parse_srt_text(text: str) -> list[SrtRecord]:
    """Parse SRT content already in memory."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]

    records: list[SrtRecord] = []
    for block in blocks:
        rec = _build_record(len(records) + 1, block)
        if rec is not None:
            records.append(rec)

    if not records:
        raise SrtParseError(
            "No subtitle blocks with a timecode were found; this does not look "
            "like a DJI telemetry SRT."
        )
    return records


def parse_srt(path: str | Path) -> list[SrtRecord]:
    """Parse a DJI ``.SRT`` file from disk."""
    path = Path(path)
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "latin-1"):
        try:
            return parse_srt_text(raw.decode(encoding))
        except UnicodeDecodeError:
            continue
    raise SrtParseError(f"Could not decode {path} as text")
