"""Derived flight telemetry.

DJI's SRT carries position but not motion: there is no speed, vertical speed,
heading or distance-from-home field. Those have to be computed -- and computing
them naively produces garbage, because the data is coarser than it looks.

On a Neo 2 sample the coordinates only change ~8.7 times per second and the
altitude only ~0.8 times per second, yet both are repeated on all 30 frames.
Differencing consecutive frames therefore yields a stationary reading punctuated
by spikes; on real footage that reads as a median of 0 m/s with peaks of 65 m/s
on a drone that never exceeded 6 m/s. Smoothing over a window before
differentiating is what makes these numbers honest, so it is on by default.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from .srt import SrtRecord

__all__ = ["Sample", "TelemetryOptions", "Track", "build_track", "haversine"]

EARTH_RADIUS_M = 6371008.8


@dataclass(slots=True)
class TelemetryOptions:
    """Tuning for the derived fields. Windows are in seconds."""

    speed_window: float = 1.0
    """Ground speed is differentiated after smoothing over this window."""
    vspeed_window: float = 2.0
    """Wider than ground speed because altitude updates at only ~1 Hz."""
    heading_window: float = 1.0
    heading_hold_below: float = 0.5
    """Below this ground speed (m/s) course is meaningless, so hold the last value."""
    home: tuple[float, float] | None = None
    """Explicit home coordinates; otherwise auto-detected from the lowest fix."""


@dataclass(slots=True)
class Sample:
    """One fully derived frame of telemetry."""

    index: int
    frame: int | None
    time: float
    """Seconds from the start of the clip."""
    timestamp: datetime | None

    latitude: float | None
    longitude: float | None
    rel_alt: float | None
    abs_alt: float | None

    speed: float | None
    """Ground speed, m/s."""
    vspeed: float | None
    """Vertical speed, m/s. Positive is climbing."""
    heading: float | None
    """Course over ground, degrees clockwise from true north."""
    distance: float | None
    """Straight-line distance from the home point, m."""

    gps_valid: bool
    source: SrtRecord


@dataclass(slots=True)
class Track:
    """A parsed flight: every frame, plus the summary stats a HUD may want."""

    samples: list[Sample]
    home: tuple[float, float] | None
    home_index: int | None
    duration: float
    fps: float | None
    gps_rate: float | None
    """How often coordinates actually changed, in Hz. Well below the frame rate."""
    alt_rate: float | None
    max_speed: float
    max_vspeed: float
    min_vspeed: float
    max_rel_alt: float
    max_distance: float
    path_length: float
    """Total ground distance travelled, m."""

    def __len__(self) -> int:
        return len(self.samples)

    def at_time(self, seconds: float) -> Sample:
        """Nearest sample to a point in the clip."""
        if not self.samples:
            raise IndexError("track is empty")
        times = [s.time for s in self.samples]
        idx = int(np.searchsorted(times, seconds))
        if idx <= 0:
            return self.samples[0]
        if idx >= len(self.samples):
            return self.samples[-1]
        before, after = self.samples[idx - 1], self.samples[idx]
        return before if abs(before.time - seconds) <= abs(after.time - seconds) else after


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _fill_gaps(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Linearly interpolate NaNs so the maths can run; report which were real."""
    valid = ~np.isnan(values)
    if not valid.any():
        return values, valid
    idx = np.arange(len(values))
    filled = values.copy()
    filled[~valid] = np.interp(idx[~valid], idx[valid], values[valid])
    return filled, valid


def _moving_average(values: np.ndarray, window: int) -> np.ndarray:
    """Centred moving average with edge padding, so length is preserved."""
    if window <= 1 or len(values) <= 1:
        return values
    window = min(window, len(values))
    if window % 2 == 0:  # keep the window centred
        window += 1
    pad = window // 2
    padded = np.pad(values, pad, mode="edge")
    kernel = np.ones(window) / window
    return np.convolve(padded, kernel, mode="valid")


def _window_frames(window_seconds: float, fps: float | None) -> int:
    if not fps or fps <= 0:
        return 1
    return max(1, round(window_seconds * fps))


def _estimate_fps(times: np.ndarray) -> float | None:
    if len(times) < 2:
        return None
    span = float(times[-1] - times[0])
    if span <= 0:
        return None
    return (len(times) - 1) / span


def _detect_home(
    lats: np.ndarray, lons: np.ndarray, rel_alt: np.ndarray, gps_valid: np.ndarray
) -> tuple[tuple[float, float] | None, int | None]:
    """Home is the lowest point the aircraft reached with a GPS lock.

    Using the first fix would be wrong whenever recording starts mid-flight --
    which is common, and true of the reference sample, where the clip opens at
    32.5 m and only reaches 0 m on landing.
    """
    usable = gps_valid & ~np.isnan(rel_alt)
    if not usable.any():
        usable = gps_valid
        if not usable.any():
            return None, None
        idx = int(np.flatnonzero(usable)[0])
        return (float(lats[idx]), float(lons[idx])), idx

    altitudes = np.where(usable, rel_alt, np.inf)
    lowest = float(np.min(altitudes))
    # Ground level is noisy at the decimetre scale; take the first fix that is
    # within 0.5 m of the minimum rather than the single lowest reading.
    candidates = np.flatnonzero(usable & (rel_alt <= lowest + 0.5))
    idx = int(candidates[0])
    return (float(lats[idx]), float(lons[idx])), idx


def build_track(
    records: list[SrtRecord], options: TelemetryOptions | None = None
) -> Track:
    """Turn parsed SRT records into per-frame telemetry with derived fields."""
    options = options or TelemetryOptions()
    if not records:
        raise ValueError("no SRT records to build a track from")

    times = np.array([r.start for r in records], dtype=float)
    lats = np.array([np.nan if r.latitude is None else r.latitude for r in records])
    lons = np.array([np.nan if r.longitude is None else r.longitude for r in records])
    rel = np.array([np.nan if r.rel_alt is None else r.rel_alt for r in records])

    gps_valid = ~(np.isnan(lats) | np.isnan(lons))
    lats_f, _ = _fill_gaps(lats)
    lons_f, _ = _fill_gaps(lons)
    rel_f, rel_valid = _fill_gaps(rel)

    fps = _estimate_fps(times)
    duration = float(times[-1] - times[0]) if len(times) > 1 else 0.0

    # Project to a local tangent plane so distances are plain metres. Over the
    # span of one flight the error from ignoring curvature is negligible.
    if gps_valid.any():
        lat0 = float(np.nanmean(lats_f))
        metres_per_deg_lat = 111132.92 - 559.82 * math.cos(2 * math.radians(lat0))
        metres_per_deg_lon = 111412.84 * math.cos(math.radians(lat0))
        x = (lons_f - float(lons_f[0])) * metres_per_deg_lon
        y = (lats_f - float(lats_f[0])) * metres_per_deg_lat
    else:
        x = np.zeros_like(times)
        y = np.zeros_like(times)

    speed_w = _window_frames(options.speed_window, fps)
    vspeed_w = _window_frames(options.vspeed_window, fps)
    heading_w = _window_frames(options.heading_window, fps)

    xs, ys = _moving_average(x, speed_w), _moving_average(y, speed_w)
    if len(times) > 1:
        vx, vy = np.gradient(xs, times), np.gradient(ys, times)
    else:
        vx = vy = np.zeros_like(times)
    speed = np.hypot(vx, vy)

    rel_s = _moving_average(rel_f, vspeed_w)
    vspeed = np.gradient(rel_s, times) if len(times) > 1 else np.zeros_like(times)

    # Heading gets its own smoothing pass, then holds through hovers where
    # course over ground is undefined and would otherwise spin.
    xh, yh = _moving_average(x, heading_w), _moving_average(y, heading_w)
    if len(times) > 1:
        hx, hy = np.gradient(xh, times), np.gradient(yh, times)
    else:
        hx = hy = np.zeros_like(times)
    heading = (np.degrees(np.arctan2(hx, hy)) + 360.0) % 360.0
    moving = np.hypot(hx, hy) >= options.heading_hold_below
    last: float | None = None
    for i in range(len(heading)):
        if moving[i]:
            last = float(heading[i])
        elif last is not None:
            heading[i] = last
        else:
            heading[i] = np.nan
    if np.isnan(heading).any() and moving.any():  # back-fill the opening hover
        first_valid = float(heading[np.flatnonzero(moving)[0]])
        heading = np.where(np.isnan(heading), first_valid, heading)

    home, home_index = (
        (options.home, None)
        if options.home is not None
        else _detect_home(lats_f, lons_f, rel, gps_valid)
    )

    if home is not None:
        home_lat, home_lon = home
        dx = (lons_f - home_lon) * (111412.84 * math.cos(math.radians(home_lat)))
        dy = (lats_f - home_lat) * (
            111132.92 - 559.82 * math.cos(2 * math.radians(home_lat))
        )
        distance = np.hypot(dx, dy)
        distance[~gps_valid] = np.nan
    else:
        distance = np.full(len(times), np.nan)

    path_length = float(np.sum(np.hypot(np.diff(xs), np.diff(ys)))) if len(times) > 1 else 0.0

    gps_changes = int(np.sum((np.diff(lats) != 0) | (np.diff(lons) != 0))) if len(times) > 1 else 0
    alt_changes = int(np.sum(np.diff(rel) != 0)) if len(times) > 1 else 0

    samples = [
        Sample(
            index=rec.index,
            frame=rec.frame,
            time=float(times[i]),
            timestamp=rec.timestamp,
            latitude=None if not gps_valid[i] else float(lats_f[i]),
            longitude=None if not gps_valid[i] else float(lons_f[i]),
            rel_alt=None if not rel_valid[i] else float(rel_f[i]),
            abs_alt=rec.abs_alt,
            speed=None if not gps_valid.any() else float(speed[i]),
            vspeed=None if not rel_valid.any() else float(vspeed[i]),
            heading=None if np.isnan(heading[i]) else float(heading[i]),
            distance=None if np.isnan(distance[i]) else float(distance[i]),
            gps_valid=bool(gps_valid[i]),
            source=rec,
        )
        for i, rec in enumerate(records)
    ]

    return Track(
        samples=samples,
        home=home,
        home_index=home_index,
        duration=duration,
        fps=fps,
        gps_rate=(gps_changes / duration) if duration else None,
        alt_rate=(alt_changes / duration) if duration else None,
        max_speed=float(np.nanmax(speed)) if len(speed) else 0.0,
        max_vspeed=float(np.nanmax(vspeed)) if len(vspeed) else 0.0,
        min_vspeed=float(np.nanmin(vspeed)) if len(vspeed) else 0.0,
        max_rel_alt=float(np.nanmax(rel_f)) if rel_valid.any() else 0.0,
        max_distance=float(np.nanmax(distance)) if not np.isnan(distance).all() else 0.0,
        path_length=path_length,
    )
