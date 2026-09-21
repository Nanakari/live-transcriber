from pathlib import Path
from types import SimpleNamespace
import json
import runpy
import shutil
import subprocess

import pytest

from app import preview
from app.subtitles import (
    EMPTY_TRANSLATION_PLACEHOLDER,
    split_cues_by_word_timestamps,
    write_display_subtitles,
)
from app.utils import RunLogger


def test_default_audio_preview_skips_video_encoding(tmp_path: Path, monkeypatch) -> None:
    audio = tmp_path / "audio.m4a"
    subtitle = tmp_path / "translation_zh.srt"
    output_dir = tmp_path / "audio_preview"
    audio.write_bytes(b"test")
    subtitle.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n原文\n中文\n",
        encoding="utf-8",
    )

    def fail_if_called(**kwargs):
        raise AssertionError("audio mode must not encode a video")

    monkeypatch.setattr(preview, "build_preview_video", fail_if_called)
    result = preview.create_preview(
        preview.PreviewOptions(
            audio=audio,
            subtitle=subtitle,
            cover=None,
            output_dir=output_dir,
            resolution="1280x720",
            subtitle_name="live_preview.zh.srt",
            video_name="live_preview.mp4",
            mode="audio",
        )
    )

    assert result["audio_player"].exists()
    assert result["audio_launcher"].exists()
    assert result["audio_subtitle"].exists()
    assert result["readme"].exists()
    assert not (output_dir / "live_preview.mp4").exists()


def test_srt_parser_preserves_numeric_subtitle_text(tmp_path: Path) -> None:
    subtitle = tmp_path / "numeric.srt"
    subtitle.write_text(
        "1\n00:00:01,000 --> 00:00:01,500\n1\n\n"
        "2\n00:00:02,000 --> 00:00:02,500\n2\n中文\n",
        encoding="utf-8",
    )

    blocks = preview.read_srt_blocks(subtitle)

    assert [block["text"] for block in blocks] == ["1", "2\n中文"]


def test_long_cue_is_split_at_word_timestamp_boundaries() -> None:
    cue = {
        "start": 10.0,
        "end": 31.0,
        "original": "これは長い原文です。さらに続きます。最後です。",
        "translation": "这是一段很长的原文。还在继续。最后一句。",
    }
    words = [
        {"word": "これは", "start": 10.0, "end": 13.0},
        {"word": "長い", "start": 13.0, "end": 17.0},
        {"word": "原文です", "start": 17.0, "end": 21.0},
        {"word": "さらに", "start": 21.0, "end": 25.0},
        {"word": "続きます", "start": 25.0, "end": 28.0},
        {"word": "最後です", "start": 28.0, "end": 31.0},
    ]

    result = split_cues_by_word_timestamps([cue], [words], max_duration=9.0)

    assert len(result) == 3
    assert max(item["end"] - item["start"] for item in result) <= 9.0
    assert "".join(item["original"] for item in result) == cue["original"]
    assert "".join(item["translation"] for item in result) == cue["translation"]


def test_display_split_preserves_zero_duration_word_text_and_source_alignment() -> None:
    cue = {
        "start": 0.0,
        "end": 15.0,
        "original": "甲乙零丙丁戊",
        "translation": "甲乙零丙丁戊",
    }
    words = [
        {"word": "甲", "start": 0.0, "end": 3.0},
        {"word": "乙", "start": 3.0, "end": 6.0},
        {"word": "零", "start": 6.0, "end": 6.0},
        {"word": "丙", "start": 6.0, "end": 9.0},
        {"word": "丁", "start": 9.0, "end": 12.0},
        {"word": "戊", "start": 12.0, "end": 15.0},
    ]

    result = split_cues_by_word_timestamps([cue], [words], max_duration=9.0)

    assert [item["original"] for item in result] == ["甲乙零丙", "丁戊"]
    assert "".join(item["original"] for item in result) == cue["original"]


def test_display_split_is_conservative_for_unsorted_word_timestamps() -> None:
    cue = {
        "start": 0.0,
        "end": 20.0,
        "original": "one two three",
        "translation": "一二三",
    }
    words = [
        {"word": "one", "start": 10.0, "end": 12.0},
        {"word": "two", "start": 4.0, "end": 8.0},
    ]

    result = split_cues_by_word_timestamps([cue], [words], max_duration=9.0)

    assert result == [cue]


def test_word_timestamps_follow_exported_segment_id_order(tmp_path: Path) -> None:
    analysis_dir = tmp_path / "analysis"
    analysis_dir.mkdir()
    transcript = tmp_path / "transcript.json"
    transcript.write_text(
        '{"segments": ['
        '{"id": 1, "words": [{"word": "first", "start": 0, "end": 1}]},'
        '{"id": 2, "words": [{"word": "second", "start": 1, "end": 2}]}'
        ']}',
        encoding="utf-8",
    )
    (analysis_dir / "analysis.json").write_text(
        '{"meta": {"input_file": "../transcript.json"}, "chunks": [{'
        '"bilingual_lines": ['
        '{"segment_id": 2, "translation_zh": "二"},'
        '{"segment_id": 1, "translation_zh": "一"}'
        ']}]}',
        encoding="utf-8",
    )
    subtitle = analysis_dir / "translation_zh.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n二\n", encoding="utf-8")

    result = preview.load_word_timed_segments(subtitle)

    assert [words[0]["word"] for words in result] == ["second", "first"]


def test_duplicate_analysis_segment_ids_disable_word_timing(tmp_path: Path) -> None:
    analysis_dir = tmp_path / "analysis"
    analysis_dir.mkdir()
    transcript = tmp_path / "transcript.json"
    transcript.write_text(
        '{"segments": [{"id": 1, "words": [{"word": "one", "start": 0, "end": 1}]}]}',
        encoding="utf-8",
    )
    (analysis_dir / "analysis.json").write_text(
        json.dumps({
            "meta": {"input_file": "../transcript.json"},
            "chunks": [{"bilingual_lines": [
                {"segment_id": 1, "translation_zh": "一"},
                {"segment_id": 1, "translation_zh": "仍是一"},
            ]}],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    subtitle = analysis_dir / "translation_zh.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n一\n", encoding="utf-8")

    assert preview.load_word_timed_segments(subtitle) == []


def test_short_translation_subcues_keep_two_line_player_roles(tmp_path: Path) -> None:
    cue = {
        "start": 0.0,
        "end": 27.0,
        "original": "原文一 原文二 原文三",
        "translation": "中",
    }
    words = [[
        {"word": "原文一", "start": 0.0, "end": 9.0},
        {"word": "原文二", "start": 9.0, "end": 18.0},
        {"word": "原文三", "start": 18.0, "end": 27.0},
    ]]
    pieces = split_cues_by_word_timestamps([cue], words, max_duration=9.0)
    srt = tmp_path / "display.srt"
    write_display_subtitles(pieces, srt, tmp_path / "display.ass")

    player = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "app" / "assets" / "floating_subtitle_overlay.pyw")
    )
    loaded = player["load_cues"](srt)

    assert [(item.source, item.translation) for item in loaded] == [
        ("原文一", EMPTY_TRANSLATION_PLACEHOLDER),
        ("原文二", "中"),
        ("原文三", EMPTY_TRANSLATION_PLACEHOLDER),
    ]


def test_external_srt_reorders_or_disables_auxiliary_data(tmp_path: Path) -> None:
    logger = RunLogger(tmp_path / "run.log", mirror_stdout=False)
    blocks = [
        {"start": 1.0, "end": 2.0, "text": "一"},
        {"start": 2.0, "end": 3.0, "text": "二"},
    ]
    metadata = [
        {"segment_id": 2, "start": 2.0, "end": 3.0, "translation_zh": "二"},
        {"segment_id": 1, "start": 1.0, "end": 2.0, "translation_zh": "一"},
    ]
    words = [
        [{"word": "two", "start": 2.0, "end": 3.0}],
        [{"word": "one", "start": 1.0, "end": 2.0}],
    ]

    aligned_words, aligned_metadata = preview.align_display_auxiliary_data(
        blocks, words, metadata, logger
    )

    assert [item[0]["word"] for item in aligned_words] == ["one", "two"]
    assert [item["segment_id"] for item in aligned_metadata] == [1, 2]

    deleted_block = preview.align_display_auxiliary_data(
        blocks[:1], words, metadata, logger
    )
    assert deleted_block == ([], [])

    blocked_blocks = [{"start": 1.0, "end": 2.0, "text": "原文 [需要复查]"}]
    blocked_metadata = [{
        "segment_id": 1,
        "start": 1.0,
        "end": 2.0,
        "original": "原文",
        "translation_zh": "",
        "translation_status": "blocked_asr_review",
        "asr_suspect": True,
    }]
    blocked_words = [[{"word": "原文", "start": 1.0, "end": 2.0}]]

    aligned_words, aligned_metadata = preview.align_display_auxiliary_data(
        blocked_blocks, blocked_words, blocked_metadata, logger
    )

    assert aligned_words == blocked_words
    assert aligned_metadata[0]["translation_status"] == "blocked_asr_review"


def test_original_subtitle_copy_requires_related_exact_candidate(tmp_path: Path, monkeypatch) -> None:
    analysis_dir = tmp_path / "analysis"
    analysis_dir.mkdir()
    subtitle = analysis_dir / "translation_zh.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n中文\n", encoding="utf-8")
    (analysis_dir / "analysis.json").write_text(
        json.dumps({"meta": {"transcript_run_id": "wanted"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    output_dir = tmp_path / "preview"
    output_dir.mkdir()
    transcript_root = tmp_path / "outputs" / "transcripts"
    transcript_root.mkdir(parents=True)
    (transcript_root / "unrelated_transcript.srt").write_text("错的原文", encoding="utf-8")
    monkeypatch.setattr(preview, "project_root", lambda: tmp_path)

    assert preview.maybe_copy_original_subtitle(subtitle, output_dir) is None
    (transcript_root / "wanted_transcript.srt").write_text("正确的原文", encoding="utf-8")

    copied = preview.maybe_copy_original_subtitle(subtitle, output_dir)

    assert copied == output_dir / "live_preview.ja.srt"
    assert copied.read_text(encoding="utf-8") == "正确的原文"


def test_missing_and_blocked_markers_survive_every_display_subcue(tmp_path: Path) -> None:
    words = [
        {"word": "a", "start": 0.0, "end": 7.0},
        {"word": "b", "start": 7.0, "end": 14.0},
        {"word": "c", "start": 14.0, "end": 21.0},
    ]
    missing = {
        "start": 0.0,
        "end": 21.0,
        "original": "abc" * 20,
        "translation": "[翻译暂缺] " + ("abc" * 20),
        "translation_status": "missing",
    }
    blocked = {
        **missing,
        "translation": "[原文待复核，翻译暂缺]",
        "translation_status": "blocked_asr_review",
        "asr_suspect": True,
    }

    missing_result = split_cues_by_word_timestamps([missing], [words], max_duration=9.0)
    blocked_result = split_cues_by_word_timestamps([blocked], [words], max_duration=9.0)

    assert len(missing_result) == len(blocked_result) == 3
    assert all(item["translation"].startswith("[翻译暂缺]") for item in missing_result)
    assert [item["translation"] for item in blocked_result] == [
        "[原文待复核，翻译暂缺]"
    ] * 3
    assert all(("abc" * 20) not in item["translation"] for item in blocked_result)

    srt = tmp_path / "display.srt"
    ass = tmp_path / "display.ass"
    from app.subtitles import write_display_subtitles
    write_display_subtitles(missing_result + blocked_result, srt, ass)
    rendered = srt.read_text(encoding="utf-8")
    assert rendered.count("[翻译暂缺]") >= 3
    assert rendered.count("[原文待复核，翻译暂缺]") >= 3

    short_blocked = {
        "start": 0.0,
        "end": 5.0,
        "original": "严重重复原文" * 8,
        "translation": "[翻译暂缺] " + ("严重重复原文" * 8),
        "translation_status": "blocked_asr_review",
        "asr_suspect": True,
    }
    short_srt = tmp_path / "short-blocked.srt"
    write_display_subtitles([short_blocked], tmp_path / "short-blocked.srt", tmp_path / "short-blocked.ass")
    short_rendered = short_srt.read_text(encoding="utf-8")
    assert "[原文待复核，翻译暂缺]" in short_rendered
    assert ("严重重复原文" * 8) not in short_rendered.strip().splitlines()[-1]


def test_long_translation_without_punctuation_stays_proportional() -> None:
    cue = {
        "start": 0.0,
        "end": 30.0,
        "original": "原文" * 30,
        "translation": "abcdefghijklmnopqrstuvwxyz" * 4,
    }
    words = [
        {"word": "一", "start": 0.0, "end": 10.0},
        {"word": "二", "start": 10.0, "end": 20.0},
        {"word": "三", "start": 20.0, "end": 30.0},
    ]

    result = split_cues_by_word_timestamps([cue], [words], max_duration=9.0)
    lengths = [len(item["translation"]) for item in result]

    assert len(result) == 3
    assert max(lengths) - min(lengths) <= 2


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
    monkeypatch.setattr(preview, "write_readme", lambda *args, **kwargs: None)

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

    assert captured["subtitle"] == output_dir / "subtitles/display.bilingual.ass"
    assert not legacy_subtitle.exists()
    assert (output_dir / "subtitles/live_preview.zh.srt").exists()
    assert (output_dir / "subtitles/live_preview.bilingual.srt").exists()
    assert (output_dir / "floating_subtitle_overlay.pyw").exists()
    assert (output_dir / "run_floating_subtitle_overlay.cmd").exists()
    assert (output_dir / "audio/audio_subtitle_player.pyw").exists()
    assert (output_dir / "audio/run_audio_subtitle_player.cmd").exists()
    assert (output_dir / "audio/audio_mode.bilingual.srt").exists()


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
    assert "OutlineColour=&H00141414" in vf
    assert "BackColour=&HFF000000" in vf
    assert "BorderStyle=1" in vf
    assert command[command.index("-framerate") + 1] == "25"
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
