from __future__ import annotations

from pathlib import Path

from .config import resource_root


def default_thumbnail_path() -> Path:
    """Return the bundled fallback image used when no media thumbnail is available."""
    return resource_root() / "app" / "assets" / "neutral_cover.png"
