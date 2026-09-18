from pathlib import Path

from app.floating_overlay import (
    AUDIO_MODE_SUBTITLE_FILENAME,
    AUDIO_PLAYER_LAUNCHER_FILENAME,
    AUDIO_PLAYER_FILENAME,
    write_audio_subtitle_player,
    write_overlay_launcher,
)


def test_generated_audio_player_is_standalone_and_uses_compact_overlay(tmp_path: Path) -> None:
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    audio = audio_dir / "source.m4a"
    subtitle = audio_dir / AUDIO_MODE_SUBTITLE_FILENAME
    audio.write_bytes(b"audio")
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n原文\n中文\n",
        encoding="utf-8",
    )

    target = audio_dir / AUDIO_PLAYER_FILENAME
    result = write_audio_subtitle_player(target, audio, subtitle)
    content = result.read_text(encoding="utf-8")

    assert result == target
    assert f'DEFAULT_SUBTITLE_RELATIVE = "{AUDIO_MODE_SUBTITLE_FILENAME}"' in content
    assert 'DEFAULT_AUDIO_RELATIVE = "source.m4a"' in content
    assert 'COMPACT_BACKGROUND_COLOR = "#161a20"' in content
    assert "-nodisp" in content
    assert "start_audio" in content
    assert "class AudioPlayback" in content
    assert "probe_audio_duration" in content
    assert "ttk.Scale" in content
    assert 'text="暂停"' in content
    assert 'text="-10s"' in content
    assert 'text="+10s"' in content
    assert "audio_player=" in content
    assert "root.attributes(\"-topmost\", True)" in content


def test_generated_audio_launcher_points_to_pythonw_player(tmp_path: Path) -> None:
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    target = audio_dir / AUDIO_PLAYER_LAUNCHER_FILENAME

    result = write_overlay_launcher(target, AUDIO_PLAYER_FILENAME)
    content = result.read_text(encoding="utf-8")

    assert result == target
    assert f'set "SCRIPT=%~dp0{AUDIO_PLAYER_FILENAME}"' in content
    assert 'set "PYTHONW=' in content
    assert content.lower().rstrip().endswith("exit /b 0")
    assert 'start "" "%PYTHONW%" "%SCRIPT%" %*' in content
