from pathlib import Path
from types import SimpleNamespace
import shutil
import subprocess

import pytest

from app import preview
from app.utils import RunLogger


def test_preview_prefers_bilingual_subtitle_for_burn_in(monkeypatch, tmp_path: Path) -> None:
    audio = tmp_path / "audio.m4a"
    subtitle = tmp_path / "translation_zh.srt"
    cover = tmp_path / "thumbnail.jpg"
    for path in (audio, subtitle, cover):
        path.write_bytes(b"test")
    output_dir = tmp_path / "preview"
    output_dir.mkdir()
    legacy_subtitle = output_dir / "live_preview.srt"
    legacy_subtitle.write_text("legacy", encoding="utf-8")
    captured = {}

    monkeypatch.setattr(preview, "ensure_ffmpeg_for_preview", lambda: None)
    monkeypatch.setattr(preview, "command_exists", lambda name: True)
    monkeypatch.setattr(preview, "prepare_cover", lambda source, target, logger: target.write_bytes(source.read_bytes()))

    def fake_original(chinese_subtitle, target_dir):
        path = target_dir / "live_preview.ja.srt"
        path.write_text("原文", encoding="utf-8")
        return path

    def fake_bilingual(chinese_subtitle, original_subtitle, target_dir):
        path = target_dir / "live_preview.bilingual.srt"
        path.write_text("日文\n中文", encoding="utf-8")
        return path

    def fake_build(**kwargs):
        captured.update(kwargs)
        kwargs["output"].write_bytes(b"video")

    monkeypatch.setattr(preview, "maybe_copy_original_subtitle", fake_original)
    monkeypatch.setattr(preview, "maybe_write_bilingual_subtitle", fake_bilingual)
    monkeypatch.setattr(preview, "maybe_write_study_subtitle", lambda *args: None)
    monkeypatch.setattr(preview, "build_preview_video", fake_build)
    monkeypatch.setattr(preview, "copy_learning_notes", lambda *args: {})
    monkeypatch.setattr(preview, "write_readme", lambda *args: None)

    preview.create_potplayer_preview(
        preview.PreviewOptions(
            audio=audio,
            subtitle=subtitle,
            cover=cover,
            output_dir=output_dir,
            resolution="1280x720",
            subtitle_name="live_preview.zh.srt",
            video_name="live_preview.mp4",
            mode="potplayer",
        )
    )

    assert captured["subtitle"] == output_dir / "live_preview.bilingual.srt"
    assert not legacy_subtitle.exists()
    assert (output_dir / "live_preview.zh.srt").exists()
    assert (output_dir / "live_preview.bilingual.srt").exists()


@pytest.mark.parametrize("name", ["normal", "speaker's", "中文 空格 [1],semi;"])
def test_subtitle_path_with_real_ffmpeg(tmp_path, name):
    bundled = Path(__file__).resolve().parents[1] / "tools" / "ffmpeg.exe"
    ffmpeg = str(bundled) if bundled.exists() else shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is unavailable")
    directory = tmp_path / name
    directory.mkdir()
    subtitle = directory / f"{name}.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:00,500\nHello\n", encoding="utf-8")
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-f", "lavfi", "-i", "color=s=320x180:d=0.1",
         "-vf", preview.build_burn_subtitle_filter(subtitle), "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_preview_burns_subtitles_and_uses_audio_duration(monkeypatch, tmp_path: Path) -> None:
    cover = tmp_path / "cover.jpg"
    audio = tmp_path / "audio.m4a"
    subtitle = tmp_path / "translation_zh.srt"
    output = tmp_path / "live_preview.mp4"
    for path in (cover, audio, subtitle):
        path.write_bytes(b"test")

    captured = {}

    monkeypatch.setattr(preview, "probe_media_duration", lambda path, logger: 3.5)
    monkeypatch.setattr(
        preview,
        "validate_preview_video",
        lambda path, expected_duration, logger: captured.update(
            validated=(path, expected_duration)
        ),
    )

    def fake_run(command, logger, *, stream_output=False):
        captured["command"] = command
        captured["stream_output"] = stream_output
        output.write_bytes(b"video")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(preview, "run_subprocess", fake_run)
    preview.build_preview_video(
        audio=audio,
        cover=cover,
        subtitle=subtitle,
        output=output,
        width=1280,
        height=720,
        logger=RunLogger(tmp_path / "run.log"),
    )

    command = captured["command"]
    vf = command[command.index("-vf") + 1]
    assert "subtitles=filename=" in vf
    assert "force_style=" in vf
    assert "PrimaryColour=&H00FFFFFF" in vf
    assert "OutlineColour=&H00000000" in vf
    assert "BackColour=&H80000000" in vf
    assert "-shortest" not in command
    assert command[command.index("-t") + 1] == "3.500"
    assert captured["validated"] == (output, 3.5)
    assert captured["stream_output"] is True


def test_preview_duration_parser_and_validation_reject_mismatch(monkeypatch, tmp_path: Path) -> None:
    assert preview.parse_media_duration("Duration: 02:45:56.03") == pytest.approx(9956.03)

    monkeypatch.setattr(
        preview,
        "probe_media_text",
        lambda path, logger: (
            "Duration: 00:00:02.00\n"
            "Stream #0:0: Video: h264\n"
            "Stream #0:1: Audio: aac"
        ),
    )
    with pytest.raises(preview.AppError, match="时长"):
        preview.validate_preview_video(
            tmp_path / "live_preview.mp4",
            3.5,
            RunLogger(tmp_path / "run.log", mirror_stdout=False),
        )
