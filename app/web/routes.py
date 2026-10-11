from __future__ import annotations

import os
import json
import re
import shutil
import socket
import sys
import io
import zipfile
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from ..config import output_root, load_config, project_root, resource_root, tool_path
from ..output_layout import group_dir_from_artifact_path, media_root
from ..utils import AppError, command_exists, is_cuda_available
from ..player_package import build_player_archive
# Keep established helper imports compatible; new domain callers use the catalog.
from ..artifact_catalog import (
    artifact_sets as _artifact_sets,
    artifact_group_dir as _artifact_group_dir,
    path_storage as _path_storage,
    recent_files as _recent_files_many,
    recent_dirs as _recent_dirs_many,
    latest_analysis as _latest_analysis,
    latest_preview as _latest_preview,
    mark_preview_freshness as _mark_preview_freshness,
    preview_transcript_run_id as _preview_transcript_run_id,
    run_id_from_audio_path as _run_id_from_audio_path,
    analysis_transcript_run_id as _analysis_transcript_run_id,
    analysis_result as _latest_analysis_result,
    preview_result as _preview_result,
)
from .jobs import cli_command_prefix, jobs
from .lifecycle import lifecycle
from ..media_assets import default_thumbnail_path
from .security import browse_file, open_file, open_folder, open_potplayer, read_text_preview, resolve_allowed_path


router = APIRouter()
templates = Jinja2Templates(directory=str(resource_root() / "app" / "web" / "templates"))


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    view = request.query_params.get("view") or request.cookies.get("transcriber_view", "simple")
    view = view if view in {"simple", "classic"} else "simple"
    response = templates.TemplateResponse(request, "classic.html" if view == "classic" else "workbench.html")
    response.set_cookie("transcriber_view", view, max_age=31536000, httponly=True, samesite="strict")
    return response


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
        "ffplay": command_exists("ffplay") or bool(tool_path("ffplay")),
        "ffprobe": command_exists("ffprobe") or bool(tool_path("ffprobe")),
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
    outputs = output_root(root=project_root())
    root = project_root()
    payload = {
        "audio": _recent_files_many([outputs / "media", outputs / "audio"], ["*.wav", "*.m4a", "*.webm", "*.mp3", "*.opus"], recursive=True),
        "transcripts": _recent_files_many([outputs / "media", outputs / "transcripts"], ["*_transcript.json", "*_transcript.srt", "*_transcript.md", "*_raw_transcript.json"], recursive=True),
        "analysis_runs": _recent_dirs_many([outputs / "media", outputs / "analysis"], recursive=True),
        "translation_srt": _recent_files_many([outputs / "media", outputs / "analysis"], ["translation_zh.srt"], recursive=True),
        "analysis_texts": _recent_files_many([outputs / "media", outputs / "analysis"], ["character_profile.md", "video_summary.md", "study_notes.md", "bilingual.md", "vocabulary.md", "grammar.md", "review.md", "run.log"], recursive=True),
        "covers": _recent_files_many([outputs / "media", outputs / "thumbnails"], ["*.jpg", "*.jpeg", "*.png", "*.webp"], recursive=True),
        "previews": _recent_dirs_many([outputs / "media", outputs / "previews"], recursive=True),
    }
    payload["latest_analysis"] = _latest_item([_latest_analysis(outputs / "media"), _latest_analysis(outputs / "analysis")])
    payload["latest_preview"] = _latest_item([_latest_preview(outputs / "media"), _latest_preview(outputs / "previews")])
    payload["artifact_sets"] = _artifact_sets(root)
    latest_preview = payload["latest_preview"]
    if latest_preview:
        analyses = {item["run_id"]: item["analysis"] for item in payload["artifact_sets"] if item.get("analysis")}
        run_id = _preview_transcript_run_id(Path(latest_preview["path"]), analyses)
        artifact = next((item for item in payload["artifact_sets"] if item["run_id"] == run_id), None)
        if artifact and artifact.get("analysis"):
            _mark_preview_freshness(latest_preview, artifact["analysis"])
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
    # The public web flow is intentionally API-backed.  The local Codex
    # provider is available only through the Skill wrapper and its gate.
    _append_optional(command, "--provider", "gemini")
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

    # Never allow a browser payload to enter the Skill-internal local route.
    _append_optional(command, "--provider", "gemini")
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
    _append_optional(command, "--preview-mode", payload.get("preview_mode") or "audio")
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
    mode = str(payload.get("mode") or "audio").strip().lower()
    if mode not in {"audio", "video", "potplayer"}:
        raise HTTPException(status_code=422, detail="预览模式必须是 audio、video 或 potplayer。")
    cover = str(payload.get("cover") or "").strip()
    artifact_run_id = str(payload.get("artifact_run_id") or "").strip()
    if not audio:
        raise HTTPException(status_code=400, detail="请选择音频文件。")
    if not subtitle:
        raise HTTPException(status_code=400, detail="请选择 translation_zh.srt。")
    if mode != "audio" and not cover:
        cover = _default_cover_for_preview(subtitle, audio)
    if mode != "audio" and not cover:
        raise HTTPException(status_code=400, detail="请选择封面图，或先放入当前媒体文件夹的 thumbnails。")
    output_dir = str(payload.get("output_dir") or "").strip()
    if not output_dir:
        output_dir = _default_preview_output_dir(artifact_run_id, subtitle, audio, mode=mode)
    command = cli_command_prefix() + ["preview", "--audio", audio, "--subtitle", subtitle, "--mode", mode]
    if cover:
        command += ["--cover", cover]
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
    target = resolve_allowed_path(path)
    try:
        target.relative_to((output_root(root=project_root())).resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="只能下载处理结果。")
    if target.suffix.lower() not in {".txt", ".md", ".json", ".srt", ".log", ".yaml", ".yml", ".mp4", ".pyw", ".cmd"}:
        raise HTTPException(status_code=403, detail="该文件类型不能下载。")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在。")
    _require_current_playback(str(target))
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


def _remove_player_export(directory: Path, staging: Path) -> None:
    target = directory.resolve()
    if target.parent != staging.resolve() or not target.name.startswith("player-export-"):
        raise ValueError("Invalid player export staging directory")
    shutil.rmtree(target)


@router.get("/api/artifacts/{run_id}/player-export")
def export_player(run_id: str):
    if jobs.has_running_jobs():
        raise HTTPException(status_code=409, detail="请等待当前任务完成后再导出播放器。")
    item = next((item for item in _artifact_sets(project_root()) if item["run_id"] == run_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="没有找到任务。")
    preview = item.get("preview") or {}
    if preview.get("audio_stale"):
        raise HTTPException(status_code=409, detail=preview["stale_reason"])
    files = preview.get("files", {})
    launcher = files.get("run_audio_subtitle_player.cmd")
    subtitle = files.get("audio_mode.bilingual.srt")
    audio = item.get("audio") or item.get("clean_audio")
    if not launcher or not subtitle or not audio:
        raise HTTPException(status_code=409, detail="请先生成音频播放器，再导出完整播放器包。")
    manifest_path = resolve_allowed_path(launcher["path"]).parent / "assets" / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        audio_reference = manifest.get("audio") or audio["path"]
    except (OSError, ValueError, AttributeError):
        raise HTTPException(status_code=409, detail="播放器清单不可读取，请重新生成播放器。")
    audio_path = resolve_allowed_path(str(audio_reference))
    subtitle_path = resolve_allowed_path(subtitle["path"], must_be_text=True)
    staging = output_root(root=project_root()) / "_staging"
    staging.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="player-export-", dir=staging)).resolve()
    archive = directory / "audio-player.zip"
    try:
        build_player_archive(audio_path, subtitle_path, archive)
        _require_current_playback(launcher["path"])
    except Exception as exc:
        _remove_player_export(directory, staging)
        if isinstance(exc, AppError):
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        raise
    return FileResponse(archive, filename="audio-player.zip", media_type="application/zip",
                        background=BackgroundTask(_remove_player_export, directory, staging))


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
    path = str(payload.get("path") or "")
    _require_current_playback(path)
    return open_file(path)


@router.get("/api/browse-file")
def browse_local_file(kind: str = Query("media")) -> dict[str, str]:
    return browse_file(kind)


@router.post("/api/open-potplayer")
def open_preview_in_potplayer(payload: dict[str, Any] = Body(...)) -> dict[str, str]:
    _require_current_playback(str(payload.get("video") or ""))
    return open_potplayer(str(payload.get("video") or ""), str(payload.get("subtitle") or ""))


def _default_preview_output_dir(artifact_run_id: str, subtitle: str, audio: str, *, mode: str = "audio") -> str:
    run_id = artifact_run_id or _transcript_run_id_from_analysis_file(Path(subtitle)) or _run_id_from_audio_path(Path(audio))
    group_dir = group_dir_from_artifact_path(Path(subtitle)) or group_dir_from_artifact_path(Path(audio))
    if group_dir:
        return str((group_dir / ("audio" if mode == "audio" else "video")).resolve())
    safe = _safe_run_id(run_id)
    if not safe:
        return ""
    return str((output_root(root=project_root()) / "previews" / safe / ("audio" if mode == "audio" else "video")).resolve())


def _default_cover_for_preview(subtitle: str, audio: str) -> str:
    group_dir = group_dir_from_artifact_path(Path(subtitle)) or group_dir_from_artifact_path(Path(audio))
    candidates: list[Path] = []
    if group_dir:
        thumbs = group_dir / "thumbnails"
        for pattern in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
            candidates.extend(thumbs.glob(pattern))
        candidates.extend(group_dir.glob("previews/*/cover.jpg"))
        candidates.extend(group_dir.glob("video/assets/cover.jpg"))
    legacy = output_root(root=project_root()) / "thumbnails"
    for pattern in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
        candidates.extend(legacy.glob(pattern))
    existing = [path for path in candidates if path.exists() and path.is_file()]
    if existing:
        return str(max(existing, key=lambda path: path.stat().st_mtime).resolve())
    fallback = default_thumbnail_path()
    return str(fallback.resolve()) if fallback.exists() and fallback.is_file() else ""


def _transcript_run_id_from_analysis_file(path: Path) -> str:
    return _analysis_transcript_run_id(path.parent)


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


def _require_current_playback(path_text: str) -> None:
    target = resolve_allowed_path(path_text)
    if target.suffix.lower() not in {".cmd", ".pyw", ".mp4", ".srt"}:
        return
    for item in _artifact_sets(project_root()):
        preview = item.get("preview") or {}
        for name, file in preview.get("files", {}).items():
            audio_entry = preview["files"].get("run_audio_subtitle_player.cmd") or preview["files"].get("audio_subtitle_player.pyw")
            mode = "audio" if audio_entry and Path(file["path"]).is_relative_to(Path(audio_entry["path"]).parent) else "video"
            if Path(file["path"]).resolve() == target and preview.get(f"{mode}_stale"):
                raise HTTPException(status_code=409, detail=preview["stale_reason"])


def _delete_artifact_run(run_id: str, root: Path) -> dict[str, Any]:
    item = next((value for value in _artifact_sets(root) if value["run_id"] == run_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="没有找到该次转录，文件可能已经被删除。")
    if not item.get("deletable") or not item.get("group_path"):
        raise HTTPException(status_code=409, detail="该文件组使用旧版跨目录结构，无法安全地从页面整组删除。")

    media_base = (output_root(root=root) / "media").resolve()
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


def is_port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False
