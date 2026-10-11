from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

from .config import output_root, resource_root, tool_path
from .utils import AppError, hidden_process_flags


_runtime_lock = threading.Lock()
_RUNTIME_BUILD_VERSION = b"2-tcl-runtime"


def _player_runtime() -> tuple[Path, Path]:
    """Reuse the shipped runtime, or build/cache a standalone source-mode player."""
    if sys.platform != "win32":
        raise AppError("完整 Windows 播放器包请在 Windows 上导出。")
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).with_name("LiveTranscriberPlayer.exe")
        internal = resource_root()
        if not executable.is_file() or not internal.is_dir():
            raise AppError("播放器运行组件不完整，请重新解压完整发行包。")
        return executable, internal

    source = resource_root() / "app" / "assets" / "floating_subtitle_overlay.pyw"
    key = hashlib.sha256(_RUNTIME_BUILD_VERSION + source.read_bytes() + sys.version.encode() + sys.executable.encode()).hexdigest()[:20]
    cache_root = (output_root() / "cache" / "player-runtime").resolve()
    cached = cache_root / key
    executable = cached / "LiveTranscriberPlayer.exe"
    with _runtime_lock:
        if executable.is_file() and (cached / "_internal").is_dir() and (cached / ".ready").is_file():
            return executable, cached / "_internal"
        if importlib.util.find_spec("PyInstaller") is None:
            raise AppError("源码导出完整播放器包需要 PyInstaller，请先安装 requirements-dev.txt；发行包可直接导出。")
        cache_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="build-", dir=cache_root) as temporary:
            stage = Path(temporary).resolve()
            assert stage.parent == cache_root
            command = [
                sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--noupx",
                "--onedir", "--windowed", "--name", "LiveTranscriberPlayer",
                "--distpath", str(stage / "dist"), "--workpath", str(stage / "build"),
                "--specpath", str(stage),
            ]
            # Conda's stdlib extensions can depend on DLLs outside DLLs/.
            for name in ("expat.dll", "libexpat.dll", "libssl-3-x64.dll", "libcrypto-3-x64.dll",
                         "ffi.dll", "sqlite3.dll", "libbz2.dll", "liblzma.dll", "zlib.dll",
                         "tcl86t.dll", "tk86t.dll"):
                dependency = Path(sys.base_prefix) / "Library" / "bin" / name
                if dependency.is_file():
                    command.extend(["--add-binary", f"{dependency}{os.pathsep}."])
            command.append(str(source))
            log_path = cache_root / f"{key}.log"
            try:
                with log_path.open("w", encoding="utf-8") as log:
                    result = subprocess.run(command, cwd=stage, stdout=log, stderr=subprocess.STDOUT,
                                            creationflags=hidden_process_flags(), timeout=600)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise AppError(f"播放器构建失败，请查看 {log_path}：{exc}") from exc
            built = stage / "dist" / "LiveTranscriberPlayer"
            if result.returncode or not (built / "LiveTranscriberPlayer.exe").is_file():
                raise AppError(f"播放器构建失败，请查看 {log_path}")
            shutil.copytree(built, cached, dirs_exist_ok=True)
            (cached / ".ready").write_text(key, encoding="ascii")
    return executable, cached / "_internal"


def build_player_archive(audio: Path, subtitle: Path, destination: Path) -> None:
    """Write a relocatable ZIP to disk without buffering media in server memory."""
    if not audio.is_file() or not subtitle.is_file():
        raise AppError("播放器所需的音频或字幕不存在，请重新生成播放器。")
    runtime_tools = {}
    for name in ("ffplay", "ffprobe"):
        found = tool_path(name) or shutil.which(name)
        if not found or not Path(found).is_file():
            raise AppError(f"缺少 {name}，无法导出完整播放器包。请重新准备工具或使用完整发行包。")
        runtime_tools[name] = Path(found)
    executable, internal = _player_runtime()
    extension = audio.suffix.lower()
    media_name = "media/source" + (extension if re.fullmatch(r"\.[a-z0-9]{1,8}", extension) else ".media")
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        archive.write(audio, media_name)
        archive.write(subtitle, "subtitles.srt")
        archive.write(executable, "runtime/LiveTranscriberPlayer.exe")
        for path in sorted(internal.rglob("*")):
            if path.is_file():
                archive.write(path, "runtime/_internal/" + path.relative_to(internal).as_posix())
        for name, path in runtime_tools.items():
            archive.write(path, f"assets/{name}.exe")
        for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
            path = resource_root() / name
            if path.is_file():
                archive.write(path, name)
        licenses = resource_root() / "licenses"
        if not licenses.is_dir() and not getattr(sys, "frozen", False):
            licenses = resource_root() / "build" / "release-assets" / "licenses"
        if licenses.is_dir():
            for path in licenses.rglob("*"):
                if path.is_file():
                    archive.write(path, "licenses/" + path.relative_to(licenses).as_posix())
        media_argument = media_name.replace("/", "\\")
        archive.writestr("run_audio_subtitle_player.cmd", (
            '@echo off\r\nsetlocal\r\n'
            'start "" "%~dp0runtime\\LiveTranscriberPlayer.exe" '
            '--srt "%~dp0subtitles.srt" '
            f'--audio "%~dp0{media_argument}" '
            '--ffplay "%~dp0assets\\ffplay.exe" %*\r\nexit /b 0\r\n'
        ))
        archive.writestr("README_play.txt", "完整解压后双击 run_audio_subtitle_player.cmd。\n"
                          "请保留 runtime、assets、media 和 subtitles.srt；无需另装 Python。\n"
                          "可将整个文件夹移动到其他 Windows 电脑，不能只复制启动器。\n")
