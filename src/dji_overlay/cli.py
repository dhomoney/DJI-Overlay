"""Command line entry point."""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .probe import FFmpegMissingError
from .render.browser import available_templates
from .render.pipeline import MODES, RenderOptions
from .render.pipeline import render as render_clip
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


def _sidecar(video: Path, explicit: Path | None, suffix: str) -> Path:
    """Find the SRT that belongs to a clip: DJI names it identically."""
    if explicit is not None:
        return explicit
    for candidate in (video.with_suffix(suffix), video.with_suffix(suffix.upper())):
        if candidate.exists():
            return candidate
    raise click.ClickException(
        f"No {suffix} found next to {video.name}. Pass one with --srt."
    )


def _parse_home(home: str | None) -> tuple[float, float] | None:
    if not home:
        return None
    try:
        lat, lon = (float(part) for part in home.split(","))
    except ValueError as exc:
        raise click.ClickException("--home expects LAT,LON in decimal degrees") from exc
    return lat, lon


@main.command()
@click.argument("video", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--srt", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Telemetry file. Defaults to the matching .SRT next to the video.")
@click.option("--out", type=click.Path(dir_okay=False, path_type=Path),
              help="Output file. Defaults to <video>_hud.<ext> beside the source.")
@click.option("--mode", type=click.Choice(MODES), default="burn", show_default=True,
              help="'burn' composites onto the video; 'alpha' writes a transparent track.")
@click.option("--template", default="dji_goggles", show_default=True,
              type=click.Choice(available_templates()))
@click.option("--altitude-unit", type=click.Choice(["m", "ft"]), default="ft", show_default=True)
@click.option("--speed-unit", type=click.Choice(["m/s", "km/h", "mph", "kn"]), default="mph",
              show_default=True)
@click.option("--vspeed-unit", type=click.Choice(["m/s", "km/h", "mph", "kn", "ft/s", "ft/min"]),
              default="mph", show_default=True)
@click.option("--distance-unit", type=click.Choice(["m", "km", "ft", "mi"]), default="ft",
              show_default=True)
@click.option("--no-msl", is_flag=True, help="Hide the sea-level altitude under the height.")
@click.option("--home", metavar="LAT,LON", help="Override the auto-detected home point.")
@click.option("--opacity", type=click.FloatRange(0.1, 1.0), default=1.0, show_default=True)
@click.option("--encoder", default="auto", show_default=True,
              help="Video encoder, or 'auto' to use NVENC when available.")
@click.option("--crf", type=int, default=18, show_default=True)
@click.option("--preset", default="medium", show_default=True)
@click.option("--alpha-codec", type=click.Choice(["prores", "png"]), default="prores",
              show_default=True, help="Transparent export format for --mode alpha.")
@click.option("--srt-offset", type=int, default=0,
              help="Shift telemetry by N frames, for footage trimmed after recording.")
@click.option("--refresh-hz", type=float, default=10.0, show_default=True,
              help="How often the readouts update. 0 renders every frame separately.")
@click.option("--start", type=float, default=0.0, help="Start time in seconds.")
@click.option("--duration", type=float, default=None, help="Seconds to render.")
def render(video: Path, srt: Path | None, out: Path | None, mode: str, template: str,
           altitude_unit: str, speed_unit: str, vspeed_unit: str, distance_unit: str,
           no_msl: bool, home: str | None, opacity: float, encoder: str, crf: int,
           preset: str, alpha_codec: str, srt_offset: int, refresh_hz: float,
           start: float, duration: float | None) -> None:
    """Render a HUD onto VIDEO."""
    srt_path = _sidecar(video, srt, ".srt")

    if out is None:
        if mode == "alpha":
            extension = ".mov" if alpha_codec == "prores" else "_%06d.png"
            out = video.with_name(f"{video.stem}_overlay{extension}")
        else:
            out = video.with_name(f"{video.stem}_hud{video.suffix.lower()}")

    options = RenderOptions(
        mode=mode,
        template=template,
        units=UnitPrefs(altitude=altitude_unit, speed=speed_unit,
                        vspeed=vspeed_unit, distance=distance_unit),
        telemetry=TelemetryOptions(home=_parse_home(home)),
        opacity=opacity,
        show_msl=not no_msl,
        encoder=encoder,
        crf=crf,
        preset=preset,
        alpha_codec=alpha_codec,
        srt_offset=srt_offset,
        hud_refresh_hz=refresh_hz,
        start=start,
        duration=duration,
    )

    from rich.progress import (
        BarColumn,
        Progress,
        SpinnerColumn,
        TaskProgressColumn,
        TextColumn,
        TimeRemainingColumn,
    )

    try:
        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            BarColumn(), TaskProgressColumn(), TimeRemainingColumn(), console=console,
        ) as bar:
            task = bar.add_task(f"Rendering {video.name}", total=None)

            def advance(done: int, total: int) -> None:
                bar.update(task, completed=done, total=total)

            result = render_clip(video, srt_path, out, options, advance)
    except (FFmpegMissingError, SrtParseError) as exc:
        raise click.ClickException(str(exc)) from exc

    for warning in result.warnings:
        console.print(f"[yellow]Warning[/yellow]: {warning}")

    reuse = 1 - result.unique_hud_states / max(1, result.frames)
    console.print(
        f"[green]Wrote[/green] {result.output}\n"
        f"  {result.frames:,} frames, {result.unique_hud_states:,} distinct HUD states "
        f"({reuse:.0%} reused)\n"
        f"  overlay band {result.region.width}x{result.region.height} at y={result.region.y}"
    )
