from __future__ import annotations

import random
import shutil
import string
import subprocess
import os
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from .output_layout import safe_name


class AppError(RuntimeError):
    """User-facing error with a Chinese explanation."""


@dataclass
class OutputPaths:
    base_dir: Path
    audio_dir: Path
    transcripts_dir: Path
    logs_dir: Path
    run_id: str
    log_file: Path


class RunLogger:
    """Small UTF-8 file logger used before optional rich logging exists."""

    def __init__(self, log_file: Path, debug: bool = False, mirror_stdout: bool = True) -> None:
        self.log_file = log_file
        self.debug = debug
        self.mirror_stdout = mirror_stdout

    def write(self, message: str) -> None:
        timestamp = datetime.now().isoformat(timespec="seconds")
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {message}\n")
        if self.mirror_stdout:
            print(f"[detail] {message}", flush=True)


def ensure_dir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AppError(f"无法创建输出目录：{path}\n请检查路径是否存在权限问题。") from exc


def generate_run_id(now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    millis = f"{now.microsecond // 1000:03d}"
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return now.strftime(f"%Y%m%d_%H%M%S_{millis}_{suffix}")


def prepare_output_paths(output_dir: Path) -> OutputPaths:
    run_id = generate_run_id()
    staging_dir = output_dir / "media" / "_staging" / run_id
    audio_dir = staging_dir / "audio"
    transcripts_dir = staging_dir / "transcripts"
    logs_dir = staging_dir / "logs"
    for path in (audio_dir, transcripts_dir, logs_dir):
        ensure_dir(path)
    return OutputPaths(
        base_dir=output_dir,
        audio_dir=audio_dir,
        transcripts_dir=transcripts_dir,
        logs_dir=logs_dir,
        run_id=run_id,
        log_file=logs_dir / f"{run_id}.log",
    )


def format_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    ms = total_ms % 1000
    total_seconds = total_ms // 1000
    sec = total_seconds % 60
    total_minutes = total_seconds // 60
    minute = total_minutes % 60
    hour = total_minutes // 60
    return f"{hour:02d}:{minute:02d}:{sec:02d}.{ms:03d}"


def format_srt_timestamp(seconds: float) -> str:
    return format_timestamp(seconds).replace(".", ",")


def command_exists(command: str) -> bool:
    return shutil.which(command) is not None


def apply_runtime_path_entries(project_root: Path, entries: list[str], logger: Optional[RunLogger] = None) -> list[str]:
    """Prepend configured runtime directories, such as CUDA DLL folders, to PATH."""
    added: list[str] = []
    current_parts = os.environ.get("PATH", "").split(os.pathsep)
    normalized_current = {str(Path(part).resolve()).lower() for part in current_parts if part}
    for entry in entries:
        expanded = Path(os.path.expandvars(entry)).expanduser()
        path = expanded if expanded.is_absolute() else project_root / expanded
        if not path.exists():
            if logger:
                logger.write(f"Runtime PATH entry not found, skipped: {path}")
            continue
        resolved = str(path.resolve())
        if resolved.lower() in normalized_current:
            continue
        os.environ["PATH"] = resolved + os.pathsep + os.environ.get("PATH", "")
        normalized_current.add(resolved.lower())
        added.append(resolved)
        if logger:
            logger.write(f"Added runtime PATH entry: {resolved}")
    return added


def hidden_process_flags() -> int:
    if os.name == "nt":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def run_subprocess(
    command: list[str],
    logger: Optional[RunLogger] = None,
    *,
    stream_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    if logger:
        logger.write("运行命令：" + " ".join(command))
    if not stream_output:
        return subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            creationflags=hidden_process_flags(),
        )

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=hidden_process_flags(),
    )
    output_lines: list[str] = []
    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.rstrip("\r\n")
        output_lines.append(line)
        if logger and line:
            logger.write(line)
    returncode = process.wait()
    return subprocess.CompletedProcess(command, returncode, stdout="\n".join(output_lines), stderr="")


def copy_source_file(input_file: Path, audio_dir: Path, run_id: str) -> Path:
    if not input_file.exists():
        raise AppError(f"输入文件不存在：{input_file}")
    if not input_file.is_file():
        raise AppError(f"输入路径不是可读取文件：{input_file}")
    stem = safe_name(input_file.stem, max_len=96)
    output_stem = f"{stem}_{run_id[-4:]}" if stem else run_id
    target = audio_dir / f"{output_stem}_source{input_file.suffix or '.bin'}"
    try:
        shutil.copy2(input_file, target)
    except OSError as exc:
        raise AppError(f"无法复制输入文件到输出目录：{target}\n请检查文件权限。") from exc
    return target


def read_text_utf8(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise AppError(f"文本文件不存在：{path}") from exc
    except UnicodeDecodeError as exc:
        raise AppError(f"文本文件不是有效 UTF-8 编码：{path}") from exc


def is_cuda_available() -> bool:
    try:
        import ctranslate2  # type: ignore

        if getattr(ctranslate2, "get_cuda_device_count")() > 0:
            return True
    except Exception:
        pass
    return command_exists("nvidia-smi")


def normalize_device_compute(
    *,
    quality: str,
    requested_device: str,
    requested_compute_type: Optional[str],
    default_compute_type: str,
    logger: Optional[RunLogger] = None,
) -> tuple[str, str, list[str]]:
    warnings: list[str] = []
    device = requested_device
    compute_type = requested_compute_type or default_compute_type

    if device == "auto":
        if quality == "cpu_safe":
            device = "cpu"
            compute_type = requested_compute_type or "int8"
            warnings.append("cpu_safe 模式按配置使用 CPU int8。")
        elif is_cuda_available():
            device = "cuda"
        else:
            raise AppError(
                "CUDA 当前不可用，已停止任务，不会自动改用 CPU。\n"
                "请检查 NVIDIA 驱动、CUDA DLL 路径和 nvidia-cublas-cu12/nvidia-cudnn-cu12 依赖。\n"
                "如果你明确想用 CPU，请使用 --device cpu 或 --quality cpu_safe。"
            )

    if device == "cpu":
        if compute_type == "int8_float16":
            compute_type = "int8"
            warnings.append("已将 int8_float16 自动切换为 CPU 兼容的 int8。")
        elif requested_compute_type is None:
            compute_type = "int8"

    if quality == "high" and device == "cuda":
        warnings.append("高质量模式将使用 GPU；建议关闭其他占用显存的程序。")

    for warning in warnings:
        if logger:
            logger.write("警告：" + warning)
    return device, compute_type, warnings


def build_initial_prompt(
    *,
    language: str,
    project_root: Path,
    default_terms_path: Path,
    explicit_terms_file: Optional[str],
    explicit_initial_prompt: Optional[str],
    logger: RunLogger,
) -> tuple[str, list[str]]:
    warnings: list[str] = []
    parts: list[str] = []
    if explicit_initial_prompt:
        parts.append(explicit_initial_prompt.strip())

    should_load_default_terms = language == "ja" and not explicit_terms_file
    terms_path: Optional[Path] = None
    if explicit_terms_file:
        terms_path = Path(explicit_terms_file).expanduser()
    elif should_load_default_terms:
        terms_path = default_terms_path if default_terms_path.is_absolute() else project_root / default_terms_path

    if terms_path:
        if not terms_path.exists():
            if explicit_terms_file:
                raise AppError(f"指定的术语文件不存在：{terms_path}")
            warning = f"默认日语术语文件不存在，已跳过：{terms_path}"
            warnings.append(warning)
            logger.write("警告：" + warning)
        else:
            terms = [line.strip() for line in read_text_utf8(terms_path).splitlines() if line.strip()]
            if terms:
                joined_terms = "、".join(terms)
                parts.append(
                    "以下は日本語のVTuber配信・雑談・歌枠の音声です。"
                    "次の固有名詞や用語が含まれる可能性があります：\n"
                    + joined_terms
                )

    prompt = "\n".join(part for part in parts if part)
    if len(prompt) > 600:
        warning = "initial_prompt 可能过长，后半部分可能被 Whisper 截断。建议控制在约 200 个 Whisper tokens 内。"
        warnings.append(warning)
        logger.write("警告：" + warning)
    return prompt, warnings


def local_created_at(timezone_name: str) -> str:
    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = timezone(timedelta(hours=9), name="Asia/Tokyo")
    return datetime.now(tz).isoformat(timespec="seconds")
