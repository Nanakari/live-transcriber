from __future__ import annotations

import os
import re
import shutil
import socket
import sys
import io
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from ..config import load_config, project_root, resource_root, tool_path
from ..output_layout import ensure_media_subdirs, group_dir_from_artifact_path, group_name_from_stem, media_group_dir, media_root
from ..utils import command_exists, is_cuda_available
from .jobs import cli_command_prefix, jobs
from .lifecycle import lifecycle
from ..media_assets import default_thumbnail_path
from .security import browse_file, open_file, open_folder, open_potplayer, read_text_preview, resolve_allowed_path


router = APIRouter()
templates = Jinja2Templates(directory=str(resource_root() / "app" / "web" / "templates"))


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "workbench.html")


@router.get("/api/status")
def status() -> dict[str, Any]:
    config = load_config()
    analysis = config.get("analysis", {})
    return {
        "defaults": {"language": config["transcribe"]["language"], "quality": config["transcribe"]["quality"],
                     "device": config["transcribe"]["device"], "proxy": config["network"]["proxy"],
                     "features": config["features"]},
        "data_dir": str(project_root()),
        "models_dir": os.environ.get("HF_HOME", str(project_root() / "models")),
        "file_picker": os.name == "nt",
        "python": sys.executable,
        "ffmpeg": command_exists("ffmpeg") or bool(tool_path("ffmpeg")),
        "yt_dlp": _module_available("yt_dlp"),
        "faster_whisper": _module_available("faster_whisper"),
        "cuda": is_cuda_available(),
        "default_asr_profile": config.get("transcribe", {}).get("quality", ""),
        "gemini_model": analysis.get("model", ""),
        "gemini_fallback_model": analysis.get("fallback_model", ""),
        "gemini_api_key_detected": bool(os.environ.get(analysis.get("api_key_env", "GEMINI_API_KEY"))),
        "gemini_api_key_validated": False,
        "paths": {"media": str(media_root().resolve())},
    }


@router.post("/api/lifecycle/heartbeat")
def lifecycle_heartbeat(payload: dict[str, Any] = Body(default={})) -> dict[str, object]:
    return lifecycle.heartbeat(str(payload.get("client_id") or ""))


@router.post("/api/lifecycle/close")
def lifecycle_close(payload: dict[str, Any] = Body(default={})) -> dict[str, object]:
    return lifecycle.close(str(payload.get("client_id") or ""))


@router.post("/api/lifecycle/shutdown")
def lifecycle_shutdown() -> dict[str, object]:
    return lifecycle.shutdown_now()


@router.get("/api/lifecycle")
def lifecycle_status() -> dict[str, object]:
    return lifecycle.status()


@router.get("/api/files/recent")
def recent_files() -> dict[str, Any]:
    root = project_root()
    payload = {
        "audio": _recent_files_many([root / "outputs" / "media", root / "outputs" / "audio"], ["*.wav", "*.m4a", "*.webm", "*.mp3", "*.opus"], recursive=True),
        "transcripts": _recent_files_many([root / "outputs" / "media", root / "outputs" / "transcripts"], ["*_transcript.json", "*_transcript.srt", "*_transcript.md", "*_raw_transcript.json"], recursive=True),
        "analysis_runs": _recent_dirs_many([root / "outputs" / "media", root / "outputs" / "analysis"], recursive=True),
        "translation_srt": _recent_files_many([root / "outputs" / "media", root / "outputs" / "analysis"], ["translation_zh.srt"], recursive=True),
        "analysis_texts": _recent_files_many([root / "outputs" / "media", root / "outputs" / "analysis"], ["character_profile.md", "video_summary.md", "study_notes.md", "bilingual.md", "vocabulary.md", "grammar.md", "review.md", "run.log"], recursive=True),
        "covers": _recent_files_many([root / "outputs" / "media", root / "outputs" / "thumbnails"], ["*.jpg", "*.jpeg", "*.png", "*.webp"], recursive=True),
        "previews": _recent_dirs_many([root / "outputs" / "media", root / "outputs" / "previews"], recursive=True),
    }
    payload["latest_analysis"] = _latest_item([_latest_analysis(root / "outputs" / "media"), _latest_analysis(root / "outputs" / "analysis")])
    payload["latest_preview"] = _latest_item([_latest_preview(root / "outputs" / "media"), _latest_preview(root / "outputs" / "previews")])
    payload["artifact_sets"] = _artifact_sets(root)
    return payload


def _latest_item(items: list[dict[str, Any] | None]) -> dict[str, Any] | None:
    present = [item for item in items if item]
    return max(present, key=lambda item: item["modified"]) if present else None


@router.post("/api/transcribe")
def start_transcribe(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    config = load_config()
    default_quality = config.get("transcribe", {}).get("quality", "high")
    default_proxy = config.get("network", {}).get("proxy", "")
    command = cli_command_prefix() + ["transcribe"]
    url = str(payload.get("url") or "").strip()
    input_path = str(payload.get("input") or "").strip()
    if input_path:
        command += ["--input", input_path]
    elif url:
        command += ["--url", url]
    else:
        raise HTTPException(status_code=400, detail="请输入 URL 或本地音频/视频路径。")
    _append_optional(command, "--language", payload.get("language"))
    _append_optional(command, "--quality", payload.get("quality") or default_quality)
    _append_optional(command, "--model", payload.get("model"))
    _append_optional(command, "--device", payload.get("device"))
    _append_optional(command, "--compute-type", payload.get("compute_type"))
    _append_optional(command, "--beam-size", payload.get("beam_size"))
    _append_optional(command, "--download-start", payload.get("download_start"))
    _append_optional(command, "--download-end", payload.get("download_end"))
    command.extend(["--proxy", str(payload.get("proxy", default_proxy) or "")])
    cookies = str(payload.get("cookies") or "").strip()
    cookies_from_browser = "" if cookies else str(payload.get("cookies_from_browser") or "").strip()
    _append_optional(command, "--cookies-from-browser", cookies_from_browser)
    _append_optional(command, "--cookies", cookies)
    _append_optional(command, "--remote-components", payload.get("remote_components") or "ejs:github")
    word_timestamps = payload.get("word_timestamps")
    if word_timestamps is True:
        command.append("--word-timestamps")
    elif word_timestamps is False:
        command.append("--no-word-timestamps")
    if payload.get("debug"):
        command.append("--debug")
    return jobs.start("transcribe", command).public()


@router.post("/api/analyze")
def start_analyze(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    input_file = str(payload.get("input") or "").strip()
    if not input_file:
        raise HTTPException(status_code=400, detail="请选择 transcript.json。")
    api_key = str(payload.get("gemini_api_key") or "").strip()
    if not payload.get("dry_run"):
        _require_analysis_key(api_key)
    command = cli_command_prefix() + ["analyze", "--input", input_file]
    _append_feature_flags(command, payload)
    _append_optional(command, "--provider", payload.get("provider") or "gemini")
    _append_optional(command, "--profile", payload.get("profile") or "multilingual_study")
    _append_optional(command, "--model", payload.get("model"))
    _append_optional(command, "--fallback-model", payload.get("fallback_model"))
    limit = payload.get("limit_chunks")
    if limit not in (None, ""):
        command += ["--limit-chunks", str(limit)]
    if payload.get("dry_run"):
        command.append("--dry-run")
    if payload.get("resume"):
        command.append("--resume")
    if payload.get("debug"):
        command.append("--debug")
    env_overrides = _analysis_environment(payload, api_key)
    return jobs.start("analyze", command, env_overrides=env_overrides).public()


@router.post("/api/pipeline")
def start_pipeline(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    config = load_config()
    default_quality = config.get("transcribe", {}).get("quality", "high")
    default_proxy = config.get("network", {}).get("proxy", "")
    modules = _selected_pipeline_modules(payload)
    if not modules:
        raise HTTPException(status_code=400, detail="请至少勾选一个模块。")
    if "analyze" in modules:
        _require_analysis_key(str(payload.get("gemini_api_key") or "").strip())

    command = cli_command_prefix() + ["pipeline", "--modules", ",".join(modules)]
    _append_feature_flags(command, payload)
    url = str(payload.get("url") or "").strip()
    input_path = str(payload.get("input") or "").strip()
    if "transcribe" in modules:
        if input_path:
            command += ["--input", input_path]
        elif url:
            command += ["--url", url]
        else:
            raise HTTPException(status_code=400, detail="模块一需要输入视频 URL 或本地音频/视频路径。")
    else:
        _append_optional(command, "--transcript", payload.get("transcript"))
        _append_optional(command, "--audio", payload.get("audio"))
        _append_optional(command, "--subtitle", payload.get("subtitle"))

    _append_optional(command, "--language", payload.get("language"))
    _append_optional(command, "--quality", payload.get("quality") or default_quality)
    _append_optional(command, "--model", payload.get("model"))
    _append_optional(command, "--device", payload.get("device"))
    _append_optional(command, "--compute-type", payload.get("compute_type"))
    _append_optional(command, "--beam-size", payload.get("beam_size"))
    _append_optional(command, "--download-start", payload.get("download_start"))
    _append_optional(command, "--download-end", payload.get("download_end"))
    command.extend(["--proxy", str(payload.get("proxy", default_proxy) or "")])
    cookies = str(payload.get("cookies") or "").strip()
    cookies_from_browser = "" if cookies else str(payload.get("cookies_from_browser") or "").strip()
    _append_optional(command, "--cookies-from-browser", cookies_from_browser)
    _append_optional(command, "--cookies", cookies)
    _append_optional(command, "--remote-components", payload.get("remote_components") or "ejs:github")
    word_timestamps = payload.get("word_timestamps")
    if word_timestamps is True:
        command.append("--word-timestamps")
    elif word_timestamps is False:
        command.append("--no-word-timestamps")

    _append_optional(command, "--provider", payload.get("provider") or "gemini")
    _append_optional(command, "--profile", payload.get("profile") or "multilingual_study")
    _append_optional(command, "--analysis-model", payload.get("analysis_model"))
    _append_optional(command, "--analysis-fallback-model", payload.get("analysis_fallback_model"))
    limit = payload.get("limit_chunks")
    if limit not in (None, ""):
        command += ["--limit-chunks", str(limit)]
    if payload.get("resume"):
        command.append("--resume")
    _append_optional(command, "--cover", payload.get("cover"))
    _append_optional(command, "--resolution", payload.get("resolution") or "1280x720")
    if payload.get("debug"):
        command.append("--debug")

    api_key = str(payload.get("gemini_api_key") or "").strip()
    env_overrides = _analysis_environment(payload, api_key)
    return jobs.start("pipeline", command, env_overrides=env_overrides).public()


def _analysis_environment(payload: dict[str, Any], api_key: str) -> dict[str, str]:
    config = load_config()
    environment = {}
    if api_key:
        environment[config.get("analysis", {}).get("api_key_env", "GEMINI_API_KEY")] = api_key
    proxy = str(payload.get("proxy", config.get("network", {}).get("proxy", "")) or "").strip()
    if proxy:
        environment.update(HTTP_PROXY=proxy, HTTPS_PROXY=proxy)
    return environment


@router.post("/api/preview")
def start_preview(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    audio = str(payload.get("audio") or "").strip()
    subtitle = str(payload.get("subtitle") or "").strip()
    cover = str(payload.get("cover") or "").strip()
    artifact_run_id = str(payload.get("artifact_run_id") or "").strip()
    if not audio:
        raise HTTPException(status_code=400, detail="请选择音频文件。")
    if not subtitle:
        raise HTTPException(status_code=400, detail="请选择 translation_zh.srt。")
    if not cover:
        cover = _default_cover_for_preview(subtitle, audio)
    if not cover:
        raise HTTPException(status_code=400, detail="请选择封面图，或先放入当前媒体文件夹的 thumbnails。")
    output_dir = str(payload.get("output_dir") or "").strip()
    if not output_dir:
        output_dir = _default_preview_output_dir(artifact_run_id, subtitle, audio)
    command = cli_command_prefix() + ["preview", "--audio", audio, "--subtitle", subtitle, "--cover", cover]
    _append_optional(command, "--output-dir", output_dir)
    _append_optional(command, "--resolution", payload.get("resolution") or "1280x720")
    if payload.get("debug"):
        command.append("--debug")
    return jobs.start("preview", command, output_dir=output_dir or None).public()


def _selected_pipeline_modules(payload: dict[str, Any]) -> list[str]:
    pairs = [
        ("transcribe", bool(payload.get("module_transcribe"))),
        ("analyze", bool(payload.get("module_analyze"))),
        ("preview", bool(payload.get("module_preview"))),
    ]
    return [name for name, enabled in pairs if enabled]


def _append_feature_flags(command: list[str], payload: dict[str, Any]) -> None:
    for name in ("character_profile", "summary", "study_notes"):
        if name not in payload:
            continue
        value = payload[name]
        if not isinstance(value, bool):
            raise HTTPException(status_code=422, detail=f"{name} 必须是布尔值。")
        command.append("--" + ("" if value else "no-") + name.replace("_", "-"))


def _require_analysis_key(api_key: str) -> None:
    key_env = load_config()["analysis"]["api_key_env"]
    if not api_key and not os.environ.get(key_env, "").strip():
        raise HTTPException(status_code=400, detail="请先在设置中填写 Gemini API Key，或选择仅转写。")


@router.get("/api/download")
def download_result(path: str = Query(...)):
    target = resolve_allowed_path(path, must_be_text=True)
    try:
        target.relative_to((project_root() / "outputs").resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="只能下载处理结果。")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在。")
    return FileResponse(target, filename=target.name)


@router.get("/api/artifacts/{run_id}/export")
def export_artifact(run_id: str):
    item = next((item for item in _artifact_sets(project_root()) if item["run_id"] == run_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="没有找到任务。")
    paths: list[Path] = []
    if item.get("transcript"):
        transcript = Path(item["transcript"]["path"])
        paths.extend([transcript, transcript.with_suffix(".md"), transcript.with_suffix(".srt")])
    paths.extend(Path(value["path"]) for value in (item.get("analysis") or {}).get("files", {}).values()
                 if value and Path(value["path"]).suffix in {".md", ".srt", ".json"})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            target = resolve_allowed_path(str(path), must_be_text=True)
            if target.is_file():
                archive.write(target, target.name)
    buffer.seek(0)
    return StreamingResponse(buffer, media_type="application/zip",
                             headers={"Content-Disposition": 'attachment; filename="transcriber-results.zip"'})


@router.get("/api/jobs")
def list_jobs() -> list[dict[str, Any]]:
    return jobs.list_jobs()


@router.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    return jobs.get_job(job_id).public()


@router.get("/api/jobs/{job_id}/log")
def get_job_log(job_id: str) -> dict[str, Any]:
    job = jobs.get_job(job_id)
    return {"job_id": job_id, "lines": list(job.recent_logs)}


@router.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str) -> dict[str, Any]:
    return jobs.stop(job_id)


@router.delete("/api/artifacts/{run_id}")
def delete_artifact(run_id: str) -> dict[str, Any]:
    if jobs.has_running_jobs():
        raise HTTPException(status_code=409, detail="有任务正在运行，请等待任务结束后再删除文件组。")
    return _delete_artifact_run(run_id, project_root())


@router.get("/api/file")
def file_preview(path: str = Query(...)) -> dict[str, object]:
    return read_text_preview(path)


@router.post("/api/open-folder")
def open_output_folder(payload: dict[str, Any] = Body(...)) -> dict[str, str]:
    return open_folder(str(payload.get("path") or ""))


@router.post("/api/open-file")
def open_output_file(payload: dict[str, Any] = Body(...)) -> dict[str, str]:
    return open_file(str(payload.get("path") or ""))


@router.get("/api/browse-file")
def browse_local_file(kind: str = Query("media")) -> dict[str, str]:
    return browse_file(kind)


@router.post("/api/open-potplayer")
def open_preview_in_potplayer(payload: dict[str, Any] = Body(...)) -> dict[str, str]:
    return open_potplayer(str(payload.get("video") or ""), str(payload.get("subtitle") or ""))



def _default_preview_output_dir(artifact_run_id: str, subtitle: str, audio: str) -> str:
    run_id = artifact_run_id or _transcript_run_id_from_analysis_file(Path(subtitle)) or _run_id_from_audio_path(Path(audio))
    group_dir = group_dir_from_artifact_path(Path(subtitle)) or group_dir_from_artifact_path(Path(audio))
    if group_dir:
        return str((ensure_media_subdirs(group_dir)["previews"] / "potplayer").resolve())
    safe = _safe_run_id(run_id)
    if not safe:
        return ""
    return str((project_root() / "outputs" / "previews" / f"{safe}_potplayer").resolve())


def _default_cover_for_preview(subtitle: str, audio: str) -> str:
    group_dir = group_dir_from_artifact_path(Path(subtitle)) or group_dir_from_artifact_path(Path(audio))
    candidates: list[Path] = []
    if group_dir:
        thumbs = group_dir / "thumbnails"
        for pattern in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
            candidates.extend(thumbs.glob(pattern))
        candidates.extend(group_dir.glob("previews/*/cover.jpg"))
    legacy = project_root() / "outputs" / "thumbnails"
    for pattern in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
        candidates.extend(legacy.glob(pattern))
    existing = [path for path in candidates if path.exists() and path.is_file()]
    if existing:
        return str(max(existing, key=lambda path: path.stat().st_mtime).resolve())
    fallback = default_thumbnail_path()
    return str(fallback.resolve()) if fallback.exists() and fallback.is_file() else ""


def _transcript_run_id_from_analysis_file(path: Path) -> str:
    analysis_json = path.parent / "analysis.json"
    if not analysis_json.exists():
        return ""
    try:
        import json

        meta = json.loads(analysis_json.read_text(encoding="utf-8")).get("meta", {})
        return str(meta.get("transcript_run_id") or "").strip()
    except Exception:
        return ""


def _run_id_from_audio_path(path: Path) -> str:
    name = path.name
    for marker in ["_source.", "_clean_16k."]:
        if marker in name:
            return name.split(marker, 1)[0]
    return ""


def _safe_run_id(run_id: str) -> str:
    text = run_id.strip()
    if not text:
        return ""
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", text)[:120]
def _module_available(module_name: str) -> bool:
    try:
        __import__(module_name)
        return True
    except Exception:
        return False


def _append_optional(command: list[str], flag: str, value: Any) -> None:
    if value not in (None, ""):
        command.extend([flag, str(value)])


def _recent_files(base: Path, patterns: list[str], *, recursive: bool = False, limit: int = 30) -> list[dict[str, Any]]:
    if not base.exists():
        return []
    files: list[Path] = []
    for pattern in patterns:
        files.extend(base.rglob(pattern) if recursive else base.glob(pattern))
    files = [path for path in files if path.is_file()]
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return [_file_item(path) for path in files[:limit]]


def _recent_files_many(bases: list[Path], patterns: list[str], *, recursive: bool = False, limit: int = 30) -> list[dict[str, Any]]:
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


def _recent_dirs(base: Path, limit: int = 20) -> list[dict[str, Any]]:
    if not base.exists():
        return []
    dirs = [path for path in base.iterdir() if path.is_dir()]
    dirs.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return [_file_item(path) for path in dirs[:limit]]


def _recent_dirs_many(bases: list[Path], *, recursive: bool = False, limit: int = 20) -> list[dict[str, Any]]:
    seen: set[str] = set()
    dirs: list[Path] = []
    for base in bases:
        if not base.exists():
            continue
        candidates = base.rglob("*") if recursive else base.iterdir()
        for path in candidates:
            if not path.is_dir():
                continue
            if path.name in {"audio", "transcripts", "analysis", "previews", "logs", "thumbnails", "chunks", "failed", "_cache"}:
                continue
            key = str(path.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            dirs.append(path)
    dirs.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return [_file_item(path) for path in dirs[:limit]]


def _latest_analysis(base: Path) -> dict[str, Any] | None:
    if not base.exists():
        return None
    required = ["analysis.json", "bilingual.md", "translation_zh.srt", "review.md"]
    candidates = [path for path in base.rglob("*") if path.is_dir() and all((path / name).exists() for name in required)]
    if not candidates:
        return None
    run = max(candidates, key=lambda path: path.stat().st_mtime)
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
    files["run.log"] = _file_item(run / "run.log") if (run / "run.log").exists() else None
    return {
        "name": run.name,
        "path": str(run.resolve()),
        "modified": datetime_from_timestamp(run.stat().st_mtime),
        "files": files,
        "chunks": len(list((run / "chunks").glob("*.json"))) if (run / "chunks").exists() else 0,
        "failed": len(list((run / "failed").iterdir())) if (run / "failed").exists() else 0,
    }


def _latest_preview(base: Path) -> dict[str, Any] | None:
    if not base.exists():
        return None
    candidates = [path for path in base.rglob("*") if path.is_dir() and (path / "live_preview.mp4").exists()]
    if not candidates:
        return None
    run = max(candidates, key=lambda path: path.stat().st_mtime)
    names = [
        "live_preview.mp4",
        "live_preview.study.srt",
        "live_preview.bilingual.srt",
        "live_preview.srt",
        "live_preview.zh.srt",
        "live_preview.ja.srt",
        "vocabulary.md",
        "grammar.md",
        "cover.jpg",
        "README_play.txt",
        "run.log",
    ]
    files = {name: _file_item(run / name) for name in names if (run / name).exists()}
    return {
        "name": run.name,
        "path": str(run.resolve()),
        "modified": datetime_from_timestamp(run.stat().st_mtime),
        "files": files,
    }


def _artifact_sets(root: Path) -> list[dict[str, Any]]:
    transcript_by_run: dict[str, Path] = {}
    audio_by_run: dict[str, Path] = {}
    clean_audio_by_run: dict[str, Path] = {}
    transcript_bases = [root / "outputs" / "media", root / "outputs" / "transcripts"]
    audio_bases = [root / "outputs" / "media", root / "outputs" / "audio"]
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
            run_id = _run_id_from_audio_path(path)
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
    for analysis_dir in [root / "outputs" / "media", root / "outputs" / "analysis"]:
        if not analysis_dir.exists():
            continue
        for run in analysis_dir.rglob("*"):
            if not run.is_dir() or not (run / "analysis.json").exists():
                continue
            transcript_run_id = _analysis_transcript_run_id(run)
            if transcript_run_id:
                current = analysis_by_transcript.get(transcript_run_id)
                item = _latest_analysis_result(run)
                if item and (not current or item["modified"] > current["modified"]):
                    analysis_by_transcript[transcript_run_id] = item
                    run_ids.add(transcript_run_id)

    preview_by_transcript: dict[str, dict[str, Any]] = {}
    for previews_dir in [root / "outputs" / "media", root / "outputs" / "previews"]:
        if not previews_dir.exists():
            continue
        for run in previews_dir.rglob("*"):
            if not run.is_dir() or not (run / "live_preview.mp4").exists():
                continue
            transcript_run_id = _preview_transcript_run_id(run, analysis_by_transcript)
            item = _preview_result(run)
            current = preview_by_transcript.get(transcript_run_id)
            if transcript_run_id and item and (not current or item["modified"] > current["modified"]):
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
        group_dir = _artifact_group_dir(related_paths, root)
        storage_bytes, storage_file_count = _path_storage(group_dir) if group_dir else _paths_storage(related_paths)
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


def _artifact_group_dir(paths: list[Path], root: Path) -> Path | None:
    """Return one complete media group only when every related path belongs to it."""
    media_base = (root / "outputs" / "media").resolve()
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


def _path_storage(path: Path) -> tuple[int, int]:
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


def _delete_artifact_run(run_id: str, root: Path) -> dict[str, Any]:
    item = next((value for value in _artifact_sets(root) if value["run_id"] == run_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="没有找到该次转录，文件可能已经被删除。")
    if not item.get("deletable") or not item.get("group_path"):
        raise HTTPException(status_code=409, detail="该文件组使用旧版跨目录结构，无法安全地从页面整组删除。")

    media_base = (root / "outputs" / "media").resolve()
    target = Path(str(item["group_path"])).resolve()
    if target.parent != media_base or target == media_base or not target.is_dir() or target.is_symlink():
        raise HTTPException(status_code=400, detail="拒绝删除：目标不是有效的媒体文件组目录。")

    storage_bytes, storage_file_count = _path_storage(target)
    try:
        shutil.rmtree(target)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"删除文件组失败：{exc}") from exc
    return {
        "run_id": run_id,
        "deleted_path": str(target),
        "deleted_bytes": storage_bytes,
        "deleted_file_count": storage_file_count,
    }



def _preview_transcript_run_id(run: Path, analysis_by_transcript: dict[str, dict[str, Any]]) -> str:
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
    zh = run / "live_preview.zh.srt"
    if zh.exists():
        try:
            zh_stat = zh.stat()
            zh_prefix = zh.read_bytes()[:4096]
            for transcript_run_id, analysis in analysis_by_transcript.items():
                translation = Path(analysis.get("files", {}).get("translation_zh.srt", {}).get("path", ""))
                if translation.exists() and translation.stat().st_size == zh_stat.st_size and translation.read_bytes()[:4096] == zh_prefix:
                    return transcript_run_id
        except Exception:
            pass
    return ""


def _transcript_run_id(path: Path) -> str:
    try:
        import json

        meta = json.loads(path.read_text(encoding="utf-8")).get("meta", {})
        return str(meta.get("run_id") or "").strip()
    except Exception:
        return ""


def _analysis_transcript_run_id(run: Path) -> str:
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


def _latest_analysis_result(run: Path) -> dict[str, Any] | None:
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


def _preview_result(run: Path) -> dict[str, Any] | None:
    if not (run / "live_preview.mp4").exists():
        return None
    names = [
        "live_preview.mp4",
        "live_preview.study.srt",
        "live_preview.bilingual.srt",
        "live_preview.srt",
        "live_preview.zh.srt",
        "live_preview.ja.srt",
        "vocabulary.md",
        "grammar.md",
        "cover.jpg",
        "README_play.txt",
        "run.log",
    ]
    return {
        "name": run.name,
        "path": str(run.resolve()),
        "modified": datetime_from_timestamp(run.stat().st_mtime),
        "files": {name: _file_item(run / name) for name in names if (run / name).exists()},
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


def is_port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False










