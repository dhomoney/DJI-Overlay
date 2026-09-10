"""Turns telemetry samples into the strings the HUD template displays."""

from __future__ import annotations

from ..telemetry import Sample, Track
from ..units import UnitPrefs, format_flight_clock

__all__ = ["FrameValues", "frame_values", "widest_values"]

FrameValues = dict[str, str]


def _compact(units: UnitPrefs, kind: str, value: float | None) -> str:
    """DJI writes readouts without a space: '40ft', not '40 ft'."""
    if value is None:
        return units.placeholder
    return f"{units.format(kind, value)}{units.suffix(kind)}"


def frame_values(
    sample: Sample,
    track: Track,
    units: UnitPrefs,
    *,
    show_msl: bool = True,
    clock_format: str = "%Y-%m-%d %H:%M:%S",
) -> FrameValues:
    """Build the id -> text mapping the template's applyFrame() consumes."""
    values: FrameValues = {
        "vspeed": _compact(units, "vspeed", sample.vspeed),
        "speed": _compact(units, "speed", sample.speed),
        "altitude": _compact(units, "altitude", sample.rel_alt),
        "distance": _compact(units, "distance", sample.distance),
        "flight-time": format_flight_clock(sample.time),
        "heading": (
            f"{sample.heading:.0f}°" if sample.heading is not None else units.placeholder
        ),
        "clock": sample.timestamp.strftime(clock_format) if sample.timestamp else "",
    }
    values["altitude-msl"] = (
        f"{_compact(units, 'altitude', sample.abs_alt)} MSL"
        if show_msl and sample.abs_alt is not None
        else ""
    )
    return values


def widest_values(units: UnitPrefs) -> FrameValues:
    """A worst-case set of readouts, used to size the region that gets captured.

    The layout is static but the text is not; measuring with implausibly long
    values means the capture window can never clip a real frame.
    """
    return {
        "vspeed": f"-8888.8{units.suffix('vspeed')}",
        "speed": f"8888.8{units.suffix('speed')}",
        "altitude": f"88888{units.suffix('altitude')}",
        "altitude-msl": f"88888{units.suffix('altitude')} MSL",
        "distance": f"88888{units.suffix('distance')}",
        "flight-time": "8:88'88\"",
        "heading": "888°",
        "clock": "8888-88-88 88:88:88",
    }
