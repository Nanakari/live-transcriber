from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .config import project_root
from .output_layout import ensure_media_subdirs, group_name_from_stem, media_group_dir
from .web.routes import _artifact_sets


def migrate_legacy_outputs(root: Path | None = None) -> dict[str, Any]:
    root = root or project_root()
    outputs = root / "outputs"
    moved: list[dict[str, str]] = []
    skipped: list[str] = []

    transcript_candidates = list((outputs / "transcripts").glob("*_transcript.json"))
    for item in _artifact_sets(root):
        label = str(item.get("label") or item.get("run_id") or "untitled_media")
        run_id = str(item.get("run_id") or "")
        group = media_group_dir(label, outputs)
        subdirs = ensure_media_subdirs(group)

        for key in ("audio", "clean_audio"):
            path = _path_from_item(item.get(key))
            if path:
                _move(path, subdirs["audio"], moved, skipped)

        related_prefixes = _related_prefixes(item, run_id)
        for audio in _matching_audio(outputs / "audio", related_prefixes):
            _move(audio, subdirs["audio"], moved, skipped)

        for transcript in _matching_transcripts(transcript_candidates, run_id, related_prefixes):
            _move(transcript, subdirs["transcripts"], moved, skipped)
        for transcript_sidecar in _matching_transcript_sidecars(outputs / "transcripts", related_prefixes):
            _move(transcript_sidecar, subdirs["transcripts"], moved, skipped)

        analysis = _path_from_item(item.get("analysis"))
        if analysis:
            _move(analysis, subdirs["analysis"], moved, skipped)

        preview = _path_from_item(item.get("preview"))
        if preview:
            _move(preview, subdirs["previews"], moved, skipped)

    for audio in _orphan_audio(outputs / "audio"):
        group = media_group_dir(group_name_from_stem(audio.stem), outputs)
        subdirs = ensure_media_subdirs(group)
        _move(audio, subdirs["audio"], moved, skipped)

    return {"moved": moved, "skipped": skipped}


def _path_from_item(item: Any) -> Path | None:
    if not isinstance(item, dict) or not item.get("path"):
        return None
    path = Path(str(item["path"]))
    return path if path.exists() else None


def _related_prefixes(item: dict[str, Any], run_id: str) -> set[str]:
    prefixes = {run_id} if run_id else set()
    for key in ("transcript", "audio", "clean_audio"):
        path = _path_from_item(item.get(key))
        if not path:
            continue
        stem = path.stem
        for suffix in ("_transcript", "_raw_transcript", "_source", "_clean_16k"):
            if stem.endswith(suffix):
                prefixes.add(stem[: -len(suffix)])
                break
    return {prefix for prefix in prefixes if prefix}


def _matching_audio(audio_dir: Path, prefixes: set[str]) -> list[Path]:
    matches: list[Path] = []
    if not audio_dir.exists():
        return matches
    for path in audio_dir.iterdir():
        if path.is_file() and any(path.name.startswith(f"{prefix}_") for prefix in prefixes):
            matches.append(path)
    return matches


def _orphan_audio(audio_dir: Path) -> list[Path]:
    if not audio_dir.exists():
        return []
    suffixes = {".wav", ".m4a", ".webm", ".mp3", ".opus"}
    return [path for path in audio_dir.iterdir() if path.is_file() and path.suffix.lower() in suffixes]


def _matching_transcripts(candidates: list[Path], run_id: str, prefixes: set[str]) -> list[Path]:
    matches: list[Path] = []
    for path in candidates:
        if not path.exists():
            continue
        if any(path.name.startswith(f"{prefix}_") for prefix in prefixes):
            matches.append(path)
            continue
        try:
            meta = json.loads(path.read_text(encoding="utf-8")).get("meta", {})
        except Exception:
            continue
        if str(meta.get("run_id") or "") == run_id:
            matches.append(path)
    return matches


def _matching_transcript_sidecars(transcript_dir: Path, prefixes: set[str]) -> list[Path]:
    matches: list[Path] = []
    if not transcript_dir.exists():
        return matches
    for path in transcript_dir.iterdir():
        if not path.is_file() or path.suffix.lower() not in {".md", ".srt"}:
            continue
        if any(path.name.startswith(f"{prefix}_transcript") for prefix in prefixes):
            matches.append(path)
    return matches


def _move(path: Path, target_dir: Path, moved: list[dict[str, str]], skipped: list[str]) -> None:
    if _is_in_media(path):
        skipped.append(str(path))
        return
    target_dir.mkdir(parents=True, exist_ok=True)
    target = _unique_target(target_dir / path.name)
    shutil.move(str(path), str(target))
    moved.append({"from": str(path), "to": str(target)})


def _unique_target(target: Path) -> Path:
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    for index in range(2, 1000):
        candidate = target.with_name(f"{stem}_{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Too many duplicate paths for {target}")


def _is_in_media(path: Path) -> bool:
    try:
        path.resolve().relative_to((project_root() / "outputs" / "media").resolve())
        return True
    except ValueError:
        return False


if __name__ == "__main__":
    result = migrate_legacy_outputs()
    print(f"moved={len(result['moved'])}")
    print(f"skipped={len(result['skipped'])}")
