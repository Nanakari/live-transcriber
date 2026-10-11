"""Install all Windows media tools without overwriting existing local binaries."""
from pathlib import Path
import os
import subprocess

if __package__:
    from .media_tools import TOOL_NAMES, install_media_tools
else:
    from media_tools import TOOL_NAMES, install_media_tools


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    if os.name != "nt":
        raise SystemExit("On Linux/macOS install ffmpeg, ffplay and ffprobe through your package manager.")
    tools_dir = root / "tools"
    install_media_tools(tools_dir, root / "build" / "release-assets" / "ffmpeg.zip")
    for name in TOOL_NAMES:
        target = tools_dir / name
        result = subprocess.run([str(target), "-version"], check=True, capture_output=True,
                                timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        print(f"{target}: {result.stdout.decode('utf-8', errors='replace').splitlines()[0]}")


if __name__ == "__main__":
    main()
