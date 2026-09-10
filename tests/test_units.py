import pytest

from dji_overlay.units import UnitPrefs, format_duration


def test_per_field_units_are_independent():
    u = UnitPrefs(altitude="ft", speed="mph", vspeed="ft/min", distance="km")
    assert u.format("altitude", 30.48) == "100"
    assert u.format("speed", 10.0) == "22.4"
    assert u.format("vspeed", 1.0) == "197"
    assert u.format("distance", 1500.0) == "1.50"


def test_suffixes_match_the_dji_hud():
    u = UnitPrefs(altitude="ft", speed="mph", vspeed="mph", distance="ft")
    assert u.format("altitude", 12.19, with_suffix=True) == "40 ft"
    assert u.format("speed", 11.66, with_suffix=True) == "26.1 mph"


def test_missing_values_render_as_placeholder():
    u = UnitPrefs(placeholder="--")
    assert u.format("speed", None) == "--"
    assert u.format_latitude(None) == "--"


def test_negative_zero_is_never_displayed():
    assert UnitPrefs(vspeed="ft/min").format("vspeed", -0.001) == "0"


def test_dms_coordinates():
    u = UnitPrefs(coordinates="dms")
    assert u.format_latitude(44.999989).startswith("44°59'")
    assert u.format_longitude(-92.999988).endswith("W")


def test_unknown_unit_is_rejected_early():
    with pytest.raises(ValueError, match="Unknown altitude unit"):
        UnitPrefs(altitude="furlongs")


def test_duration_formatting():
    assert format_duration(53) == "0:53"
    assert format_duration(473) == "7:53"   # the 07'53" from the reference HUD
    assert format_duration(3723) == "1:02:03"
