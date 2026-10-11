"""Index transcript, analysis and player results independently of the Web layer.

Legacy and current output layouts are read here. Callers authorize actions;
this module never starts jobs or deletes files.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .config import output_root
from .output_layout import group_dir_from_artifact_path
from .preview_state import preview_is_current


def run_id_from_audio_path(path: Path) -> str:
    name = path.name
    for marker in ["_source.", "_clean_16k."]:
        if marker in name:
            return name.split(marker, 1)[0]
    return ""


def recent_files(bases: list[Path], patterns: list[str], *, recursive: bool = False, limit: int = 30) -> list[dict[str, Any]]:
    seen: set[str] = set()
    files: list[Path] = []
    for base in bases:
        if not base.exists():
            continue
        for pattern in patterns:
            candidates = base.rglob(pattern) if recursive else base.glob(pattern)
            for path in candidates:
                if not path.is_file():
                    continue
                key = str(path.resolve()).lower()
                if key in seen:
                    continue
                seen.add(key)
                files.append(path)
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return [_file_item(path) for path in files[:limit]]


def recent_dirs(bases: list[Path], *, recursive: bool = False, limit: int = 20) -> list[dict[str, Any]]:
    seen: set[str] = set()
    dirs: list[Path] = []
    for base in bases:
        if not base.exists():
            continue
        candidates = base.rglob("*") if recursive else base.iterdir()
        for path in candidates:
            if not path.is_dir():
                continue
            if path.name in {"audio", "transcripts", "analysis", "previews", "video", "assets", "subtitles", "logs", "thumbnails", "chunks", "failed", "_cache"}:
                continue
            key = str(path.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            dirs.append(path)
    dirs.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return [_file_item(path) for path in dirs[:limit]]


def latest_analysis(base: Path) -> dict[str, Any] | None:
    if not base.exists():
        return None
    required = ["analysis.json", "bilingual.md", "translation_zh.srt", "review.md"]
    candidates = [path for path in base.rglob("*") if path.is_dir() and all((path / name).exists() for name in required)]
    if not candidates:
        return None
    run = max(candidates, key=lambda path: path.stat().st_mtime)
    result = analysis_result(run)
    if result:
        result["files"]["run.log"] = _file_item(run / "run.log") if (run / "run.log").exists() else None
    return result


def _is_preview_package(path: Path) -> bool:
    return any(
        (path / name).exists()
        for name in (
            "live_preview.mp4",
            "audio_subtitle_player.pyw",
            "run_audio_subtitle_player.cmd",
        )
    )


def latest_preview(base: Path) -> dict[str, Any] | None:
    if not base.exists():
        return None
    candidates = [path for path in base.rglob("*") if path.is_dir() and _is_preview_package(path)]
    if not candidates:
        return None
    run = max(candidates, key=lambda path: path.stat().st_mtime)
    return preview_result(run)


def artifact_sets(root: Path) -> list[dict[str, Any]]:
    outputs = output_root(root=root)
    transcript_by_run: dict[str, Path] = {}
    audio_by_run: dict[str, Path] = {}
    clean_audio_by_run: dict[str, Path] = {}
    transcript_bases = [outputs / "media", outputs / "transcripts"]
    audio_bases = [outputs / "media", outputs / "audio"]
    for base in transcript_bases:
        if not base.exists():
            continue
        for path in base.rglob("*_transcript.json"):
            if path.name.endswith("_raw_transcript.json"):
                continue
            run_id = _transcript_run_id(path) or path.name.replace("_transcript.json", "")
            current = transcript_by_run.get(run_id)
            if not current or path.stat().st_mtime > current.stat().st_mtime:
                transcript_by_run[run_id] = path
    for base in audio_bases:
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            run_id = run_id_from_audio_path(path)
            if not run_id:
                continue
            if "_clean_16k." in path.name:
                current = clean_audio_by_run.get(run_id)
                if not current or path.stat().st_mtime > current.stat().st_mtime:
                    clean_audio_by_run[run_id] = path
            elif "_source." in path.name:
                current = audio_by_run.get(run_id)
                if not current or path.stat().st_mtime > current.stat().st_mtime:
                    audio_by_run[run_id] = path

    run_ids: set[str] = set(transcript_by_run)
    analysis_by_transcript: dict[str, dict[str, Any]] = {}
    for analysis_dir in [outputs / "media", outputs / "analysis"]:
        if not analysis_dir.exists():
            continue
        for run in analysis_dir.rglob("*"):
            if not run.is_dir() or not (run / "analysis.json").exists():
                continue
            transcript_run_id = analysis_transcript_run_id(run)
            if transcript_run_id:
                current = analysis_by_transcript.get(transcript_run_id)
                item = analysis_result(run)
                if item and (not current or (run / "analysis.json").stat().st_mtime_ns >
                             (Path(current["path"]) / "analysis.json").stat().st_mtime_ns):
                    analysis_by_transcript[transcript_run_id] = item
                    run_ids.add(transcript_run_id)

    preview_by_transcript: dict[str, dict[str, Any]] = {}
    for previews_dir in [outputs / "media", outputs / "previews"]:
        if not previews_dir.exists():
            continue
        for run in previews_dir.rglob("*"):
            if not run.is_dir() or not _is_preview_package(run):
                continue
            transcript_run_id = preview_transcript_run_id(run, analysis_by_transcript)
            item = preview_result(run)
            current = preview_by_transcript.get(transcript_run_id)
            if transcript_run_id and item:
                if current:
                    # Audio may be regenerated independently of the video.
                    # Keep both modes and validate each against its own manifest.
                    files = dict(current["files"])
                    for name, file in item["files"].items():
                        previous = files.get(name)
                        if not previous or Path(file["path"]).stat().st_mtime_ns >= Path(previous["path"]).stat().st_mtime_ns:
                            files[name] = file
                    if Path(current["path"]).stat().st_mtime_ns > run.stat().st_mtime_ns:
                        item = dict(current)
                    item["files"] = files
                preview_by_transcript[transcript_run_id] = item
                run_ids.add(transcript_run_id)

    items: list[dict[str, Any]] = []
    for run_id in sorted(run_ids):
        transcript = transcript_by_run.get(run_id)
        source_audio = audio_by_run.get(run_id) or _audio_from_transcript(transcript)
        clean_audio = clean_audio_by_run.get(run_id) or _clean_audio_from_transcript(transcript)
        latest_mtime = max(
            [path.stat().st_mtime for path in [transcript, source_audio, clean_audio] if path and path.exists()]
            + [0]
        )
        analysis = analysis_by_transcript.get(run_id)
        preview = preview_by_transcript.get(run_id)
        if preview and analysis:
            mark_preview_freshness(preview, analysis)
        if analysis:
            latest_mtime = max(latest_mtime, Path(analysis["path"]).stat().st_mtime)
        if preview:
            latest_mtime = max(latest_mtime, Path(preview["path"]).stat().st_mtime)
        related_paths = [
            path
            for path in [
                transcript,
                source_audio,
                clean_audio,
                Path(analysis["path"]) if analysis else None,
                Path(preview["path"]) if preview else None,
            ]
            if path and path.exists()
        ]
        group_dir = artifact_group_dir(related_paths, root)
        storage_bytes, storage_file_count = path_storage(group_dir) if group_dir else _paths_storage(related_paths)
        items.append(
            {
                "run_id": run_id,
                "label": _artifact_label(transcript, run_id, source_audio),
                "modified": datetime_from_timestamp(latest_mtime),
                "transcript": _file_item(transcript) if transcript and transcript.exists() else None,
                "audio": _file_item(source_audio) if source_audio and source_audio.exists() else None,
                "clean_audio": _file_item(clean_audio) if clean_audio and clean_audio.exists() else None,
                "analysis": analysis,
                "preview": preview,
                "storage_bytes": storage_bytes,
                "storage_file_count": storage_file_count,
                "group_path": str(group_dir) if group_dir else "",
                "deletable": group_dir is not None,
            }
        )
    items.sort(key=lambda item: item["modified"], reverse=True)
    return items


def mark_preview_freshness(preview: dict, analysis: dict) -> None:
    subtitle = Path(analysis["files"]["translation_zh.srt"]["path"])
    for mode, name in (("audio", "run_audio_subtitle_player.cmd"), ("video", "live_preview.mp4")):
        file = preview["files"].get(name)
        if mode == "audio" and not file:
            file = preview["files"].get("audio_subtitle_player.pyw")
        preview[f"{mode}_stale"] = bool(file and not preview_is_current(Path(file["path"]).parent, subtitle))
    preview["stale"] = preview["audio_stale"] or preview["video_stale"]
    preview["stale_reason"] = "分析已更新或播放器版本无法确认，请重新生成音频播放器或字幕视频。" if preview["stale"] else ""


def artifact_group_dir(paths: list[Path], root: Path) -> Path | None:
    """Return one complete media group only when every related path belongs to it."""
    media_base = (output_root(root=root) / "media").resolve()
    groups: set[Path] = set()
    for path in paths:
        try:
            relative = path.resolve().relative_to(media_base)
        except (OSError, ValueError):
            return None
        if len(relative.parts) < 2:
            return None
        groups.add((media_base / relative.parts[0]).resolve())
    if len(groups) != 1:
        return None
    group_dir = next(iter(groups))
    if group_dir.parent != media_base or not group_dir.is_dir() or group_dir.is_symlink():
        return None
    return group_dir


def path_storage(path: Path) -> tuple[int, int]:
    return _paths_storage([path])


def _paths_storage(paths: list[Path]) -> tuple[int, int]:
    total_bytes = 0
    file_count = 0
    seen: set[str] = set()
    for path in paths:
        candidates = path.rglob("*") if path.is_dir() else [path]
        for candidate in candidates:
            try:
                if not candidate.is_file():
                    continue
                key = os.path.normcase(str(candidate.absolute()))
                if key in seen:
                    continue
                seen.add(key)
                total_bytes += candidate.stat().st_size
                file_count += 1
            except OSError:
                continue
    return total_bytes, file_count


def preview_transcript_run_id(run: Path, analysis_by_transcript: dict[str, dict[str, Any]]) -> str:
    manifest = run / "assets" / "manifest.json"
    if manifest.exists():
        try:
            candidate = str(json.loads(manifest.read_text(encoding="utf-8")).get("transcript_run_id") or "")
            if candidate in analysis_by_transcript:
                return candidate
        except (OSError, ValueError, AttributeError):
            pass
    if run.name.endswith("_potplayer"):
        candidate = run.name.removesuffix("_potplayer")
        if candidate in analysis_by_transcript:
            return candidate
    run_log = run / "run.log"
    if run_log.exists():
        try:
            text = run_log.read_text(encoding="utf-8", errors="replace")
            match = re.search(r"outputs[\\/]+(?:media[\\/]+[^\\/]+[\\/]+audio|audio)[\\/]+(.+?)_(?:source|clean_16k)\.", text)
            if match:
                candidate = match.group(1)
                if candidate in analysis_by_transcript:
                    return candidate
        except Exception:
            pass
    zh = _first_existing(run, ["live_preview.zh.srt", "subtitles/live_preview.zh.srt"])
    if zh:
        try:
            zh_stat = zh.stat()
            zh_prefix = zh.read_bytes()[:4096]
            for transcript_run_id, analysis in analysis_by_transcript.items():
                translation = Path(analysis.get("files", {}).get("translation_zh.srt", {}).get("path", ""))
                if translation.exists() and translation.stat().st_size == zh_stat.st_size and translation.read_bytes()[:4096] == zh_prefix:
                    return transcript_run_id
        except Exception:
            pass
    group = group_dir_from_artifact_path(run)
    if group:
        candidates = [run_id for run_id, analysis in analysis_by_transcript.items()
                      if group_dir_from_artifact_path(Path(analysis["path"])) == group]
        if len(candidates) == 1:
            return candidates[0]
    return ""


def _transcript_run_id(path: Path) -> str:
    try:
        import json

        meta = json.loads(path.read_text(encoding="utf-8")).get("meta", {})
        return str(meta.get("run_id") or "").strip()
    except Exception:
        return ""


def analysis_transcript_run_id(run: Path) -> str:
    try:
        import json

        meta = json.loads((run / "analysis.json").read_text(encoding="utf-8")).get("meta", {})
        return str(meta.get("transcript_run_id") or "").strip()
    except Exception:
        return ""


def _audio_from_transcript(transcript: Path | None) -> Path | None:
    if not transcript or not transcript.exists():
        return None
    try:
        import json

        meta = json.loads(transcript.read_text(encoding="utf-8")).get("meta", {})
        value = str(meta.get("source_audio_file") or "").strip()
        if not value:
            return None
        path = Path(value)
        return path if path.exists() and path.is_file() else None
    except Exception:
        return None


def _clean_audio_from_transcript(transcript: Path | None) -> Path | None:
    if not transcript or not transcript.exists():
        return None
    try:
        import json

        meta = json.loads(transcript.read_text(encoding="utf-8")).get("meta", {})
        value = str(meta.get("clean_audio_file") or "").strip()
        if not value:
            return None
        path = Path(value)
        return path if path.exists() and path.is_file() else None
    except Exception:
        return None


def _artifact_label(transcript: Path | None, fallback: str, source_audio: Path | None = None) -> str:
    candidates: list[str] = []
    if source_audio:
        candidates.append(source_audio.name)
    if transcript and transcript.exists():
        try:
            import json

            meta = json.loads(transcript.read_text(encoding="utf-8")).get("meta", {})
            candidates.extend(
                str(value or "").strip()
                for value in [meta.get("title"), meta.get("input_file"), meta.get("source_audio_file")]
            )
        except Exception:
            pass
    for value in candidates:
        label = _clean_artifact_name(value, fallback)
        if label:
            return label
    return _format_run_label(fallback)


def _clean_artifact_name(value: str, fallback: str) -> str:
    if not value:
        return ""
    name = Path(value).name
    for suffix in ["_source.m4a", "_source.webm", "_source.mp3", "_source.wav", "_clean_16k.wav", "_transcript.json"]:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    name = re.sub(r"_[0-9a-z]{4}$", "", name)
    mojibake_markers = ["�", "ï", "ã", "ð", "Â", "Õ"]
    if sum(1 for marker in mojibake_markers if marker in name) >= 1:
        return ""
    if any(0x80 <= ord(ch) <= 0x9F for ch in name):
        return ""
    if not name:
        return ""
    if (name == fallback or name.startswith(fallback)) and _looks_generated_run_id(fallback):
        return ""
    return name[:96]


def _looks_generated_run_id(value: str) -> bool:
    return bool(re.match(r"^\d{8}_\d{6}_\d{3}_[0-9a-z]{4}$", value or ""))


def _format_run_label(run_id: str) -> str:
    parts = run_id.split("_")
    if len(parts) >= 2 and len(parts[0]) == 8 and len(parts[1]) == 6:
        date = f"{parts[0][4:6]}-{parts[0][6:8]}"
        time = f"{parts[1][0:2]}:{parts[1][2:4]}"
        return f"文件组 {date} {time}"
    return run_id


def analysis_result(run: Path) -> dict[str, Any] | None:
    required = ["analysis.json", "bilingual.md", "translation_zh.srt", "review.md"]
    if not all((run / name).exists() for name in required):
        return None
    files = {name: _file_item(run / name) for name in required}
    for name in ("vocabulary.md", "grammar.md"):
        if (run / name).exists():
            files[name] = _file_item(run / name)
    if (run / "study_notes.md").exists():
        files["study_notes.md"] = _file_item(run / "study_notes.md")
    if (run / "video_summary.md").exists():
        files["video_summary.md"] = _file_item(run / "video_summary.md")
    if (run / "character_profile.md").exists():
        files["character_profile.md"] = _file_item(run / "character_profile.md")
    return {
        "name": run.name,
        "path": str(run.resolve()),
        "modified": datetime_from_timestamp(run.stat().st_mtime),
        "files": files,
        "chunks": len(list((run / "chunks").glob("*.json"))) if (run / "chunks").exists() else 0,
        "failed": len(list((run / "failed").iterdir())) if (run / "failed").exists() else 0,
    }


def preview_result(run: Path) -> dict[str, Any] | None:
    if not _is_preview_package(run):
        return None
    video_mode = (run / "live_preview.mp4").exists()
    audio_dir = run if (run / "audio_subtitle_player.pyw").exists() or (run / "run_audio_subtitle_player.cmd").exists() else run.parent / "audio"
    names = [
        "live_preview.mp4",
        "display.bilingual.srt",
        "live_preview.study.srt",
        "live_preview.bilingual.srt",
        "live_preview.srt",
        "live_preview.zh.srt",
        "live_preview.ja.srt",
        "floating_subtitle_overlay.pyw",
        "run_floating_subtitle_overlay.cmd",
        "audio_subtitle_player.pyw",
        "run_audio_subtitle_player.cmd",
        "audio_mode.bilingual.srt",
        "vocabulary.md",
        "grammar.md",
        "cover.jpg",
        "README_play.txt",
        "run.log",
    ]
    files = {name: _file_item(path) for name in names
             if (path := _first_existing(run, [name, "subtitles/" + name, "assets/" + name]))}
    for name in (
        "audio_subtitle_player.pyw",
        "run_audio_subtitle_player.cmd",
        "audio_mode.bilingual.srt",
        "README_play.txt",
    ):
        path = audio_dir / name
        if path.exists():
            files[name] = _file_item(path)
    if not video_mode:
        files.pop("README_play.txt", None)
        readme = audio_dir / "README_play.txt"
        if readme.exists():
            files["README_play.txt"] = _file_item(readme)
    return {
        "name": run.name,
        "path": str(run.resolve()),
        "modified": datetime_from_timestamp(run.stat().st_mtime),
        "files": files,
    }


def _first_existing(base: Path, names: list[str]) -> Path | None:
    for name in names:
        path = base / name
        if path.exists():
            return path
    return None


def _file_item(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "name": path.name,
        "path": str(path.resolve()),
        "size": stat.st_size,
        "modified": datetime_from_timestamp(stat.st_mtime),
        "is_dir": path.is_dir(),
    }


def datetime_from_timestamp(value: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(value).isoformat(timespec="seconds")
