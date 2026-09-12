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
        "previews": group_dir / "video",
        "logs": group_dir / "logs",
        "thumbnails": group_dir / "thumbnails",
    }


def ensure_media_subdirs(group_dir: Path) -> dict[str, Path]:
    dirs = media_subdirs(group_dir)
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def write_media_index(group_dir: Path) -> None:
    """Give each task a concise entry point without moving historical files."""
    lines = ["# 媒体任务", "", "## 阅读与播放", ""]
    videos = list(group_dir.glob("video/live_preview.mp4")) or list(group_dir.glob("previews/*/live_preview.mp4"))
    if videos:
        lines.append(f"- [播放字幕视频]({videos[0].relative_to(group_dir).as_posix()})（任意 MP4 播放器）")
    for transcript in sorted((group_dir / "transcripts").glob("*_transcript.md")):
        lines.append(f"- [原文转写](<{transcript.relative_to(group_dir).as_posix()}>)")
    analyses = sorted((group_dir / "analysis").glob("*/analysis.json"), key=lambda path: path.stat().st_mtime)
    if analyses:
        for name, label in [("video_summary.md", "全片总结"), ("study_notes.md", "学习笔记"), ("bilingual.md", "完整双语稿"), ("review.md", "复查清单")]:
            path = analyses[-1].parent / name
            if path.exists():
                lines.append(f"- [{label}]({path.relative_to(group_dir).as_posix()})")
    lines.extend(["", "## 目录", "", "- video/：最终视频，字幕位于 subtitles/，辅助文件位于 assets/。",
                  "- audio/：保留的源音频，重建视频时使用。",
                  "- transcripts/：原文转写与精细时间轴。",
                  "- analysis/：按运行时间保留的分析文档，以上入口指向最新版本。",
                  "- thumbnails/、logs/：封面和处理记录。", ""])
    (group_dir / "README.md").write_text("\n".join(lines), encoding="utf-8")


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
