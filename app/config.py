from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import os
import sys
from typing import Any

import yaml

from .utils import AppError


DEFAULT_CONFIG: dict[str, Any] = {
    "app": {"output_dir": "outputs", "timezone": "Asia/Tokyo"},
    "transcribe": {
        "quality": "fast",
        "language": "auto",
        "device": "auto",
        "compute_type": "int8_float16",
        "condition_on_previous_text": True,
    },
    "quality_profiles": {
        "fast": {"model": "small", "beam_size": 5, "vad_filter": True, "word_timestamps": False},
        "accurate": {"model": "medium", "beam_size": 8, "vad_filter": True, "word_timestamps": True},
        "cpu_safe": {
            "model": "medium",
            "beam_size": 5,
            "vad_filter": True,
            "word_timestamps": False,
            "preferred_device": "cpu",
            "preferred_compute_type": "int8",
        },
        "high": {
            "model": "large-v3-turbo",
            "beam_size": 5,
            "vad_filter": True,
            "word_timestamps": True,
            "preferred_compute_type": "int8_float16",
        },
    },
    "audio": {
        "sample_rate": 16000,
        "channels": 1,
        "delete_clean_wav_after_transcribe": True,
        "delete_source_m4a_after_preview": True,
    },
    "network": {"proxy": ""},
    "features": {"summary": True, "study_notes": True, "character_profile": False},
    "dictionary": {"terms_path": "dictionaries/vtuber_terms.txt"},
    "cleaner": {"no_speech_prob_threshold": 0.9},
    "runtime": {
        "auto_add_cuda_dll_paths": True,
        "cuda_dll_paths": [
            ".venv/Lib/site-packages/nvidia/cublas/bin",
            ".venv/Lib/site-packages/nvidia/cudnn/bin",
            ".venv/Lib/site-packages/nvidia/cuda_nvrtc/bin",
        ],
    },
    "analysis": {
        "provider": "gemini",
        "model": "gemini-3.5-flash-lite",
        "fallback_model": "gemini-3.1-flash-lite",
        "api_key_env": "GEMINI_API_KEY",
        "source_language": "",
        "target_language": "zh",
        "profile": "multilingual_study",
        "chunk_minutes": 2,
        "max_segments_per_chunk": 20,
        "temperature": 0.2,
        "max_retries": 6,
        "retry_backoff_seconds": 10,
        "request_timeout_seconds": 300,
        "request_interval_seconds": 6,
        "cache_enabled": True,
        "output_formats": ["json", "markdown", "srt"],
    },
}


def project_root() -> Path:
    override = os.environ.get("LIVE_TRANSCRIBER_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        # A build kept inside the source project shares its data with source runs.
        for parent in Path(sys.executable).resolve().parents:
            if (parent / "main.py").is_file() and (parent / "app" / "config.py").is_file():
                return parent
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LiveTranscriber"
    return Path(__file__).resolve().parents[1]


def resource_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS).resolve()  # type: ignore[attr-defined]
    return Path(__file__).resolve().parents[1]


def tool_path(name: str) -> Path | None:
    """Find shipped tools independently of the writable data directory."""
    suffix = ".exe" if os.name == "nt" else ""
    for root in (resource_root(), Path(sys.executable).resolve().parent, project_root()):
        candidate = root / "tools" / f"{name}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_config_file(config_path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise AppError(f"{config_path.name} 格式错误：{exc}") from exc
    except UnicodeDecodeError as exc:
        raise AppError(f"{config_path.name} 必须使用 UTF-8 编码。") from exc
    if not isinstance(loaded, dict):
        raise AppError(f"{config_path.name} 顶层必须是 YAML 字典。")
    return loaded


def load_config() -> dict[str, Any]:
    root = project_root()
    config = deepcopy(DEFAULT_CONFIG)
    for name in ("config.yaml", "config.local.yaml"):
        config_path = root / name
        if config_path.exists():
            config = _deep_merge(config, _read_config_file(config_path))
    return config
