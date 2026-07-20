from __future__ import annotations

import shutil
from pathlib import Path

from .config import project_root
from .utils import AppError, RunLogger, run_subprocess


def ffmpeg_command() -> str | None:
    bundled = project_root() / "tools" / "ffmpeg.exe"
    if bundled.exists():
        return str(bundled.resolve())
    return shutil.which("ffmpeg")


def convert_to_clean_wav(source_file: Path, output_file: Path, logger: RunLogger) -> Path:
    """Convert source audio/video to mono 16 kHz PCM wav for ASR."""
    ffmpeg = ffmpeg_command()
    if not ffmpeg:
        raise AppError(
            "未找到 ffmpeg。\n"
            "请确认项目 tools/ffmpeg.exe 存在，或把 ffmpeg.exe 所在目录加入 PATH。\n"
            "Windows 可从 https://ffmpeg.org/download.html 或 winget/chocolatey 安装。"
        )
    if not source_file.exists():
        raise AppError(f"源音频文件不存在：{source_file}")

    command = [
        ffmpeg,
        "-y",
        "-i",
        str(source_file),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-vn",
        "-c:a",
        "pcm_s16le",
        "-progress",
        "pipe:1",
        "-nostats",
        str(output_file),
    ]
    result = run_subprocess(command, logger, stream_output=True)
    if result.returncode != 0:
        raise AppError(
            "ffmpeg 转换音频失败。\n"
            "请确认输入文件可播放、不是空文件，并确认 ffmpeg 支持该格式。\n"
            f"ffmpeg 输出：{(result.stderr or result.stdout).strip()}"
        )
    if not output_file.exists() or output_file.stat().st_size == 0:
        raise AppError("ffmpeg 已运行，但输出 wav 文件为空。请检查源音频是否有效。")
    return output_file
