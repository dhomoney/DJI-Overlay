"""Per-field unit conversion and formatting.

Every readout picks its own unit independently, so a pilot can fly altitude in
feet while reading speed in mph and distance in metres.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["UnitPrefs", "ALTITUDE_UNITS", "SPEED_UNITS", "VSPEED_UNITS", "DISTANCE_UNITS"]

# unit name -> (multiplier applied to the SI value, display suffix, decimal places)
ALTITUDE_UNITS: dict[str, tuple[float, str, int]] = {
    "m": (1.0, "m", 0),
    "ft": (3.280839895, "ft", 0),
}
SPEED_UNITS: dict[str, tuple[float, str, int]] = {
    "m/s": (1.0, "m/s", 1),
    "km/h": (3.6, "km/h", 1),
    "mph": (2.236936292, "mph", 1),
    "kn": (1.943844492, "kn", 1),
}
# DJI's own HUD reports vertical speed in the same unit as ground speed, so the
# speed units are all valid here too.
VSPEED_UNITS: dict[str, tuple[float, str, int]] = {
    "m/s": (1.0, "m/s", 1),
    "ft/s": (3.280839895, "ft/s", 1),
    "ft/min": (196.8503937, "ft/min", 0),
    "km/h": (3.6, "km/h", 1),
    "mph": (2.236936292, "mph", 1),
    "kn": (1.943844492, "kn", 1),
}
DISTANCE_UNITS: dict[str, tuple[float, str, int]] = {
    "m": (1.0, "m", 0),
    "km": (0.001, "km", 2),
    "ft": (3.280839895, "ft", 0),
    "mi": (0.000621371192, "mi", 2),
}

_TABLES = {
    "altitude": ALTITUDE_UNITS,
    "speed": SPEED_UNITS,
    "vspeed": VSPEED_UNITS,
    "distance": DISTANCE_UNITS,
}


@dataclass(slots=True)
class UnitPrefs:
    """Which unit each readout uses. Values must be keys of the tables above."""

    altitude: str = "m"
    speed: str = "m/s"
    vspeed: str = "m/s"
    distance: str = "m"
    coordinates: str = "decimal"  # decimal | dms
    placeholder: str = "--"
    """Shown when a value is unavailable (no GPS lock, for instance)."""

    def __post_init__(self) -> None:
        for field_name, table in _TABLES.items():
            value = getattr(self, field_name)
            if value not in table:
                raise ValueError(
                    f"Unknown {field_name} unit {value!r}; "
                    f"expected one of {', '.join(table)}"
                )
        if self.coordinates not in ("decimal", "dms"):
            raise ValueError(f"Unknown coordinate format {self.coordinates!r}")

    def convert(self, kind: str, si_value: float | None) -> float | None:
        """Convert an SI value into the configured unit for ``kind``."""
        if si_value is None:
            return None
        factor, _, _ = _TABLES[kind][getattr(self, kind)]
        return si_value * factor

    def suffix(self, kind: str) -> str:
        return _TABLES[kind][getattr(self, kind)][1]

    def format(self, kind: str, si_value: float | None, *, with_suffix: bool = False) -> str:
        """Format an SI value for display, honouring the placeholder."""
        if si_value is None:
            return self.placeholder
        _, suffix, places = _TABLES[kind][getattr(self, kind)]
        text = f"{self.convert(kind, si_value):.{places}f}"
        if text in ("-0", "-0.0", "-0.00"):  # avoid a negative zero on the HUD
            text = text[1:]
        return f"{text} {suffix}" if with_suffix else text

    def format_latitude(self, value: float | None) -> str:
        return self._format_coord(value, "N", "S")

    def format_longitude(self, value: float | None) -> str:
        return self._format_coord(value, "E", "W")

    def _format_coord(self, value: float | None, positive: str, negative: str) -> str:
        if value is None:
            return self.placeholder
        if self.coordinates == "decimal":
            return f"{value:.6f}"
        hemisphere = positive if value >= 0 else negative
        magnitude = abs(value)
        degrees = int(magnitude)
        minutes_full = (magnitude - degrees) * 60
        minutes = int(minutes_full)
        seconds = (minutes_full - minutes) * 60
        return f"{degrees}°{minutes:02d}'{seconds:04.1f}\"{hemisphere}"


def format_duration(seconds: float) -> str:
    """Elapsed flight time as M:SS, or H:MM:SS once past an hour."""
    seconds = max(0.0, seconds)
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
