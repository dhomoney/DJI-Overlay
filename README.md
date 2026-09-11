# DJI-Overlay

Burn the flight telemetry from your DJI `.SRT` sidecar onto your footage as a
DJI-style HUD — height, distance from home, ground and vertical speed, heading
and flight time — or export it as a transparent track to composite in your
editor.

> **Status: early development.** The telemetry engine, the HUD renderer, both
> export paths and the web UI work. Nothing is published to Docker Hub yet.

## Why

DJI writes three files per recording: the video, an audio track, and an `.SRT`
full of GPS coordinates, altitude and camera settings. Nothing consumer-facing
puts that data back onto the video. This does.

## The web UI

```bash
docker run --rm --shm-size 1g -v "$PWD:/media" --user "$(id -u):$(id -g)" \
    -p 127.0.0.1:8787:8787 dhomoney/dji-overlay:latest serve
```

Then open <http://127.0.0.1:8787>. It lists the clips in the folder you mounted,
pairs each with its `.SRT`, and gives you a scrubber and a live preview: the
HUD is composited onto a real frame of your footage at preview size, so you can
see what feet-versus-metres or 70% opacity actually looks like over your own
video before committing to a render that takes minutes. Renders queue up, one
at a time, and report progress and an estimate; you can cancel one part way
through and the partial file is removed.

The server binds `0.0.0.0` inside the container because a container's loopback
is its own — publishing on `127.0.0.1:8787` is what keeps it off the network.
Natively it binds `127.0.0.1` by default:

```bash
dji-overlay serve --media-dir ~/Videos/drone
```

Use `localhost` at your peril: most browsers resolve it to `::1` first, which a
server bound to `127.0.0.1` never answers. The address it prints is the one
that works.

## Quick start (Docker — recommended)

```bash
docker run --rm -v "$PWD:/media" --user "$(id -u):$(id -g)" --shm-size 1g \
    dhomoney/dji-overlay:latest \
    render /media/DJI_0001.MP4
```

The `.SRT` is picked up automatically when it sits next to the video with the
same name, and the result is written to `DJI_0001_hud.mp4`.

`--shm-size 1g` matters: Chromium renders the HUD, and Docker's default 64 MB
of shared memory is not enough for 4K.

### Hardware encoding

The tool uses NVENC when it is genuinely available and falls back to software
encoding otherwise — it trial-runs the encoder rather than trusting
`ffmpeg -encoders`, because a container without GPU access advertises NVENC and
then fails at the end of the render. To get NVENC inside Docker you need the
NVIDIA Container Toolkit and `--gpus all`:

```bash
docker run --rm --gpus all --shm-size 1g -v "$PWD:/media" \
    dhomoney/dji-overlay:latest render /media/DJI_0001.MP4
```

Without it the container encodes on the CPU, which on 4K is several times
slower than a native run on a machine with a GPU.

## Quick start (native)

Needs `ffmpeg` on your `PATH`.

```bash
uv venv && uv pip install -e ".[dev]"
playwright install chromium
dji-overlay render DJI_0001.MP4
```

The web UI is an optional extra, because the CLI should not need a web
framework to render a video:

```bash
uv pip install -e ".[web]"
dji-overlay serve --media-dir .
```

## Usage

Inspect a flight without rendering anything:

```bash
dji-overlay inspect DJI_0001.SRT --altitude-unit ft --speed-unit mph
```

Burn the HUD in, in metric, at a specific home point:

```bash
dji-overlay render DJI_0001.MP4 \
    --altitude-unit m --speed-unit km/h --distance-unit m \
    --home 44.999989,-92.999988
```

Export a transparent overlay to composite yourself:

```bash
dji-overlay render DJI_0001.MP4 --mode alpha
```

That writes ProRes 4444 in a `.mov`, which drops straight onto a track above
your footage in kdenlive, Resolve or Premiere. `--alpha-codec png` writes a
numbered PNG sequence instead, if you would rather not carry a 1 GB/min file.

Render a short section while you dial in the look:

```bash
dji-overlay render DJI_0001.MP4 --start 40 --duration 12 --out preview.mp4
```

### Units

Every readout takes its own unit, so you can fly altitude in feet and read
distance in metres:

| Option | Choices |
|---|---|
| `--altitude-unit` | `m`, `ft` |
| `--speed-unit` | `m/s`, `km/h`, `mph`, `kn` |
| `--vspeed-unit` | `m/s`, `km/h`, `mph`, `kn`, `ft/s`, `ft/min` |
| `--distance-unit` | `m`, `km`, `ft`, `mi` |

## What the HUD shows, and what it cannot

Height is displayed as metres above your takeoff point, with sea-level altitude
stacked beneath it in smaller type. Distance is measured from the home point,
which defaults to the lowest point of the flight rather than the first GPS fix —
recordings often start mid-air, and the first fix would then be wrong.

The DJI goggles also show battery charge, signal strength, video bitrate,
satellite count, goggles charge and flight mode. **None of those are recorded in
the SRT**, so this tool does not display them. Showing invented values on a
video that reads as a flight record would be worse than showing nothing.

## A note on derived telemetry

DJI's SRT contains position, not motion — there is no speed, vertical speed or
heading field. Those are computed here, and they are computed with smoothing on
purpose: the coordinates in a Neo 2 file only update about 8.7 times a second
and the altitude about once a second, even though every value is repeated on all
30 frames. Differencing consecutive frames produces a drone that appears to sit
still and then teleport at 145 mph.

Readouts also hold for a tenth of a second by default (`--refresh-hz`), which
matches how the real goggles behave and keeps the numbers from flickering.

If the video was trimmed or re-encoded after recording, the SRT no longer lines
up with it. The tool compares frame counts and says so; `--srt-offset` shifts
telemetry by a number of frames to correct it.

## Development

```bash
uv pip install -e ".[dev]"      # includes the web extra
pytest              # unit tests
pytest -m integration   # also drives Chromium and ffmpeg
ruff check src tests
```

The layers are worth keeping straight: `srt.py` and `telemetry.py` know nothing
about rendering, `render/` knows nothing about HTTP, and `web/` is translation
only — paths through `web/library.py`, settings into a `RenderOptions`, job
events out as SSE. A new front end should consume those events rather than
reach into the pipeline.

Flight footage is gitignored. The committed test fixture is a real landing with
its coordinates shifted to a neutral location — telemetry contains the pilot's
home, so never commit an unmodified SRT.

## License

MIT. The bundled Barlow Semi Condensed font is licensed under the SIL Open Font
License; see `src/dji_overlay/templates/fonts/OFL.txt`.
