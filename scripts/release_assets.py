"""Stage redistributable tools and dependency notices outside the source tree."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import shutil
import subprocess
from pathlib import Path

import imageio_ffmpeg
import requests

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "build" / "release-assets"


def main() -> None:
    tools_dir = STAGE / "tools"
    licenses = STAGE / "licenses"
    tools_dir.mkdir(parents=True, exist_ok=True)
    licenses.mkdir(parents=True, exist_ok=True)
    # Always use the pinned wheel's binary, not an arbitrary developer PATH binary.
    shutil.copy2(imageio_ffmpeg.get_ffmpeg_exe(), tools_dir / "ffmpeg.exe")
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Install Node.js 22 LTS to build the portable YouTube runtime.")
    version = subprocess.check_output([node, "--version"], text=True).strip()
    if not re.fullmatch(r"v\d+\.\d+\.\d+", version):
        raise RuntimeError("Unexpected Node.js version.")
    checksums = requests.get(f"https://nodejs.org/dist/{version}/SHASUMS256.txt", timeout=60)
    checksums.raise_for_status()
    expected = next(line.split()[0] for line in checksums.text.splitlines() if line.split()[-1] == "win-x64/node.exe")
    if hashlib.sha256(Path(node).read_bytes()).hexdigest() != expected:
        raise RuntimeError("Local Node.js does not match the official Windows x64 checksum.")
    shutil.copy2(node, tools_dir / "node.exe")
    response = requests.get(f"https://raw.githubusercontent.com/nodejs/node/{version}/LICENSE", timeout=60)
    response.raise_for_status()
    (licenses / "Node-LICENSE.txt").write_text(response.text, encoding="utf-8")
    for name in ("COPYING.GPLv3", "COPYING.LGPLv3"):
        response = requests.get(f"https://raw.githubusercontent.com/FFmpeg/FFmpeg/n7.1/{name}", timeout=60)
        response.raise_for_status()
        (licenses / f"FFmpeg-{name}.txt").write_text(response.text, encoding="utf-8")
    ffmpeg_version = subprocess.check_output([str(tools_dir / "ffmpeg.exe"), "-version"], text=True)
    (licenses / "FFmpeg-build.txt").write_text(ffmpeg_version, encoding="utf-8")
    packages = []
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name", "unknown")
        if name.lower().startswith("nvidia-"):
            continue  # CPU release does not ship the optional CUDA runtime.
        packages.append({"name": name, "version": distribution.version})
        for file in distribution.files or []:
            if not any(marker in file.name.lower() for marker in ("license", "copying", "notice")):
                continue
            source = Path(distribution.locate_file(file))
            if source.is_file() and source.stat().st_size < 2_000_000:
                package_dir = licenses / name
                package_dir.mkdir(exist_ok=True)
                target = package_dir / str(file).replace("/", "_").replace("\\", "_")
                shutil.copy2(source, target)
    (licenses / "packages.json").write_text(json.dumps(packages, indent=2), encoding="utf-8")
    (licenses / "tools.json").write_text(json.dumps({"node": version, "node_source": f"https://nodejs.org/dist/{version}/node-{version}.tar.gz",
        "ffmpeg_binary_source": "https://github.com/imageio/imageio-binaries/tree/master/ffmpeg",
        "ffmpeg_source": "https://ffmpeg.org/releases/ffmpeg-7.1.tar.xz"}, indent=2), encoding="utf-8")
    print(f"Prepared tools and notices: {STAGE}")


if __name__ == "__main__":
    main()
