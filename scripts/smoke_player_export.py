"""Build/export, relocate, and exercise the complete Windows player ZIP.

Run from the project root: python -m scripts.smoke_player_export
Uses a short silent WAV and SDL's dummy audio device; no API or models needed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import wave
import zipfile

from app.player_package import build_player_archive
from app.utils import hidden_process_flags


def main() -> None:
    temporary_base = Path(tempfile.gettempdir()).resolve()
    with tempfile.TemporaryDirectory(prefix="transcriber-player-smoke-", dir=temporary_base) as directory:
        root = Path(directory).resolve()
        assert root.parent == temporary_base
        audio = root / "source.wav"
        with wave.open(str(audio), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16000)
            writer.writeframes(b"\0\0" * 4000)
        subtitle = root / "subtitles.srt"
        subtitle.write_text("1\n00:00:00,000 --> 00:00:00,250\nSmoke test\n", encoding="utf-8")
        archive = root / "player.zip"
        print("Building portable player ZIP...", flush=True)
        build_player_archive(audio, subtitle, archive)
        relocated = root / "字幕 player's relocated folder"
        with zipfile.ZipFile(archive) as package:
            package.extractall(relocated)
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() not in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "TCL_LIBRARY", "TK_LIBRARY", "TCLLIBPATH"}}
        environment.update(PATH=str(Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "System32"),
                           SDL_AUDIODRIVER="dummy")
        command = [str(relocated / "runtime/LiveTranscriberPlayer.exe"), "--check-runtime",
                   "--srt", str(relocated / "subtitles.srt"), "--audio", str(relocated / "media/source.wav"),
                   "--ffplay", str(relocated / "assets/ffplay.exe")]
        result = subprocess.run(command, cwd=relocated, env=environment,
                                creationflags=hidden_process_flags(), capture_output=True, timeout=45)
        if result.returncode:
            diagnostic = relocated / "player_runtime_error.log"
            detail = diagnostic.read_text(encoding="utf-8") if diagnostic.exists() else result.stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"Player exit {result.returncode}: {detail}")
        print(json.dumps({"status": "passed", "archive_bytes": archive.stat().st_size,
                          "minimal_path": True, "relocated": True, "audio_driver": "dummy"}))


if __name__ == "__main__":
    main()
