"""Renders HUD frames as transparent PNGs with headless Chromium.

The page is loaded once and only its text nodes are swapped between frames.
Two things keep this fast enough to be practical on 4K footage:

* only the band of the frame the HUD actually occupies is captured, which on a
  3840x2160 clip is a fraction of the pixels;
* identical readouts are rendered once and reused. Telemetry updates far slower
  than 30 fps and the displayed values are rounded, so a typical clip contains
  a few thousand distinct HUD states rather than one per frame.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

from .hud import FrameValues

__all__ = ["TEMPLATE_ROOT", "CaptureRegion", "HudRenderer", "available_templates"]

TEMPLATE_ROOT = Path(__file__).resolve().parent.parent / "templates"

REFERENCE_HEIGHT = 1080
"""The template is authored at this height; everything scales from it."""


def available_templates() -> list[str]:
    return sorted(
        p.name for p in TEMPLATE_ROOT.iterdir() if p.is_dir() and (p / "hud.html").exists()
    )


@dataclass(frozen=True, slots=True)
class CaptureRegion:
    """The sub-rectangle of the frame that the HUD occupies."""

    x: int
    y: int
    width: int
    height: int

    @property
    def is_full_frame(self) -> bool:
        return self.x == 0 and self.y == 0


class HudRenderer:
    """Context manager owning a Chromium page for the life of a render."""

    def __init__(
        self,
        width: int,
        height: int,
        *,
        template: str = "dji_goggles",
        opacity: float = 1.0,
        scale: float | None = None,
    ) -> None:
        self.width = width
        self.height = height
        self.template = template
        self.opacity = opacity
        self.scale = scale if scale is not None else height / REFERENCE_HEIGHT
        self._cache: dict[tuple[str, ...], bytes] = {}
        self._playwright = None
        self._browser = None
        self._page = None
        self.region = CaptureRegion(0, 0, width, height)

    @property
    def template_path(self) -> Path:
        path = TEMPLATE_ROOT / self.template / "hud.html"
        if not path.exists():
            raise FileNotFoundError(
                f"Unknown template {self.template!r}. Available: "
                f"{', '.join(available_templates())}"
            )
        return path

    def __enter__(self) -> Self:
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            args=["--force-color-profile=srgb", "--disable-lcd-text"]
        )
        self._page = self._browser.new_page(
            viewport={"width": self.width, "height": self.height},
            device_scale_factor=1,
        )
        self._page.goto(self.template_path.as_uri())
        self._page.evaluate(
            "opts => window.configure(opts)",
            {"scale": self.scale, "opacity": self.opacity},
        )
        self._page.evaluate("() => window.hudReady")
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        for closer in (self._page, self._browser):
            if closer is not None:
                closer.close()
        if self._playwright is not None:
            self._playwright.stop()

    def measure(self, sample_values: FrameValues, padding_rem: float = 2.0) -> CaptureRegion:
        """Find the band the HUD occupies, using worst-case text.

        The full width is always captured so that a longer readout can never be
        cut off horizontally; only the vertical extent is trimmed.
        """
        assert self._page is not None
        self._page.evaluate("v => window.applyFrame(v)", sample_values)
        top = self._page.evaluate(
            """
            (padRem) => {
              const rem = parseFloat(getComputedStyle(document.documentElement).fontSize);
              const boxes = [...document.querySelectorAll('.cluster')]
                  .map(el => el.getBoundingClientRect());
              if (!boxes.length) return 0;
              const highest = Math.min(...boxes.map(b => b.top));
              return Math.max(0, Math.floor(highest - padRem * rem));
            }
            """,
            padding_rem,
        )
        self.region = CaptureRegion(0, int(top), self.width, self.height - int(top))
        return self.region

    def render(self, values: FrameValues) -> bytes:
        """PNG bytes for one HUD state, cached by the text it displays."""
        key = tuple(values[k] for k in sorted(values))
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        assert self._page is not None
        self._page.evaluate("v => window.applyFrame(v)", values)
        png = self._page.screenshot(
            omit_background=True,
            type="png",
            clip={
                "x": self.region.x,
                "y": self.region.y,
                "width": self.region.width,
                "height": self.region.height,
            },
        )
        self._cache[key] = png
        return png

    @property
    def cache_size(self) -> int:
        return len(self._cache)
