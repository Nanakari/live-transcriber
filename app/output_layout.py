from __future__ import annotations

import re
from pathlib import Path

MEDIA_ROOT_NAME = "media"


def safe_name(value: str, *, max_len: int = 120) -> str:
    text = re.sub(r'[\\/:*?"<>|]+', " ", value or "")
    text = re.sub(r"\s+", " ", text).strip(" ._")
    return text[:max_len].strip(" ._")


def media_root(base_output_dir: Path | None = None) -> Path:
    from .config import project_root

    base = base_output_dir or (project_root() / "outputs")
    return base / MEDIA_ROOT_NAME


def media_group_dir(group_name: str, base_output_dir: Path | None = None) -> Path:
    safe = safe_name(group_name) or "untitled_media"
    return media_root(base_output_dir) / safe


def media_subdirs(group_dir: Path) -> dict[str, Path]:
    return {
        "audio": group_dir / "audio",
        "transcripts": group_dir / "transcripts",
        "analysis": group_dir / "analysis",
        "previews": group_dir / "previews",
        "logs": group_dir / "logs",
        "thumbnails": group_dir / "thumbnails",
    }


def ensure_media_subdirs(group_dir: Path) -> dict[str, Path]:
    dirs = media_subdirs(group_dir)
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def is_in_media(path: Path) -> bool:
    try:
        relative = path.resolve().relative_to(media_root().resolve())
    except ValueError:
        return False
    return len(relative.parts) >= 2


def group_dir_from_media_path(path: Path) -> Path | None:
    try:
        relative = path.resolve().relative_to(media_root().resolve())
    except ValueError:
        return None
    if not relative.parts:
        return None
    return media_root() / relative.parts[0]


def group_dir_from_artifact_path(path: Path) -> Path | None:
    group = group_dir_from_media_path(path)
    if group:
        return group
    return None


def group_name_from_stem(stem: str) -> str:
    text = stem
    for suffix in ("_source", "_clean_16k", "_raw_transcript", "_transcript"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    return safe_name(text) or safe_name(stem) or "untitled_media"
