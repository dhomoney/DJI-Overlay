"""Command line entry point."""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .srt import SrtParseError, parse_srt
from .telemetry import TelemetryOptions, build_track
from .units import UnitPrefs, format_duration

console = Console()


@click.group()
@click.version_option(package_name="dji-overlay")
def main() -> None:
    """Overlay DJI flight telemetry onto your footage."""


@main.command()
@click.argument("srt_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--altitude-unit", type=click.Choice(["m", "ft"]), default="m")
@click.option("--speed-unit", type=click.Choice(["m/s", "km/h", "mph", "kn"]), default="m/s")
@click.option(
    "--distance-unit", type=click.Choice(["m", "km", "ft", "mi"]), default="m"
)
@click.option(
    "--home",
    metavar="LAT,LON",
    help="Home coordinates. Defaults to the lowest point of the flight.",
)
@click.option("--rows", default=10, show_default=True, help="Sample rows to print.")
def inspect(
    srt_path: Path,
    altitude_unit: str,
    speed_unit: str,
    distance_unit: str,
    home: str | None,
    rows: int,
) -> None:
    """Parse an SRT and report the flight it describes."""
    try:
        records = parse_srt(srt_path)
    except SrtParseError as exc:
        raise click.ClickException(str(exc)) from exc

    options = TelemetryOptions()
    if home:
        try:
            lat, lon = (float(part) for part in home.split(","))
        except ValueError as exc:
            raise click.ClickException("--home expects LAT,LON in decimal degrees") from exc
        options.home = (lat, lon)

    track = build_track(records, options)
    units = UnitPrefs(
        altitude=altitude_unit,
        speed=speed_unit,
        vspeed=speed_unit,
        distance=distance_unit,
    )

    summary = Table(box=None, show_header=False, pad_edge=False)
    summary.add_column(style="dim")
    summary.add_column()
    summary.add_row("File", str(srt_path))
    summary.add_row("Samples", f"{len(track):,}")
    summary.add_row("Duration", f"{format_duration(track.duration)} @ {track.fps:.2f} fps")
    if track.samples[0].timestamp:
        summary.add_row("Recorded", track.samples[0].timestamp.strftime("%Y-%m-%d %H:%M:%S"))
    if track.home:
        detected = "explicit" if home else f"auto, from sample {track.home_index:,}"
        summary.add_row(
            "Home", f"{track.home[0]:.6f}, {track.home[1]:.6f}  [dim]({detected})[/dim]"
        )
    summary.add_row("Max altitude", units.format("altitude", track.max_rel_alt, with_suffix=True))
    summary.add_row("Max speed", units.format("speed", track.max_speed, with_suffix=True))
    summary.add_row("Max distance", units.format("distance", track.max_distance, with_suffix=True))
    summary.add_row("Path length", units.format("distance", track.path_length, with_suffix=True))
    console.print(summary)

    # The sampling rates are the reason the derived fields are smoothed; showing
    # them makes an implausible-looking readout self-explanatory.
    if track.gps_rate is not None and track.fps and track.gps_rate < track.fps * 0.9:
        console.print(
            f"\n[yellow]Note[/yellow]: GPS updates at ~{track.gps_rate:.1f} Hz and altitude at "
            f"~{track.alt_rate:.1f} Hz, below the {track.fps:.0f} fps frame rate. "
            "Speed, vertical speed and heading are smoothed before differentiation."
        )

    table = Table(title="\nTelemetry", title_justify="left")
    table.add_column("Time", justify="right")
    table.add_column("VS", justify="right")
    table.add_column("HS", justify="right")
    table.add_column("H", justify="right")
    table.add_column("D", justify="right")
    table.add_column("HDG", justify="right")
    table.add_column("Position", justify="right", style="dim")

    step = max(1, len(track) // max(1, rows))
    for sample in track.samples[::step][:rows]:
        table.add_row(
            format_duration(sample.time),
            units.format("vspeed", sample.vspeed, with_suffix=True),
            units.format("speed", sample.speed, with_suffix=True),
            units.format("altitude", sample.rel_alt, with_suffix=True),
            units.format("distance", sample.distance, with_suffix=True),
            f"{sample.heading:.0f}°" if sample.heading is not None else units.placeholder,
            f"{units.format_latitude(sample.latitude)}, "
            f"{units.format_longitude(sample.longitude)}",
        )
    console.print(table)


if __name__ == "__main__":
    main()
