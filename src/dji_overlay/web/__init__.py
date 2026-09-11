"""The web UI: browse a media directory, preview the HUD, queue renders.

``create_app`` is imported lazily so that ``import dji_overlay`` -- and the CLI
with it -- does not require FastAPI to be installed. The web extra is optional;
the CLI is not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .app import create_app

__all__ = ["create_app"]


def __getattr__(name: str) -> Any:
    if name == "create_app":
        from .app import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
