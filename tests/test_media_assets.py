from __future__ import annotations

from app.media_assets import default_thumbnail_path


def test_default_thumbnail_is_bundled_and_nonempty() -> None:
    thumbnail = default_thumbnail_path()

    assert thumbnail.is_file()
    assert thumbnail.stat().st_size > 0
    assert thumbnail.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
