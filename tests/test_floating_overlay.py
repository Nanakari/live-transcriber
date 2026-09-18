from pathlib import Path

from app.floating_overlay import write_floating_subtitle_overlay, write_overlay_launcher


def test_generated_overlay_uses_reference_compact_style(tmp_path: Path) -> None:
    video_dir = tmp_path / "video"
    subtitle = video_dir / "subtitles" / "display.bilingual.srt"
    subtitle.parent.mkdir(parents=True)
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n原文\n中文\n",
        encoding="utf-8",
    )

    target = video_dir / "floating_subtitle_overlay.pyw"
    result = write_floating_subtitle_overlay(target, subtitle)
    content = result.read_text(encoding="utf-8")

    assert result == target
    assert 'DEFAULT_SUBTITLE_RELATIVE = "subtitles/display.bilingual.srt"' in content
    assert 'COMPACT_BACKGROUND_COLOR = "#161a20"' in content
    assert 'COMPACT_BORDER_COLOR = "#3b4654"' in content
    assert "COMPACT_FONT_SIZE = 20" in content
    assert 'SOURCE_COLOR = "#dbeafe"' in content
    assert 'TRANSLATION_COLOR = "#ffffff"' in content
    assert "root.overrideredirect(True)" in content
    assert "root.attributes(\"-topmost\", True)" in content
    assert "self.source_label.place(" in content
    assert "self.translation_label.place(" in content


def test_generated_overlay_launcher_is_clickable_without_pyw_association(tmp_path: Path) -> None:
    video_dir = tmp_path / "video"
    video_dir.mkdir(parents=True)
    target = video_dir / "run_floating_subtitle_overlay.cmd"

    result = write_overlay_launcher(target, "floating_subtitle_overlay.pyw")
    content = result.read_text(encoding="utf-8")

    assert result == target
    assert 'set "SCRIPT=%~dp0floating_subtitle_overlay.pyw"' in content
    assert 'set "PYTHONW=' in content
    assert 'start "" "%PYTHONW%" "%SCRIPT%" %*' in content
