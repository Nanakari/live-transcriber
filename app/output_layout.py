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
    floating_overlay = group_dir / "video" / "floating_subtitle_overlay.pyw"
    floating_launcher = group_dir / "video" / "run_floating_subtitle_overlay.cmd"
    if floating_overlay.exists():
        floating_entry = floating_launcher if floating_launcher.exists() else floating_overlay
        lines.append(
            f"- [悬挂字幕框（直接启动）](<{floating_entry.relative_to(group_dir).as_posix()}>)"
            "（样式对齐 Gemini Live Translator compact 模式）"
        )
    audio_player = group_dir / "audio" / "audio_subtitle_player.pyw"
    audio_launcher = group_dir / "audio" / "run_audio_subtitle_player.cmd"
    audio_subtitle = group_dir / "audio" / "audio_mode.bilingual.srt"
    if audio_player.exists():
        audio_entry = audio_launcher if audio_launcher.exists() else audio_player
        lines.append(
            f"- [音频 + 悬挂字幕（直接启动）](<{audio_entry.relative_to(group_dir).as_posix()}>)"
            "（只播放音频，底部独立字幕框，不打开视频画面）"
        )
    if audio_subtitle.exists():
        lines.append(
            f"- [音频模式双语字幕](<{audio_subtitle.relative_to(group_dir).as_posix()}>)"
        )
    for transcript in sorted((group_dir / "transcripts").glob("*_transcript.md")):
        lines.append(f"- [原文转写](<{transcript.relative_to(group_dir).as_posix()}>)")
    analyses = sorted((group_dir / "analysis").glob("*/analysis.json"), key=lambda path: path.stat().st_mtime)
    if analyses:
        repaired_transcript = analyses[-1].parent / "repaired_transcript.srt"
        if repaired_transcript.exists():
            lines.append(
                f"- [高置信度修复原文](<{repaired_transcript.relative_to(group_dir).as_posix()}>)"
            )
        for name, label in [
            ("repaired_transcript.md", "高置信度修复原文（Markdown）"),
            ("repair_log.json", "修复日志（原文/修复/置信度）"),
            ("video_summary.md", "全片总结"),
            ("study_notes.md", "学习笔记"),
            ("bilingual.md", "完整双语稿"),
            ("review.md", "复查清单"),
        ]:
            path = analyses[-1].parent / name
            if path.exists():
                lines.append(f"- [{label}]({path.relative_to(group_dir).as_posix()})")
    lines.extend(["", "## 目录", "", "- audio/：源音频、默认音频模式字幕和独立音频播放器。",
                  "- video/：可选字幕视频，字幕位于 subtitles/，辅助文件位于 assets/。",
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
