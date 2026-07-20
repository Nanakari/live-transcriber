from __future__ import annotations

import os
import subprocess
import shutil
from pathlib import Path
import winreg

from fastapi import HTTPException

from ..config import project_root
from ..utils import hidden_process_flags


TEXT_EXTENSIONS = {".txt", ".md", ".json", ".srt", ".log", ".yaml", ".yml"}
OPENABLE_EXTENSIONS = TEXT_EXTENSIONS | {".mp4", ".m4a", ".wav", ".mp3", ".jpg", ".jpeg", ".png", ".webp"}
MAX_PREVIEW_BYTES = 256 * 1024


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_allowed_path(path_text: str, *, must_be_text: bool = False) -> Path:
    if not path_text:
        raise HTTPException(status_code=400, detail="path is required")

    root = project_root().resolve()
    target = Path(path_text).expanduser()
    if not target.is_absolute():
        target = root / target
    target = target.resolve()

    if not _is_relative_to(target, root):
        raise HTTPException(status_code=403, detail="path is outside the project directory")
    if must_be_text and target.suffix.lower() not in TEXT_EXTENSIONS:
        raise HTTPException(status_code=403, detail="file type is not allowed for preview")
    return target


def read_text_preview(path_text: str) -> dict[str, object]:
    target = resolve_allowed_path(path_text, must_be_text=True)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")

    size = target.stat().st_size
    truncated = size > MAX_PREVIEW_BYTES
    if truncated:
        with target.open("rb") as f:
            f.seek(max(0, size - MAX_PREVIEW_BYTES))
            raw = f.read(MAX_PREVIEW_BYTES)
    else:
        raw = target.read_bytes()

    return {
        "path": str(target),
        "size": size,
        "truncated": truncated,
        "content": raw.decode("utf-8", errors="replace"),
    }


def open_folder(path_text: str) -> dict[str, str]:
    target = resolve_allowed_path(path_text)
    folder = target if target.is_dir() else target.parent
    if not folder.exists():
        raise HTTPException(status_code=404, detail="folder not found")
    if os.name != "nt":
        raise HTTPException(status_code=400, detail="open folder is only supported on Windows")
    subprocess.Popen(["explorer", str(folder)])
    return {"path": str(folder)}


def open_file(path_text: str) -> dict[str, str]:
    target = resolve_allowed_path(path_text)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    if target.suffix.lower() not in OPENABLE_EXTENSIONS:
        raise HTTPException(status_code=403, detail="file type is not allowed to open")
    if os.name != "nt":
        raise HTTPException(status_code=400, detail="open file is only supported on Windows")
    os.startfile(str(target))  # type: ignore[attr-defined]
    return {"path": str(target)}


def browse_file(kind: str) -> dict[str, str]:
    if os.name != "nt":
        raise HTTPException(status_code=400, detail="file picker is only supported on Windows")

    filters = {
        "media": "媒体文件|*.mp4;*.mkv;*.webm;*.m4a;*.mp3;*.wav;*.opus|所有文件|*.*",
        "audio": "音频文件|*.m4a;*.mp3;*.wav;*.opus;*.webm|所有文件|*.*",
        "transcript": "Transcript JSON|*_transcript.json;*.json|所有文件|*.*",
        "subtitle": "字幕文件|*.srt|所有文件|*.*",
        "cover": "图片文件|*.jpg;*.jpeg;*.png;*.webp|所有文件|*.*",
        "cookies": "cookies.txt|*.txt|所有文件|*.*",
    }
    selected_filter = filters.get(kind, "所有文件|*.*").replace("'", "''")
    script = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "Add-Type -AssemblyName System.Windows.Forms; "
        "[System.Windows.Forms.Application]::EnableVisualStyles(); "
        "$owner = New-Object System.Windows.Forms.Form; "
        "$owner.TopMost = $true; $owner.ShowInTaskbar = $false; "
        "$owner.StartPosition = 'CenterScreen'; $owner.Size = New-Object System.Drawing.Size(1,1); "
        "$owner.Opacity = 0; $owner.Show(); "
        "$dialog = New-Object System.Windows.Forms.OpenFileDialog; "
        "$dialog.Title = '选择文件'; "
        f"$dialog.Filter = '{selected_filter}'; "
        "$dialog.CheckFileExists = $true; $dialog.Multiselect = $false; "
        "$result = $dialog.ShowDialog($owner); "
        "if ($result -eq [System.Windows.Forms.DialogResult]::OK) { "
        "[Console]::Out.Write($dialog.FileName) }; "
        "$dialog.Dispose(); $owner.Close(); $owner.Dispose();"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-STA", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=hidden_process_flags(),
        )
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"无法启动 Windows 文件选择器：{exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "Windows 文件选择器返回错误。"
        raise HTTPException(status_code=500, detail=f"无法打开文件选择器：{detail[:500]}")
    return {"path": completed.stdout.lstrip("\ufeff").strip()}


def open_potplayer(video_text: str, subtitle_text: str | None = None) -> dict[str, str]:
    video = resolve_allowed_path(video_text)
    if not video.exists() or not video.is_file():
        raise HTTPException(status_code=404, detail="video file not found")
    if video.suffix.lower() != ".mp4":
        raise HTTPException(status_code=403, detail="only mp4 preview files can be opened with PotPlayer")

    subtitle: Path | None = None
    if subtitle_text:
        subtitle = resolve_allowed_path(subtitle_text)
        if not subtitle.exists() or not subtitle.is_file():
            raise HTTPException(status_code=404, detail="subtitle file not found")
        if subtitle.suffix.lower() != ".srt":
            raise HTTPException(status_code=403, detail="only srt subtitles can be loaded")

    potplayer = find_potplayer()
    if not potplayer:
        raise HTTPException(
            status_code=404,
            detail="未找到 PotPlayer。请确认已安装 PotPlayer，或把 PotPlayerMini64.exe 加入 PATH。",
        )

    command = [str(potplayer), str(video)]
    if subtitle:
        command.append(f'/sub="{subtitle}"')
    subprocess.Popen(command)
    return {"player": str(potplayer), "video": str(video), "subtitle": str(subtitle or "")}


def find_potplayer() -> Path | None:
    for name in ("PotPlayerMini64.exe", "PotPlayerMini.exe"):
        found = shutil.which(name)
        if found:
            return Path(found)

    for path in _potplayer_paths_from_registry():
        if path.exists():
            return path

    candidates = [
        Path("C:/Program Files/DAUM/PotPlayer/PotPlayerMini64.exe"),
        Path("C:/Program Files/DAUM/PotPlayer/PotPlayerMini.exe"),
        Path("C:/Program Files (x86)/DAUM/PotPlayer/PotPlayerMini.exe"),
        Path("C:/Program Files/PotPlayer/PotPlayerMini64.exe"),
        Path("D:/PotPlayer/PotPlayerMini64.exe"),
        Path("D:/PotPlayer/PotPlayerMini.exe"),
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def _potplayer_paths_from_registry() -> list[Path]:
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    paths: list[Path] = []
    for root, subkey in roots:
        try:
            with winreg.OpenKey(root, subkey) as key:
                for index in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        child_name = winreg.EnumKey(key, index)
                        with winreg.OpenKey(key, child_name) as child:
                            display_name = str(_read_reg_value(child, "DisplayName") or "")
                            if "PotPlayer" not in display_name:
                                continue
                            icon = _read_reg_value(child, "DisplayIcon")
                            install = _read_reg_value(child, "InstallLocation")
                            if icon:
                                paths.append(Path(str(icon).strip('"').split(",", 1)[0]))
                            if install:
                                base = Path(str(install).strip('"'))
                                paths.extend([base / "PotPlayerMini64.exe", base / "PotPlayerMini.exe"])
                    except OSError:
                        continue
        except OSError:
            continue
    return paths


def _read_reg_value(key: object, name: str) -> str | None:
    try:
        value, _ = winreg.QueryValueEx(key, name)  # type: ignore[arg-type]
        return str(value)
    except OSError:
        return None

