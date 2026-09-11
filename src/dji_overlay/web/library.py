"""The media library: the files the server is allowed to touch.

The web UI never uploads footage. It browses a directory that was mounted into
the container (``/media``) or passed with ``--media-dir``, renders in place and
writes the result beside the source, exactly as the CLI does. That means every
path arriving from the browser is untrusted, and the one job of this module is
to make sure a request can never name a file outside the root.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..probe import VideoInfo, probe

__all__ = ["VIDEO_SUFFIXES", "Clip", "Library", "LibraryError"]

VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv", ".m4v", ".avi"})

SKIP_DIRECTORIES = frozenset({".git", "__pycache__", "node_modules", ".Trash"})

MAX_DEPTH = 4
"""How far below the media root to look. Deep trees are someone's whole archive."""


class LibraryError(Exception):
    """A path was outside the library, or is not something we can render."""


@dataclass(frozen=True, slots=True)
class Clip:
    path: str
    """POSIX-style path relative to the media root; the browser's handle on a file."""
    name: str
    size: int
    modified: float
    srt: str | None
    """The sidecar telemetry, relative to the root, when one sits next to the clip."""
    rendered: bool
    """True when an output for this clip already exists beside it."""

    def as_dict(self, info: VideoInfo | None = None) -> dict:
        data = {
            "path": self.path,
            "name": self.name,
            "size": self.size,
            "modified": self.modified,
            "srt": self.srt,
            "rendered": self.rendered,
            "ready": self.srt is not None,
        }
        if info is not None:
            data |= {
                "width": info.width,
                "height": info.height,
                "resolution": info.resolution,
                "fps": round(info.fps, 3),
                "duration": round(info.duration, 3),
                "frames": info.frame_count,
                "codec": info.codec,
                "has_audio": info.has_audio,
                "is_log": info.is_log,
            }
        return data


class Library:
    """A rooted view of one directory of footage."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise LibraryError(f"{self.root} is not a directory")
        self._probe_cache: dict[tuple[str, int, int], VideoInfo] = {}

    def resolve(self, relative: str) -> Path:
        """Turn a path from the browser into a real one inside the root.

        Absolute paths, ``..`` segments and symlinks that point out of the tree
        are all rejected rather than clamped: a request that names a file
        outside the library is a bug or an attack, never a near miss worth
        guessing at.
        """
        candidate = PurePosixPath(relative or "")
        if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
            raise LibraryError(f"{relative!r} is outside the media directory")

        full = (self.root / Path(*candidate.parts)).resolve()
        if full != self.root and not full.is_relative_to(self.root):
            raise LibraryError(f"{relative!r} is outside the media directory")
        return full

    def video(self, relative: str) -> Path:
        """Resolve a path that has to be an existing, renderable video."""
        path = self.resolve(relative)
        if not path.is_file():
            raise LibraryError(f"{relative!r} does not exist")
        if path.suffix.lower() not in VIDEO_SUFFIXES:
            raise LibraryError(f"{path.name} is not a video file")
        return path

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def sidecar(self, video: Path) -> Path | None:
        """DJI names the telemetry after the clip, and cases it inconsistently."""
        for suffix in (".srt", ".SRT", ".Srt"):
            candidate = video.with_suffix(suffix)
            if candidate.is_file():
                return candidate
        return None

    def clips(self) -> list[Clip]:
        """Every renderable clip under the root, newest first."""
        found: list[Clip] = []
        for path in self._walk(self.root, depth=0):
            if path.suffix.lower() not in VIDEO_SUFFIXES:
                continue
            if self._looks_like_our_output(path):
                continue
            stat = path.stat()
            srt = self.sidecar(path)
            found.append(
                Clip(
                    path=self.relative(path),
                    name=path.name,
                    size=stat.st_size,
                    modified=stat.st_mtime,
                    srt=self.relative(srt) if srt else None,
                    rendered=self._output_exists(path),
                )
            )
        found.sort(key=lambda clip: clip.modified, reverse=True)
        return found

    def info(self, video: Path) -> VideoInfo:
        """Probe a clip, remembering the answer for as long as the file is unchanged."""
        stat = video.stat()
        key = (str(video), stat.st_size, int(stat.st_mtime))
        cached = self._probe_cache.get(key)
        if cached is None:
            cached = probe(video)
            self._probe_cache[key] = cached
        return cached

    def default_output(self, video: Path, mode: str, alpha_codec: str) -> Path:
        """Where a render lands by default -- the same names the CLI chooses."""
        if mode == "alpha":
            extension = ".mov" if alpha_codec == "prores" else "_%06d.png"
            return video.with_name(f"{video.stem}_overlay{extension}")
        return video.with_name(f"{video.stem}_hud{video.suffix.lower()}")

    def _output_exists(self, video: Path) -> bool:
        return any(
            self.default_output(video, mode, codec).exists()
            for mode, codec in (("burn", "prores"), ("alpha", "prores"))
        )

    def _looks_like_our_output(self, path: Path) -> bool:
        """Don't offer a previous render back as source footage."""
        return path.stem.endswith(("_hud", "_overlay"))

    def _walk(self, directory: Path, depth: int):
        if depth > MAX_DEPTH:
            return
        try:
            entries = sorted(directory.iterdir())
        except PermissionError:
            return
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                if entry.name in SKIP_DIRECTORIES or entry.is_symlink():
                    continue
                yield from self._walk(entry, depth + 1)
            elif entry.is_file():
                yield entry
