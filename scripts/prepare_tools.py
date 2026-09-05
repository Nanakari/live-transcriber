"""Install the ffmpeg supplied by the pinned wheel; never overwrite a local tool."""
from pathlib import Path
import shutil
import imageio_ffmpeg


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    target = root / "tools" / "ffmpeg.exe"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        shutil.copy2(imageio_ffmpeg.get_ffmpeg_exe(), target)
    print(f"ffmpeg: {target}")


if __name__ == "__main__":
    main()
