"""Verify a folder release without developer PATH, Python, credentials or models."""
from __future__ import annotations
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen


def main() -> None:
    release = Path(sys.argv[1]).resolve()
    executable = release / "LiveTranscriber.exe"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with tempfile.TemporaryDirectory(prefix="transcriber-release-") as directory:
        env = {key: value for key, value in os.environ.items()
               if key.upper() not in {"PYTHONPATH", "PYTHONHOME", "GEMINI_API_KEY", "HF_HOME", "FASTER_WHISPER_MODEL_DIR", "CUDA_PATH"}}
        env.update(PATH=str(Path(os.environ["SystemRoot"]) / "System32"), LIVE_TRANSCRIBER_HOME=directory,
                   CUDA_VISIBLE_DEVICES="", HF_HUB_OFFLINE="1")
        for arguments in ([str(executable), "--help"], [str(executable), "_yt-dlp", "--version"],
                          [str(release / "_internal" / "tools" / "ffmpeg.exe"), "-version"],
                          [str(release / "_internal" / "tools" / "node.exe"), "--version"]):
            subprocess.run(arguments, env=env, cwd=directory, check=True, capture_output=True, timeout=60, creationflags=flags)
        media = Path(directory) / "字幕 sample's"
        media.mkdir()
        ffmpeg = str(release / "_internal" / "tools" / "ffmpeg.exe")
        for arguments in (
            [ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x180", "-frames:v", "1", str(media / "cover.png")],
            [ffmpeg, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(media / "audio.wav")],
        ):
            subprocess.run(arguments, env=env, cwd=directory, check=True, capture_output=True, timeout=60, creationflags=flags)
        (media / "translation.srt").write_text("1\n00:00:00,000 --> 00:00:00,900\n字幕测试\n", encoding="utf-8")
        video_dir = media / "video"
        result = subprocess.run(
            [str(executable), "preview", "--audio", str(media / "audio.wav"), "--subtitle", str(media / "translation.srt"),
             "--cover", str(media / "cover.png"), "--output-dir", str(video_dir), "--resolution", "320x180"],
            env=env, cwd=directory, capture_output=True, timeout=60, creationflags=flags,
        )
        assert result.returncode == 0, result.stdout.decode("utf-8", errors="replace") + result.stderr.decode("utf-8", errors="replace")
        assert (video_dir / "live_preview.mp4").stat().st_size > 0
        assert (video_dir / "subtitles" / "display.bilingual.ass").exists()
        assert (video_dir / "assets" / "manifest.json").exists()
        # Exercise the default audio workflow and its bundled GUI/audio runtime.
        audio_dir = media / "audio-player"
        subprocess.run(
            [str(executable), "preview", "--mode", "audio", "--audio", str(media / "audio.wav"),
             "--subtitle", str(media / "translation.srt"), "--output-dir", str(audio_dir)],
            env=env, cwd=directory, check=True, capture_output=True, timeout=60, creationflags=flags,
        )
        launcher = (audio_dir / "run_audio_subtitle_player.cmd").read_text(encoding="utf-8")
        assert "LiveTranscriberPlayer.exe" in launcher and "pythonw" not in launcher
        player_env = dict(env, SDL_AUDIODRIVER="dummy")
        subprocess.run(
            [str(release / "LiveTranscriberPlayer.exe"), "--srt", str(audio_dir / "audio_mode.bilingual.srt"),
             "--audio", str(media / "audio.wav"), "--check-runtime"],
            env=player_env, cwd=directory, check=True, timeout=60, creationflags=flags,
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        with (Path(directory) / "server.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen([str(executable), "web", "--port", str(port), "--no-browser"],
                                       cwd=directory, env=env, stdout=log, stderr=log, creationflags=flags)
            try:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError((Path(directory) / "server.log").read_text(encoding="utf-8"))
                    try:
                        with urlopen(f"http://127.0.0.1:{port}/api/status", timeout=5) as response:
                            status = json.load(response)
                        break
                    except OSError:
                        time.sleep(0.5)
                else:
                    raise RuntimeError("Packaged server did not become ready.")
                assert status["ffmpeg"] and status["yt_dlp"] and status["faster_whisper"], status
                assert not status["gemini_api_key_detected"], "Release picked up a developer credential"
                assert status["defaults"]["proxy"] == ""
                assert status["defaults"]["quality"] == "high"
                for path, marker in (("/", "workbench.js"), ("/?view=classic", "classic.js"), ("/static/classic.js", "renderQuickAction"), ("/static/classic.css", ".shell"), ("/static/workbench.js", "startTask"), ("/static/workbench.css", ".shell")):
                    with urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as response:
                        if path.endswith(".js"):
                            assert response.headers.get_content_type() == "application/javascript"
                        assert marker in response.read().decode("utf-8")
            finally:
                process.terminate()
                process.wait(timeout=15)
    print("Release smoke checks passed with isolated data and a minimal PATH.")


if __name__ == "__main__":
    main()
