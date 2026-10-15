from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import traceback
import webbrowser
from pathlib import Path
from typing import Any

from .analysis.analyzer import AnalyzeOptions, run_analysis
from .cli_args import build_parser
from .audio_processor import convert_to_clean_wav
from .cleaner import clean_segments
from .config import output_root, load_config, project_root, resource_root
from .downloader import download_audio, download_thumbnail
from .media_assets import default_thumbnail_path
from .exporters.json_exporter import export_json
from .exporters.markdown_exporter import export_markdown
from .exporters.srt_exporter import export_srt
from .preview import PreviewOptions, create_preview
from .schemas import TranscriptDocument, TranscriptMeta
from .transcriber import transcribe_audio
from .output_layout import ensure_media_subdirs, group_dir_from_artifact_path, media_group_dir, group_name_from_stem
from .utils import (
    AppError,
    RunLogger,
    apply_runtime_path_entries,
    build_initial_prompt,
    copy_source_file,
    local_created_at,
    normalize_device_compute,
    prepare_output_paths,
)


def _configure_text_streams() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_configure_text_streams()
try:
    from rich.console import Console

    console = Console()
except Exception:
    console = None


def _print(message: str) -> None:
    if console:
        console.print(message)
    else:
        print(message, flush=True)


def load_local_secrets() -> None:
    secrets_path = project_root() / "secrets.local.env"
    if not secrets_path.exists():
        return
    try:
        for line in secrets_path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, value = text.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        return


def _resolve_options(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    quality = args.quality or config["transcribe"]["quality"]
    profile = config["quality_profiles"].get(quality)
    if not profile:
        raise AppError(f"未知 quality：{quality}。可选值：fast / accurate / cpu_safe / high")

    language = args.language or config["transcribe"]["language"]
    if language.strip() == "":
        raise AppError("language 参数不能为空。")

    device = args.device or ("cpu" if quality == "cpu_safe" else config["transcribe"]["device"])
    if args.compute_type:
        compute_type = args.compute_type
    elif args.device == "cuda":
        compute_type = config["transcribe"]["compute_type"]
    else:
        compute_type = profile.get("preferred_compute_type") or config["transcribe"]["compute_type"]

    options = {
        "quality": quality,
        "language": language,
        "model": args.model or profile["model"],
        "device": device,
        "compute_type": compute_type,
        "compute_type_was_explicit": args.compute_type is not None,
        "beam_size": args.beam_size if args.beam_size is not None else profile["beam_size"],
        "vad_filter": args.vad_filter if args.vad_filter is not None else profile["vad_filter"],
        "word_timestamps": args.word_timestamps if args.word_timestamps is not None else profile["word_timestamps"],
        "condition_on_previous_text": (
            args.condition_on_previous_text
            if args.condition_on_previous_text is not None
            else config["transcribe"]["condition_on_previous_text"]
        ),
        "output_dir": Path(args.output_dir or config["app"]["output_dir"]),
        "timezone": config["app"].get("timezone", "Asia/Tokyo"),
        "terms_path": Path(config["dictionary"].get("terms_path", "dictionaries/vtuber_terms.txt")),
        "no_speech_prob_threshold": float(config["cleaner"].get("no_speech_prob_threshold", 0.9)),
    }
    return options


def transcribe_task(args: argparse.Namespace) -> dict[str, Path]:
    if not args.url and not args.input:
        raise AppError("请至少提供 --url 或 --input。")

    root = project_root()
    config = load_config()
    options = _resolve_options(args, config)
    proxy = args.proxy if args.proxy is not None else str(config.get("network", {}).get("proxy") or "").strip()
    output_dir = output_root(options["output_dir"], root=root)
    paths = prepare_output_paths(output_dir)
    logger = RunLogger(paths.log_file, debug=args.debug)
    logger.write("运行开始。")

    runtime = config.get("runtime", {})
    if runtime.get("auto_add_cuda_dll_paths", True):
        runtime_entries = runtime.get("cuda_dll_paths", [])
        path_roots = [root]
        bundled_root = resource_root()
        if bundled_root != root:
            path_roots.append(bundled_root)
        for path_root in path_roots:
            added_paths = apply_runtime_path_entries(path_root, runtime_entries, logger)
            for added_path in added_paths:
                _print(f"Runtime PATH: {added_path}")

    device_was_auto = options["device"] == "auto"
    device, compute_type, warnings = normalize_device_compute(
        quality=options["quality"],
        requested_device=options["device"],
        requested_compute_type=options["compute_type"] if options["compute_type_was_explicit"] else None,
        default_compute_type=options["compute_type"],
        logger=logger,
    )
    for warning in warnings:
        _print(f"警告：{warning}")

    _print("[1/6] 准备输入...")
    if args.input and args.url:
        _print("提示：同时提供了 --input 和 --url，将优先使用本地 --input。")
        logger.write("同时提供 input 和 url，优先使用 input。")

    title = ""
    source_url = ""
    input_file = ""

    _print("[2/6] 下载或定位音频...")
    if args.input:
        original_input = Path(args.input).expanduser()
        source_audio = copy_source_file(original_input, paths.audio_dir, paths.run_id)
        input_file = str(original_input)
    else:
        source_url = args.url or ""
        source_audio, title = download_audio(
            source_url,
            paths.audio_dir,
            paths.run_id,
            proxy,
            logger,
            cookies_from_browser=args.cookies_from_browser,
            cookies=args.cookies,
            remote_components=args.remote_components,
            download_start=args.download_start,
            download_end=args.download_end,
        )

    output_stem = source_audio.stem.removesuffix("_source")
    group_dir = media_group_dir(group_name_from_stem(output_stem), output_dir)
    media_dirs = ensure_media_subdirs(group_dir)
    media_source_audio = media_dirs["audio"] / source_audio.name
    if source_audio.resolve() != media_source_audio.resolve():
        shutil.move(str(source_audio), media_source_audio)
        source_audio = media_source_audio
    media_log_file = media_dirs["logs"] / paths.log_file.name
    if paths.log_file.exists() and paths.log_file.resolve() != media_log_file.resolve():
        shutil.move(str(paths.log_file), media_log_file)
    logger.log_file = media_log_file
    logger.write(f"媒体输出目录：{group_dir}")
    if source_url:
        try:
            thumbnail = download_thumbnail(
                source_url,
                media_dirs["thumbnails"],
                output_stem,
                proxy,
                logger,
                cookies_from_browser=args.cookies_from_browser,
                cookies=args.cookies,
                remote_components=args.remote_components,
            )
            if thumbnail:
                logger.write(f"thumbnail={thumbnail}")
        except Exception as exc:
            logger.write(f"thumbnail skipped: {exc}")

    prompt, prompt_warnings = build_initial_prompt(
        language=options["language"],
        project_root=root,
        default_terms_path=options["terms_path"],
        explicit_terms_file=args.terms_file,
        explicit_initial_prompt=args.initial_prompt,
        logger=logger,
        metadata_hint=title,
    )
    for warning in prompt_warnings:
        _print(f"警告：{warning}")

    clean_audio = media_dirs["audio"] / f"{output_stem}_clean_16k.wav"
    _print("[3/6] 转换音频格式...")
    convert_to_clean_wav(source_audio, clean_audio, logger)

    _print("[4/6] 加载 faster-whisper 模型...")
    logger.write(
        f"转写参数：quality={options['quality']}, model={options['model']}, language={options['language']}, "
        f"beam_size={options['beam_size']}, vad={options['vad_filter']}, words={options['word_timestamps']}, "
        f"device={device}, compute_type={compute_type}"
    )

    _print("[5/6] 开始语音识别...")
    raw_segments, info = transcribe_audio(
        audio_path=clean_audio,
        model_name=options["model"],
        device=device,
        compute_type=compute_type,
        language=options["language"],
        beam_size=options["beam_size"],
        vad_filter=options["vad_filter"],
        word_timestamps=options["word_timestamps"],
        condition_on_previous_text=options["condition_on_previous_text"],
        initial_prompt=prompt,
        device_was_auto=device_was_auto,
        logger=logger,
    )

    defer_temp_cleanup = bool(getattr(args, "defer_temp_cleanup", False))
    keep_clean_audio = args.keep_temp or defer_temp_cleanup or not bool(
        config.get("audio", {}).get("delete_clean_wav_after_transcribe", True)
    )
    record_clean_audio = args.keep_temp or not bool(
        config.get("audio", {}).get("delete_clean_wav_after_transcribe", True)
    )
    meta = TranscriptMeta(
        title=title,
        source_url=source_url,
        input_file=input_file,
        source_audio_file=str(source_audio),
        clean_audio_file=str(clean_audio) if record_clean_audio else "",
        download_start=args.download_start or "",
        download_end=args.download_end or "",
        language=options["language"],
        detected_language=info.get("detected_language", ""),
        language_probability=info.get("language_probability"),
        quality=options["quality"],
        model=options["model"],
        device=info.get("device", device),
        compute_type=info.get("compute_type", compute_type),
        beam_size=options["beam_size"],
        vad_filter=options["vad_filter"],
        word_timestamps=options["word_timestamps"],
        condition_on_previous_text=options["condition_on_previous_text"],
        initial_prompt_used=prompt,
        created_at=local_created_at(options["timezone"]),
        run_id=output_stem,
    )

    raw_document = TranscriptDocument(meta=meta, segments=raw_segments)
    cleaned_segments = clean_segments(raw_segments, no_speech_prob_threshold=options["no_speech_prob_threshold"])
    cleaned_document = TranscriptDocument(meta=meta, segments=cleaned_segments)

    _print("[6/6] 导出转写结果...")
    raw_json = media_dirs["transcripts"] / f"{output_stem}_raw_transcript.json"
    transcript_json = media_dirs["transcripts"] / f"{output_stem}_transcript.json"
    transcript_srt = media_dirs["transcripts"] / f"{output_stem}_transcript.srt"
    transcript_md = media_dirs["transcripts"] / f"{output_stem}_transcript.md"
    export_json(raw_document, raw_json)
    export_json(cleaned_document, transcript_json)
    export_srt(cleaned_document, transcript_srt)
    export_markdown(cleaned_document, transcript_md)
    from .output_layout import write_media_index
    write_media_index(group_dir)
    if not keep_clean_audio:
        try:
            clean_audio.unlink(missing_ok=True)
            logger.write(f"已删除转写临时 WAV：{clean_audio}")
        except OSError as exc:
            logger.write(f"警告：无法删除转写临时 WAV：{exc}")
    logger.write("运行完成。")
    cleanup_empty_staging(paths.audio_dir.parent, output_dir)

    _print("\n完成：")
    completed_paths = [source_audio, raw_json, transcript_json, transcript_srt, transcript_md, media_log_file]
    if keep_clean_audio and not defer_temp_cleanup and clean_audio.exists():
        completed_paths.insert(1, clean_audio)
    for path in completed_paths:
        _print(f"- {path}")
    result = {"transcript": transcript_json, "audio": source_audio, "output_dir": group_dir}
    if defer_temp_cleanup and not record_clean_audio:
        result["temporary_clean_audio"] = clean_audio
    print("ARTIFACT_RESULT " + json.dumps({key: str(value) for key, value in result.items()}), flush=True)
    return result


def handle_transcribe(args: argparse.Namespace) -> int:
    transcribe_task(args)
    return 0


def cleanup_empty_staging(staging_dir: Path, base_output_dir: Path | None = None) -> None:
    base = output_root(base_output_dir, root=project_root())
    staging_roots = [base / "_staging", base / "media" / "_staging"]
    target = staging_dir.resolve()
    if not any(target != root.resolve() and target.is_relative_to(root.resolve()) for root in staging_roots):
        return
    if not staging_dir.exists():
        return

    # prepare_output_paths creates empty child directories before the first
    # download. Remove only empty directories inside this staging run.
    candidates = sorted(
        (path for path in staging_dir.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for candidate in [*candidates, staging_dir]:
        if not candidate.exists():
            continue
        try:
            candidate.rmdir()
        except OSError:
            continue


def analyze_task(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config()
    analysis_config = config.get("analysis", {})
    input_file = Path(args.input).expanduser()
    provider = args.provider or analysis_config.get("provider", "gemini")
    if provider == "local":
        if os.environ.get("LIVE_TRANSCRIBER_SKILL_MODE") != "1":
            raise AppError("local provider 是 Skill 内部能力，请通过 $live-transcriber 调用。")
        model = args.model or analysis_config.get("local_model") or "codex-default"
        fallback_model = args.fallback_model or ""
    else:
        model = args.model or analysis_config.get("model", "gemini-3.5-flash-lite")
        fallback_model = args.fallback_model or analysis_config.get("fallback_model", "gemini-3.1-flash-lite")
    options = AnalyzeOptions(
        **{name: getattr(args, name, None) if getattr(args, name, None) is not None
           else config.get("features", {}).get(name, name != "character_profile")
           for name in ("character_profile", "summary", "study_notes")},
        input_file=input_file,
        provider=provider,
        profile=args.profile or analysis_config.get("profile", "multilingual_study"),
        source_language=args.source_language or analysis_config.get("source_language", ""),
        target_language=args.target_language or analysis_config.get("target_language", "zh"),
        model=model,
        fallback_model=fallback_model,
        api_key_env=analysis_config.get("api_key_env", "GEMINI_API_KEY"),
        chunk_minutes=float(args.chunk_minutes or analysis_config.get("chunk_minutes", 3)),
        max_segments_per_chunk=int(args.max_segments_per_chunk or analysis_config.get("max_segments_per_chunk", 40)),
        temperature=float(analysis_config.get("temperature", 0.2)),
        max_retries=int(analysis_config.get("max_retries", 2)),
        invalid_json_retries=max(0, int(
            getattr(args, "invalid_json_retries", None)
            if getattr(args, "invalid_json_retries", None) is not None
            else analysis_config.get("invalid_json_retries", 3)
        )),
        retry_backoff_seconds=float(analysis_config.get("retry_backoff_seconds", 3)),
        request_timeout_seconds=float(analysis_config.get("request_timeout_seconds", 300)),
        request_interval_seconds=float(analysis_config.get("request_interval_seconds", 6)),
        cache_enabled=bool(analysis_config.get("cache_enabled", True)),
        limit_chunks=args.limit_chunks,
        dry_run=args.dry_run,
        resume=args.resume,
        debug=args.debug,
        local_command=analysis_config.get("local_command", "codex"),
        local_timeout_seconds=float(analysis_config.get("local_timeout_seconds", 360)),
        local_reasoning_effort=str(analysis_config.get("local_reasoning_effort", "medium") or "").strip(),
        local_ignore_user_config=bool(analysis_config.get("local_ignore_user_config", True)),
        local_concurrency=max(1, int(
            getattr(args, "local_concurrency", None)
            if getattr(args, "local_concurrency", None) is not None
            else analysis_config.get("local_concurrency", 4)
        )),
        auto_repair_enabled=bool(
            getattr(args, "auto_repair", None)
            if getattr(args, "auto_repair", None) is not None
            else analysis_config.get("auto_repair_enabled", True)
        ),
        auto_repair_threshold=float(
            getattr(args, "auto_repair_threshold", None)
            if getattr(args, "auto_repair_threshold", None) is not None
            else analysis_config.get("auto_repair_threshold", 0.80)
        ),
    )
    result = run_analysis(options)
    _print("\n分析完成：" if not result.get("dry_run") else "\nDry-run 完成：")
    _print(f"- run_id: {result['run_id']}")
    _print(f"- output_dir: {result['output_dir']}")
    if result.get("dry_run"):
        _print(f"- preview: {result['dry_run_file']}")
    else:
        fallback_lines = result["document"].meta.fallback_lines
        if fallback_lines:
            _print(
                f"- 警告：{fallback_lines} 条字幕自动补译后仍缺少中文翻译；"
                "已用带标记的原文占位，时间轴保持完整。"
            )
        review_items = result["document"].meta.review_items
        if review_items:
            _print(
                f"- 提示：发现 {review_items} 条人工复查项，结果已生成但不应视为无风险完成。"
            )
        auto_repaired_lines = result["document"].meta.auto_repaired_lines
        if auto_repaired_lines:
            _print(
                f"- 高置信度自动修复：已采用 {auto_repaired_lines} 条；原文和修复日志均已保留。"
            )
        for path in result.get("output_paths", {}).values():
            _print(f"- {path}")
    result["exit_code"] = 0
    if not result.get("dry_run"):
        meta = result["document"].meta
        total_lines = sum(len(chunk.bilingual_lines) for chunk in result["document"].chunks)
        if result["document"].analysis_error or not total_lines or meta.fallback_lines == total_lines:
            result["exit_code"] = 1
        elif meta.failed_chunks or meta.fallback_lines:
            result["exit_code"] = 2
        print("ANALYSIS_STATUS " + json.dumps({
            "quality_status": meta.quality_status,
            "failed_chunks": meta.failed_chunks,
            "fallback_lines": meta.fallback_lines,
            "review_items": meta.review_items,
            "review_segments": meta.review_segments,
            "auto_repair_enabled": meta.auto_repair_enabled,
            "auto_repair_threshold": meta.auto_repair_threshold,
            "auto_repaired_lines": meta.auto_repaired_lines,
            "unresolved_review_items": meta.unresolved_review_items,
            "video_summary_error": result["document"].video_summary_error,
            "analysis_error": result["document"].analysis_error,
            "total_lines": total_lines,
        }, ensure_ascii=False), flush=True)
    print("ARTIFACT_RESULT " + json.dumps({"output_dir": str(result["output_dir"])}), flush=True)
    return result


def handle_analyze(args: argparse.Namespace) -> int:
    return analyze_task(args)["exit_code"]


def handle_preview(args: argparse.Namespace) -> int:
    result = _create_preview(args)
    _print_preview_result(result)
    return 0


def _create_preview(args: argparse.Namespace) -> dict[str, Path]:
    options = PreviewOptions(
        audio=Path(args.audio),
        subtitle=Path(args.subtitle),
        cover=Path(args.cover) if args.cover else None,
        output_dir=Path(args.output_dir) if args.output_dir else None,
        resolution=args.resolution,
        subtitle_name=args.subtitle_name,
        video_name=args.video_name,
        mode=getattr(args, "mode", "audio"),
        debug=args.debug,
    )
    return create_preview(options)


def _print_preview_result(result: dict[str, Path]) -> None:
    _print("\n媒体预览包已生成：")
    seen: set[str] = set()
    for path in result.values():
        if not isinstance(path, Path):
            continue
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        _print(f"- {path}")


def handle_pipeline(args: argparse.Namespace) -> int:
    modules = [part.strip() for part in str(args.modules or "").split(",") if part.strip()]
    allowed = {"transcribe", "analyze", "preview"}
    unknown = [module for module in modules if module not in allowed]
    if unknown:
        raise AppError(f"未知模块：{', '.join(unknown)}")
    if not modules:
        raise AppError("请至少勾选一个模块。")

    transcript: Path | None = Path(args.transcript).expanduser() if args.transcript else None
    audio: Path | None = Path(args.audio).expanduser() if args.audio else None
    subtitle: Path | None = Path(args.subtitle).expanduser() if args.subtitle else None
    exit_code = 0
    generated_wavs: list[Path] = []

    if "transcribe" in modules and not args.url and not args.input:
        raise AppError("模块一需要视频 URL 或本地音频/视频路径。")
    if "transcribe" not in modules and "analyze" in modules and not transcript:
        raise AppError("跳过模块一时，模块二需要选择 transcript.json。")
    if "analyze" not in modules and "preview" in modules and not subtitle:
        raise AppError("跳过模块二时，模块三需要选择 translation_zh.srt。")

    _print(f"[pipeline] 模块：{', '.join(modules)}")

    if "transcribe" in modules:
        transcribe_args = argparse.Namespace(
            url=args.url,
            input=args.input,
            output_dir=None,
            quality=args.quality,
            model=args.model,
            device=args.device,
            compute_type=args.compute_type,
            language=args.language,
            beam_size=args.beam_size,
            vad_filter=None,
            word_timestamps=args.word_timestamps,
            condition_on_previous_text=None,
            initial_prompt=None,
            terms_file=None,
            proxy=args.proxy,
            download_start=args.download_start,
            download_end=args.download_end,
            cookies_from_browser=args.cookies_from_browser,
            cookies=args.cookies,
            remote_components=args.remote_components,
            keep_temp=False,
            defer_temp_cleanup=True,
            debug=args.debug,
        )
        _print("[pipeline] 开始模块一：转写")
        transcribed = transcribe_task(transcribe_args)
        transcript = transcribed["transcript"]
        audio = transcribed["audio"]
        if transcribed.get("temporary_clean_audio"):
            generated_wavs.append(transcribed["temporary_clean_audio"])
        _print(f"[pipeline] transcript={transcript}")

    if "analyze" in modules:
        if not transcript:
            raise AppError("模块二需要 transcript.json。")
        analyze_args = argparse.Namespace(
            **{name: getattr(args, name, None) for name in ("character_profile", "summary", "study_notes")},
            input=str(transcript),
            provider=args.provider,
            profile=args.profile,
            source_language=None,
            target_language=None,
            model=args.analysis_model,
            fallback_model=args.analysis_fallback_model,
            chunk_minutes=None,
            max_segments_per_chunk=None,
            local_concurrency=args.local_concurrency,
            auto_repair=args.auto_repair,
            auto_repair_threshold=args.auto_repair_threshold,
            limit_chunks=args.limit_chunks,
            dry_run=False,
            resume=args.resume,
            debug=args.debug,
        )
        _print("[pipeline] 开始模块二：翻译与学习分析")
        analyzed = analyze_task(analyze_args)
        exit_code = analyzed["exit_code"]
        analysis_dir = analyzed["output_dir"]
        if exit_code == 1:
            _print("[pipeline] 翻译失败，已保留转写和复查资料；请配置后重试分析。")
            return 1
        subtitle = analysis_dir / "translation_zh.srt"
        _print(f"[pipeline] analysis={analysis_dir}")

    if "preview" in modules:
        if not audio and transcript:
            audio = _audio_from_transcript_meta(transcript)
        if not audio:
            raise AppError("模块三需要音频文件。")
        if not subtitle:
            raise AppError("模块三需要 translation_zh.srt。")
        cover = Path(args.cover).expanduser() if args.cover else _cover_for_artifacts(subtitle, audio)
        preview_args = argparse.Namespace(
            audio=str(audio),
            subtitle=str(subtitle),
            cover=str(cover) if cover else None,
            output_dir=None,
            resolution=args.resolution,
            subtitle_name="live_preview.zh.srt",
            video_name="live_preview.mp4",
            mode=args.preview_mode,
            debug=args.debug,
        )
        preview_label = "音频播放器" if args.preview_mode == "audio" else "通用视频预览"
        _print(f"[pipeline] 开始模块三：{preview_label}")
        preview_result = _create_preview(preview_args)
        _print_preview_result(preview_result)

    deleted_wavs = _cleanup_generated_pipeline_wavs(transcript, audio, generated_wavs=generated_wavs)
    for deleted_wav in deleted_wavs:
        _print(f"[pipeline] 已删除中间 WAV：{deleted_wav}")
    _print("[pipeline] 完整处理完成。" if exit_code == 0 else "[pipeline] 部分完成，请复查缺失的翻译。")
    return exit_code


def _cleanup_generated_pipeline_wavs(
    transcript: Path | None, audio: Path | None, *, generated_wavs: list[Path] | None = None,
) -> list[Path]:
    """Delete only this run's disposable WAVs, never the player's audio input."""
    group_dir = None
    for artifact in (transcript, audio):
        if artifact:
            group_dir = group_dir_from_artifact_path(artifact)
            if group_dir:
                break
    if group_dir is None:
        return []

    audio_dir = group_dir / "audio"
    if not audio_dir.exists():
        return []
    deleted: list[Path] = []
    for path in generated_wavs or []:
        path = path.resolve()
        if path.parent != audio_dir.resolve() or not path.name.endswith("_clean_16k.wav"):
            continue
        if audio is not None and path == audio.resolve():
            continue
        try:
            path.unlink(missing_ok=True)
            if not path.exists():
                deleted.append(path)
        except OSError as exc:
            _print(f"[pipeline] 警告：无法删除中间 WAV {path}：{exc}")
    return deleted


def _audio_from_transcript_meta(transcript: Path) -> Path | None:
    try:
        meta = json.loads(transcript.read_text(encoding="utf-8")).get("meta", {})
    except Exception:
        return None
    for key in ("source_audio_file", "clean_audio_file"):
        value = str(meta.get(key) or "").strip()
        if value:
            path = Path(value)
            if path.exists():
                return path
    return None


def _cover_for_artifacts(subtitle: Path, audio: Path) -> Path | None:
    from .output_layout import group_dir_from_artifact_path

    group_dir = group_dir_from_artifact_path(subtitle) or group_dir_from_artifact_path(audio)
    candidates: list[Path] = []
    if group_dir:
        thumbs = group_dir / "thumbnails"
        for pattern in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
            candidates.extend(thumbs.glob(pattern))
        candidates.extend(group_dir.glob("previews/*/cover.jpg"))
    global_thumbs = output_root(root=project_root()) / "thumbnails"
    for pattern in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
        candidates.extend(global_thumbs.glob(pattern))
    existing = [path for path in candidates if path.exists() and path.is_file()]
    return max(existing, key=lambda path: path.stat().st_mtime) if existing else default_thumbnail_path()


def handle_web(args: argparse.Namespace) -> int:
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise AppError("当前版本为本机应用，请使用 --host 127.0.0.1。")
    try:
        import uvicorn
    except ImportError as exc:
        raise AppError(f"缺少 Web 依赖：{exc}\n请先运行：pip install -r requirements.txt") from exc

    from .web.routes import is_port_available

    port = args.port
    while not is_port_available(args.host, port):
        if port >= args.port + 2:
            raise AppError(
                f"端口 {args.host}:{args.port} 已被占用。\n"
                f"请换一个端口，例如：python main.py web --port {args.port + 1}"
            )
        port += 1
    if args.host == "0.0.0.0":
        _print("警告：当前监听 0.0.0.0，可能暴露到局域网。仅在你明确需要时使用。")

    url_host = f"[{args.host}]" if ":" in args.host else args.host
    url = f"http://{url_host}:{port}"
    _print(f"多语言影音研析已启动：{url}")
    should_open_browser = args.open_browser or (getattr(sys, "frozen", False) and not args.no_browser)
    if should_open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run("app.web.server:app", host=args.host, port=port, reload=False)
    return 0


def run() -> None:
    project_root().mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(project_root() / "models"))
    if getattr(sys, "frozen", False) and (sys.stdout is None or sys.stderr is None):
        log_dir = output_root(root=project_root()) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stream = (log_dir / "web_launcher.log").open("a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = stream
        if sys.stderr is None:
            sys.stderr = stream
    _configure_text_streams()
    if getattr(sys, "frozen", False) and len(sys.argv) == 1:
        sys.argv.append("web")
    load_local_secrets()
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "transcribe":
            raise SystemExit(handle_transcribe(args))
        if args.command == "analyze":
            raise SystemExit(handle_analyze(args))
        if args.command == "preview":
            raise SystemExit(handle_preview(args))
        if args.command == "pipeline":
            raise SystemExit(handle_pipeline(args))
        if args.command == "web":
            raise SystemExit(handle_web(args))
        parser.print_help()
    except AppError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        if getattr(args, "debug", False):
            traceback.print_exc()
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"错误：发生未预期的问题：{exc}", file=sys.stderr)
        if getattr(args, "debug", False):
            traceback.print_exc()
        raise SystemExit(1)
