from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import os
import sys
from typing import Any

import yaml

from .utils import AppError



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
    if name in {"ffplay", "ffprobe"}:
        override = os.environ.get(f"LIVE_TRANSCRIBER_{name.upper()}", "").strip()
        if override and (candidate := Path(override).expanduser()).is_file():
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


# __file__ points into the bundle in PyInstaller, too. Keep defaults beside code
# so source and portable builds load exactly the same configuration resource.
DEFAULT_CONFIG: dict[str, Any] = _read_config_file(Path(__file__).with_name("defaults.yaml"))


def load_config(root: Path | None = None) -> dict[str, Any]:
    root = root or project_root()
    config = deepcopy(DEFAULT_CONFIG)
    for name in ("config.yaml", "config.local.yaml"):
        config_path = root / name
        if config_path.exists():
            config = _deep_merge(config, _read_config_file(config_path))
    return config


def output_root(override: str | Path | None = None, *, root: Path | None = None) -> Path:
    """Resolve every result/cache/log path against the same configured base."""
    root = (root or project_root()).resolve()
    configured = override if override is not None else load_config(root)["app"]["output_dir"]
    path = Path(configured).expanduser()
    return (path if path.is_absolute() else root / path).resolve()
