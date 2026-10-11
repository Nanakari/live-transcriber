"""Pinned Windows FFmpeg tools shared by source setup and release builds."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import zipfile

import requests

FFMPEG_VERSION = "8.1.2"
FFMPEG_URL = f"https://github.com/GyanD/codexffmpeg/releases/download/{FFMPEG_VERSION}/ffmpeg-{FFMPEG_VERSION}-essentials_build.zip"
FFMPEG_SHA256 = "db580001caa24ac104c8cb856cd113a87b0a443f7bdf47d8c12b1d740584a2ec"
TOOL_NAMES = ("ffmpeg.exe", "ffplay.exe", "ffprobe.exe")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verified_archive(archive: Path) -> Path:
    if archive.is_file() and sha256(archive) == FFMPEG_SHA256:
        return archive
    archive.parent.mkdir(parents=True, exist_ok=True)
    # Interrupted downloads never become a valid cache entry.
    with tempfile.TemporaryDirectory(prefix="ffmpeg-download-", dir=archive.parent) as temporary:
        downloaded = Path(temporary) / "ffmpeg.zip"
        print(f"Downloading verified FFmpeg tools: {FFMPEG_URL}", flush=True)
        with requests.get(FFMPEG_URL, stream=True, timeout=(15, 120)) as response:
            response.raise_for_status()
            with downloaded.open("wb") as target:
                for block in response.iter_content(1024 * 1024):
                    target.write(block)
        if sha256(downloaded) != FFMPEG_SHA256:
            raise RuntimeError("FFmpeg archive checksum mismatch; no tools were installed.")
        downloaded.replace(archive)
    return archive


def install_media_tools(tools_dir: Path, archive: Path, *, licenses: Path | None = None,
                        overwrite: bool = False) -> None:
    missing = [name for name in TOOL_NAMES if overwrite or not (tools_dir / name).is_file()
               or (tools_dir / name).stat().st_size == 0]
    if not missing:
        return
    archive = verified_archive(archive)
    tools_dir.mkdir(parents=True, exist_ok=True)
    licenses = licenses or tools_dir / "licenses"
    licenses.mkdir(parents=True, exist_ok=True)
    # Validate/extract the entire requested set before publishing any executable.
    with zipfile.ZipFile(archive) as bundle, tempfile.TemporaryDirectory(prefix="ffmpeg-install-", dir=tools_dir) as temporary:
        stage = Path(temporary)
        for name in missing:
            matches = [item for item in bundle.infolist() if item.filename.endswith("/bin/" + name)]
            if len(matches) != 1 or matches[0].file_size == 0:
                raise RuntimeError(f"FFmpeg archive is missing a unique, nonempty {name}")
            with bundle.open(matches[0]) as source, (stage / name).open("wb") as target:
                shutil.copyfileobj(source, target)
        for member in bundle.namelist():
            if member.endswith("/LICENSE") or member.endswith("/README.txt"):
                (licenses / ("FFmpeg-" + Path(member).name)).write_bytes(bundle.read(member))
        for name in missing:
            (stage / name).replace(tools_dir / name)
    (licenses / "FFmpeg-tools.json").write_text(json.dumps({
        "version": FFMPEG_VERSION, "binary_source": FFMPEG_URL, "sha256": FFMPEG_SHA256,
        "source": f"https://ffmpeg.org/releases/ffmpeg-{FFMPEG_VERSION}.tar.xz",
        "installed_tools": missing,
    }, indent=2), encoding="utf-8")
