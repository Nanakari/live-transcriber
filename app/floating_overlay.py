from __future__ import annotations

import json
import os
import sys
from pathlib import Path


OVERLAY_FILENAME = "floating_subtitle_overlay.pyw"
AUDIO_PLAYER_FILENAME = "audio_subtitle_player.pyw"
OVERLAY_LAUNCHER_FILENAME = "run_floating_subtitle_overlay.cmd"
AUDIO_PLAYER_LAUNCHER_FILENAME = "run_audio_subtitle_player.cmd"
AUDIO_MODE_SUBTITLE_FILENAME = "audio_mode.bilingual.srt"
_PLACEHOLDER = "__DEFAULT_SUBTITLE_RELATIVE__"
_AUDIO_PLACEHOLDER = "__DEFAULT_AUDIO_RELATIVE__"
_TEMPLATE_PATH = Path(__file__).with_name("assets") / OVERLAY_FILENAME


def _relative_or_absolute(source: Path, target_parent: Path) -> str:
    source = source.expanduser().resolve()
    try:
        return source.relative_to(target_parent.resolve()).as_posix()
    except ValueError:
        return str(source)


def _write_overlay_script(target: Path, subtitle: Path, audio: Path | None = None) -> Path:
    target = target.expanduser()
    subtitle = subtitle.expanduser().resolve()
    if not subtitle.exists() or not subtitle.is_file():
        raise FileNotFoundError(f"悬挂字幕框的字幕文件不存在：{subtitle}")
    if not _TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"悬挂字幕框模板不存在：{_TEMPLATE_PATH}")

    target.parent.mkdir(parents=True, exist_ok=True)
    content = _TEMPLATE_PATH.read_text(encoding="utf-8")
    content = content.replace(
        json.dumps(_PLACEHOLDER),
        json.dumps(_relative_or_absolute(subtitle, target.parent), ensure_ascii=False),
    ).replace(
        json.dumps(_AUDIO_PLACEHOLDER),
        json.dumps(
            _relative_or_absolute(audio, target.parent) if audio is not None else "",
            ensure_ascii=False,
        ),
    )
    target.write_text(content, encoding="utf-8", newline="\n")
    return target


def write_floating_subtitle_overlay(target: Path, subtitle: Path) -> Path:
    """Write a self-contained compact Tkinter overlay beside a preview."""
    return _write_overlay_script(target, subtitle)


def write_audio_subtitle_player(target: Path, audio: Path, subtitle: Path) -> Path:
    """Write a self-contained audio player with the same compact overlay style."""
    audio = audio.expanduser().resolve()
    if not audio.exists() or not audio.is_file():
        raise FileNotFoundError(f"音频模式的音频文件不存在：{audio}")
    return _write_overlay_script(target, subtitle, audio)


def write_overlay_launcher(target: Path, script_name: str, *, subtitle: Path | None = None, audio: Path | None = None) -> Path:
    """Write a Windows launcher so the generated player is clickable without .pyw association."""
    target = target.expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    if getattr(sys, "frozen", False):
        if subtitle is None:
            raise ValueError("Packaged player requires a subtitle path")
        player = Path(sys.executable).with_name("LiveTranscriberPlayer.exe")
        def argument(path: Path) -> str:
            return '"' + str(path.resolve()).replace("%", "%%") + '"'
        arguments = f"--srt {argument(subtitle)}"
        if audio is not None:
            arguments += f" --audio {argument(audio)}"
        else:
            arguments += " --no-audio"
        content = f'@echo off\nchcp 65001 >nul\nsetlocal\nstart "" {argument(player)} {arguments} %*\nexit /b 0\n'
        target.write_text(content, encoding="utf-8", newline="\r\n")
        return target
    project_pythonw = Path(__file__).resolve().parents[1] / ".venv" / "Scripts" / "pythonw.exe"
    pythonw_prefix = "%~dp0"
    try:
        pythonw_reference = os.path.relpath(project_pythonw, target.parent.resolve()).replace("/", "\\")
    except ValueError:
        # Test/output folders can be on another Windows drive; use an absolute
        # project interpreter path in that case.  Absolute Windows paths must
        # not be prefixed with %~dp0, which would turn D:\\... into
        # %~dp0D:\\... and make the interpreter path invalid.
        pythonw_reference = str(project_pythonw)
        pythonw_prefix = ""
    content = f"""@echo off
setlocal
set "SCRIPT=%~dp0{script_name}"
set "PYTHONW={pythonw_prefix}{pythonw_reference}"
if not exist "%PYTHONW%" set "PYTHONW=pythonw.exe"
if not exist "%SCRIPT%" exit /b 2
start "" "%PYTHONW%" "%SCRIPT%" %*
exit /b 0
"""
    target.write_text(content, encoding="utf-8", newline="\r\n")
    return target
