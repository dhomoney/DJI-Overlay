# DJI-Overlay

Burn the flight telemetry from your DJI `.SRT` sidecar onto your footage as a
DJI-style HUD — altitude, speed, distance from home, heading and flight time —
or export it as a transparent overlay track to composite in your editor.

> **Status: early development.** The telemetry engine works; the renderer is
> being built.

## Why

DJI writes three files per recording: the video, an audio track, and an `.SRT`
full of GPS coordinates, altitude and camera settings. Nothing consumer-facing
puts that data back onto the video. This does.

## Quick start (Docker — recommended)

```bash
docker run --rm -v "$PWD:/media" davidho/dji-overlay:latest \
    render /media/DJI_0001.MP4 --srt /media/DJI_0001.SRT --out /media/DJI_0001_hud.mp4
```

## Quick start (native)

```bash
uv venv && uv pip install -e ".[dev]"
dji-overlay inspect samples/DJI_0001.SRT
```

Requires `ffmpeg` on your `PATH`.

## A note on derived telemetry

DJI's SRT contains position, not motion — there is no speed, vertical speed or
heading field. Those are computed here, and they are computed with smoothing on
purpose: the coordinates in a Neo 2 file only update about 8.7 times a second
and the altitude about once a second, even though every value is repeated on all
30 frames. Differencing consecutive frames produces a drone that appears to sit
still and then teleport at 145 mph. See `src/dji_overlay/telemetry.py`.

## License

MIT
