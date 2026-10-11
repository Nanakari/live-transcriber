"""Exercise the generated source-mode player and real Tk subtitle window.

python -m scripts.smoke_audio_player [--dummy-audio] [--interactive]
Default: a quiet synthetic tone on the default audio device, no microphone/API.
"""
from __future__ import annotations

import argparse
from array import array
import json
import math
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import time
import wave

from app.preview import PreviewOptions, create_audio_preview


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dummy-audio", action="store_true")
    parser.add_argument("--interactive", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    workspace = root / "build" / "player-smoke"
    workspace.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="audio-", dir=workspace))
    audio_dir = directory / "audio"
    audio_dir.mkdir()
    audio = audio_dir / "source.wav"
    rate, duration = 16000, 30
    samples = array("h", (int(800 * math.sin(2 * math.pi * (440 + (i // (10 * rate)) * 110) * i / rate))
                          for i in range(rate * duration)))
    with wave.open(str(audio), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(samples.tobytes())
    analysis = directory / "analysis"
    analysis.mkdir()
    sources = ["Audio playback test", "Pause and seek test", "Subtitle synchronization test"]
    translations = ["第一段：音频播放测试", "第二段：暂停与跳转测试", "第三段：字幕同步测试"]
    for name, texts in (("translation_zh.srt", translations), ("repaired_transcript.srt", sources)):
        (analysis / name).write_text("\n\n".join(
            f"{i+1}\n00:00:{i*10:02d},000 --> 00:00:{(i+1)*10:02d},000\n{text}"
            for i, text in enumerate(texts)
        ) + "\n", encoding="utf-8")
    (analysis / "analysis.json").write_text(json.dumps({
        "meta": {"transcript_run_id": "player-smoke"},
        "chunks": [{"bilingual_lines": [dict(segment_id=i+1, start=i*10, end=(i+1)*10,
                                             original=sources[i], translation_zh=translations[i]) for i in range(3)]}],
    }), encoding="utf-8")
    result = create_audio_preview(PreviewOptions(audio=audio, subtitle=analysis / "translation_zh.srt",
        cover=None, output_dir=audio_dir, resolution="1280x720", subtitle_name="live_preview.zh.srt",
        video_name="live_preview.mp4", mode="audio"))
    os.environ["PATH"] = str(Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "System32")
    for key in ("LIVE_TRANSCRIBER_FFPLAY", "LIVE_TRANSCRIBER_FFPROBE", "SDL_AUDIODRIVER"):
        os.environ.pop(key, None)
    if args.dummy_audio:
        os.environ["SDL_AUDIODRIVER"] = "dummy"
    player_module = runpy.run_path(str(result["audio_player"]))
    engine = player_module["find_ffplay"]()
    assert engine.parent == audio_dir / "assets", engine
    measured = player_module["probe_audio_duration"](audio, engine, 1)
    assert abs(measured - duration) < .1, measured
    # Check the actual device opened, not just that the ffplay executable exists.
    if not args.interactive:
        started = time.monotonic()
        check = subprocess.run([str(engine), "-hide_banner", "-loglevel", "info", "-nodisp", "-autoexit",
                                "-t", "1", str(audio)], capture_output=True, timeout=15,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        diagnostic = check.stderr.decode("utf-8", errors="replace")
        (directory / "audio-device.log").write_text(diagnostic, encoding="utf-8")
        assert check.returncode == 0 and time.monotonic() - started >= .8, diagnostic
        assert not any(error in diagnostic.lower() for error in ("audio open failed", "sdl_openaudiodevice", "failed to open")), diagnostic
    cues = player_module["load_cues"](result["audio_subtitle"])
    playback = player_module["AudioPlayback"](audio, engine, duration=measured, loop=args.interactive)
    overlay = player_module["FloatingSubtitleOverlay"](cues, audio_player=playback, title="Live Transcriber 播放器验证")
    if args.interactive:
        # Expose the same widgets to desktop inspection tools that omit
        # borderless/tool windows from their window inventory.
        overlay.root.overrideredirect(False)
        overlay.root.after(5000, playback.pause)
    failures, checks, processes = [], [], []

    def checkpoint(name, action):
        try:
            action()
            checks.append(name)
        except Exception as exc:
            failures.append(f"{name}: {exc!r}")
            overlay.close()

    def label_is(index):
        assert overlay.source_label.cget("text") == sources[index], repr(overlay.source_label.cget("text"))
        assert overlay.translation_label.cget("text") == translations[index], repr(overlay.translation_label.cget("text"))

    def playing():
        assert playback.process is not None and playback.process.poll() is None, f"ffplay exited at {playback.position():.2f}s"
        processes.append(playback.process)

    def pause():
        assert overlay.root.winfo_viewable(), "subtitle window is not visible"
        label_is(0)
        playing()
        overlay.play_button.invoke()
        assert not playback.playing and processes[-1].poll() is not None

    def seek():
        position = playback.position()
        assert 0.7 < position < 2, position
        assert overlay.root.attributes("-topmost")
        overlay._begin_progress_seek(None)
        overlay.progress_var.set(15)
        overlay._commit_progress_seek(None)
        assert playback.position() == 15

    def resume():
        label_is(1)
        overlay.play_button.invoke()
        assert playback.playing

    def skip():
        playing()
        overlay._seek_by(9)

    def finish():
        playing()
        label_is(2)
        overlay.close()
        assert all(process.poll() is not None for process in processes)

    if not args.interactive:
        for delay, name, action in ((1200, "play/pause", pause), (1600, "paused/seek", seek),
                                    (1900, "second subtitle/resume", resume), (2800, "seek forward", skip),
                                    (3300, "third subtitle/close", finish)):
            overlay.root.after(delay, lambda name=name, action=action: checkpoint(name, action))
        overlay.root.after(10000, lambda: checkpoint("timeout", lambda: (_ for _ in ()).throw(TimeoutError())))
    print(f"PLAYER_SMOKE_DIR {directory}", flush=True)
    try:
        overlay.run()
    finally:
        playback.close()
    report = {"status": "failed" if failures else ("interactive-closed" if args.interactive else "passed"),
              "audio_driver": "dummy" if args.dummy_audio else "default-device",
              "minimal_path": True, "duration": measured, "checks": checks, "failures": failures}
    (directory / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    if failures or (not args.interactive and len(checks) != 5):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
