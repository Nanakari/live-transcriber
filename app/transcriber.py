from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .schemas import TranscriptSegment, TranscriptWord
from .utils import AppError, RunLogger, format_timestamp


def _local_model_reference(model_name: str) -> str:
    explicit_root = os.environ.get("FASTER_WHISPER_MODEL_DIR", "").strip()
    if explicit_root:
        candidate = Path(explicit_root).expanduser() / model_name
    else:
        hf_home = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
        candidate = hf_home / "local_models" / model_name
    required = (candidate / "model.bin", candidate / "config.json")
    return str(candidate.resolve()) if all(path.is_file() and path.stat().st_size > 0 for path in required) else model_name


def _segment_to_model(segment: Any, fallback_id: int) -> TranscriptSegment:
    words: list[TranscriptWord] = []
    for word in getattr(segment, "words", None) or []:
        words.append(
            TranscriptWord(
                word=getattr(word, "word", ""),
                start=float(getattr(word, "start", 0.0) or 0.0),
                end=float(getattr(word, "end", 0.0) or 0.0),
                probability=getattr(word, "probability", None),
            )
        )

    start = float(getattr(segment, "start", 0.0) or 0.0)
    end = float(getattr(segment, "end", 0.0) or 0.0)
    return TranscriptSegment(
        id=int(getattr(segment, "id", fallback_id) or fallback_id),
        start=start,
        end=end,
        start_text=format_timestamp(start),
        end_text=format_timestamp(end),
        text=getattr(segment, "text", "") or "",
        avg_logprob=getattr(segment, "avg_logprob", None),
        no_speech_prob=getattr(segment, "no_speech_prob", None),
        words=words,
    )


def _transcribe_once(
    *,
    audio_path: Path,
    model_name: str,
    device: str,
    compute_type: str,
    language: str,
    beam_size: int,
    vad_filter: bool,
    word_timestamps: bool,
    condition_on_previous_text: bool,
    initial_prompt: str,
    logger: RunLogger,
) -> tuple[list[TranscriptSegment], dict[str, Any]]:
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as exc:
        raise AppError("未安装 faster-whisper。请运行：python -m pip install -r requirements.txt") from exc

    try:
        model_reference = _local_model_reference(model_name)
        if model_reference != model_name:
            logger.write(f"使用完整本地模型：{model_reference}")
        else:
            logger.write("本地完整模型不存在，将从 Hugging Face 下载；首次运行可能需要较长时间。")
        logger.write(f"加载模型：model={model_name}, device={device}, compute_type={compute_type}")
        model = WhisperModel(model_reference, device=device, compute_type=compute_type)
    except Exception as exc:
        logger.write(f"Model load exception: {type(exc).__name__}: {exc}")
        raise AppError(
            "faster-whisper 模型加载失败。\n"
            "可能原因：模型名称错误、Hugging Face 网络不可达、模型下载失败，或 CUDA/compute_type 不兼容。\n"
            "如果使用 CUDA，请检查 nvidia-smi、CUDA DLL 路径、nvidia-cublas-cu12 和 nvidia-cudnn-cu12。\n"
            "本程序不会在 CUDA 失败时自动改用 CPU；如需 CPU，请显式使用 --device cpu 或 --quality cpu_safe。"
        ) from exc

    transcribe_language = None if language == "auto" else language
    try:
        logger.write("开始转写音频。")
        segments_iter, info = model.transcribe(
            str(audio_path),
            language=transcribe_language,
            beam_size=beam_size,
            vad_filter=vad_filter,
            word_timestamps=word_timestamps,
            condition_on_previous_text=condition_on_previous_text,
            initial_prompt=initial_prompt or None,
        )
        duration = float(getattr(info, "duration", 0.0) or 0.0)
        raw_segments = []
        last_reported = -5.0
        for segment in segments_iter:
            raw_segments.append(segment)
            end = float(getattr(segment, "end", 0.0) or 0.0)
            if duration > 0:
                percent = max(0.0, min(100.0, end / duration * 100.0))
                if percent >= last_reported + 2.0:
                    logger.write(
                        f"whisper progress percent={percent:.1f} "
                        f"audio={format_timestamp(end)}/{format_timestamp(duration)} "
                        f"segments={len(raw_segments)}"
                    )
                    last_reported = percent
        if duration > 0:
            logger.write(
                f"whisper progress percent=100.0 "
                f"audio={format_timestamp(duration)}/{format_timestamp(duration)} "
                f"segments={len(raw_segments)}"
            )
    except Exception as exc:
        logger.write(f"Transcribe exception: {type(exc).__name__}: {exc}")
        raise AppError(
            "语音识别失败。\n"
            "如果使用 CUDA，可能是 CUDA DLL 缺失、显存不足或 compute_type 不兼容。\n"
            "本程序不会自动回退 CPU；请修复 CUDA，或显式使用 --device cpu / --quality cpu_safe。"
        ) from exc

    detected_language = getattr(info, "language", "") or ""
    language_probability = getattr(info, "language_probability", None)
    result_segments = [_segment_to_model(segment, index) for index, segment in enumerate(raw_segments, start=1)]
    return result_segments, {
        "device": device,
        "compute_type": compute_type,
        "detected_language": detected_language,
        "language_probability": language_probability,
    }


def transcribe_audio(
    *,
    audio_path: Path,
    model_name: str,
    device: str,
    compute_type: str,
    language: str,
    beam_size: int,
    vad_filter: bool,
    word_timestamps: bool,
    condition_on_previous_text: bool,
    initial_prompt: str,
    device_was_auto: bool,
    logger: RunLogger,
) -> tuple[list[TranscriptSegment], dict[str, Any]]:
    """Run faster-whisper without silently falling back from CUDA to CPU."""
    try:
        segments, info = _transcribe_once(
            audio_path=audio_path,
            model_name=model_name,
            device=device,
            compute_type=compute_type,
            language=language,
            beam_size=beam_size,
            vad_filter=vad_filter,
            word_timestamps=word_timestamps,
            condition_on_previous_text=condition_on_previous_text,
            initial_prompt=initial_prompt,
            logger=logger,
        )
    except AppError:
        raise

    if language == "auto" and not info.get("detected_language"):
        logger.write("警告：自动语言检测未返回结果，语言记录为 unknown。")
        info["detected_language"] = "unknown"

    if not segments:
        logger.write("警告：识别结果为空。")
    return segments, info





