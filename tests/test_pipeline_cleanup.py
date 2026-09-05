from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import cli
from app.cleaner import clean_segments
from app.schemas import TranscriptSegment, TranscriptWord


def _fragment_segment(
    segment_id: int,
    text: str,
    start: float,
    *,
    duration: float = 0.3,
) -> TranscriptSegment:
    end = start + duration
    return TranscriptSegment(
        id=segment_id,
        start=start,
        end=end,
        start_text="",
        end_text="",
        text=text,
        avg_logprob=-0.3,
        no_speech_prob=0.0,
        words=[
            TranscriptWord(
                word=f" {text}" if text.isascii() else text,
                start=start,
                end=end,
                probability=0.95,
            )
        ],
    )


def test_pipeline_cleanup_removes_only_generated_clean_wav(
    monkeypatch, tmp_path: Path
) -> None:
    group = tmp_path / "media" / "sample"
    audio_dir = group / "audio"
    transcripts_dir = group / "transcripts"
    audio_dir.mkdir(parents=True)
    transcripts_dir.mkdir(parents=True)
    clean_wav = audio_dir / "sample_clean_16k.wav"
    source_wav = audio_dir / "sample_source.wav"
    other_wav = audio_dir / "notes.wav"
    transcript = transcripts_dir / "sample_transcript.json"
    clean_wav.write_bytes(b"clean")
    source_wav.write_bytes(b"source")
    other_wav.write_bytes(b"other")
    transcript.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli, "group_dir_from_artifact_path", lambda path: group)

    deleted = cli._cleanup_generated_pipeline_wavs(transcript, source_wav)

    assert deleted == [clean_wav]
    assert not clean_wav.exists()
    assert source_wav.exists()
    assert other_wav.exists()


def test_pipeline_cleanup_removes_source_m4a_only_after_valid_preview(
    monkeypatch, tmp_path: Path
) -> None:
    group = tmp_path / "media" / "sample"
    audio_dir = group / "audio"
    transcripts_dir = group / "transcripts"
    preview_dir = group / "previews" / "potplayer"
    audio_dir.mkdir(parents=True)
    transcripts_dir.mkdir(parents=True)
    preview_dir.mkdir(parents=True)
    source_m4a = audio_dir / "sample_source.m4a"
    unrelated_m4a = audio_dir / "recording.m4a"
    transcript = transcripts_dir / "sample_transcript.json"
    preview = preview_dir / "live_preview.mp4"
    source_m4a.write_bytes(b"source")
    unrelated_m4a.write_bytes(b"other")
    transcript.write_text(
        json.dumps({"meta": {"source_audio_file": str(source_m4a)}}),
        encoding="utf-8",
    )
    preview.write_bytes(b"video")
    monkeypatch.setattr(cli, "group_dir_from_artifact_path", lambda path: group)

    deleted = cli._cleanup_pipeline_source_m4a(transcript, source_m4a, preview)

    assert deleted == [source_m4a.resolve()]
    assert not source_m4a.exists()
    assert unrelated_m4a.exists()


@pytest.mark.parametrize("preview_bytes", [None, b""])
def test_pipeline_cleanup_keeps_source_m4a_without_completed_preview(
    monkeypatch, tmp_path: Path, preview_bytes: bytes | None
) -> None:
    group = tmp_path / "media" / "sample"
    audio_dir = group / "audio"
    preview_dir = group / "previews" / "potplayer"
    audio_dir.mkdir(parents=True)
    preview_dir.mkdir(parents=True)
    source_m4a = audio_dir / "sample_source.m4a"
    preview = preview_dir / "live_preview.mp4"
    source_m4a.write_bytes(b"source")
    if preview_bytes is not None:
        preview.write_bytes(preview_bytes)
    monkeypatch.setattr(cli, "group_dir_from_artifact_path", lambda path: group)

    deleted = cli._cleanup_pipeline_source_m4a(None, source_m4a, preview)

    assert deleted == []
    assert source_m4a.exists()


def test_pipeline_cleanup_keeps_external_m4a(
    monkeypatch, tmp_path: Path
) -> None:
    group = tmp_path / "media" / "sample"
    preview_dir = group / "previews" / "potplayer"
    preview_dir.mkdir(parents=True)
    external_m4a = tmp_path / "external_source.m4a"
    preview = preview_dir / "live_preview.mp4"
    external_m4a.write_bytes(b"source")
    preview.write_bytes(b"video")
    monkeypatch.setattr(cli, "group_dir_from_artifact_path", lambda path: group)

    deleted = cli._cleanup_pipeline_source_m4a(None, external_m4a, preview)

    assert deleted == []
    assert external_m4a.exists()


def test_clean_segments_repairs_extreme_stretched_tail_timestamp() -> None:
    segment = TranscriptSegment(
        id=589,
        start=1505.86,
        end=1526.86,
        start_text="00:25:05.860",
        end_text="00:25:26.860",
        text="最中下層じゃなきゃダメ",
        words=[
            TranscriptWord(word="最", start=1505.86, end=1505.94),
            TranscriptWord(word="中", start=1505.94, end=1506.44),
            TranscriptWord(word="下", start=1506.44, end=1508.16),
            TranscriptWord(word="層", start=1508.16, end=1508.40),
            TranscriptWord(word="じゃ", start=1508.40, end=1509.08),
            TranscriptWord(word="な", start=1509.08, end=1509.10),
            TranscriptWord(word="き", start=1509.10, end=1509.26),
            TranscriptWord(word="ゃ", start=1509.26, end=1520.54),
            TranscriptWord(word="ダ", start=1520.54, end=1520.58),
            TranscriptWord(word="メ", start=1520.58, end=1526.86),
        ],
    )

    cleaned = clean_segments([segment])[0]

    assert cleaned.end == 1509.76
    assert cleaned.end_text == "00:25:09.760"
    assert cleaned.text == segment.text


def test_clean_segments_leaves_normal_long_segment_unchanged() -> None:
    segment = TranscriptSegment(
        id=1,
        start=0.0,
        end=12.0,
        start_text="00:00:00.000",
        end_text="00:00:12.000",
        text="これは十分に長い自然な字幕文章で通常の速度で話しています",
        words=[
            TranscriptWord(word="これは", start=0.0, end=1.0),
            TranscriptWord(word="十分に", start=1.0, end=2.0),
            TranscriptWord(word="長い", start=2.0, end=3.0),
            TranscriptWord(word="自然な", start=3.0, end=4.0),
            TranscriptWord(word="字幕文章で", start=4.0, end=6.0),
            TranscriptWord(word="通常の速度で", start=6.0, end=9.0),
            TranscriptWord(word="話しています", start=9.0, end=12.0),
        ],
    )

    cleaned = clean_segments([segment])[0]

    assert cleaned.end == segment.end
    assert cleaned.end_text == segment.end_text


def test_clean_segments_repairs_low_confidence_leading_anchor_before_long_gap() -> None:
    segment = TranscriptSegment(
        id=1192,
        start=8528.46,
        end=8535.97,
        start_text="02:22:08.460",
        end_text="02:22:15.970",
        text="サンマーさん",
        words=[
            TranscriptWord(word="サ", start=8528.46, end=8528.78, probability=0.024),
            TranscriptWord(word="ン", start=8535.57, end=8535.67, probability=0.868),
            TranscriptWord(word="マー", start=8535.67, end=8535.81, probability=0.993),
            TranscriptWord(word="さん", start=8535.81, end=8535.97, probability=0.982),
        ],
    )

    cleaned = clean_segments([segment])[0]

    assert cleaned.start == pytest.approx(8535.37)
    assert cleaned.start_text == "02:22:15.370"
    assert cleaned.end == segment.end
    assert cleaned.text == segment.text
    assert cleaned.words == segment.words


def test_clean_segments_leaves_high_confidence_leading_pause_unchanged() -> None:
    segment = TranscriptSegment(
        id=1,
        start=100.0,
        end=106.0,
        start_text="00:01:40.000",
        end_text="00:01:46.000",
        text="えー 次の話です",
        words=[
            TranscriptWord(word="えー", start=100.0, end=100.4, probability=0.92),
            TranscriptWord(word="次の話です", start=104.0, end=106.0, probability=0.98),
        ],
    )

    cleaned = clean_segments([segment])[0]

    assert cleaned.start == segment.start
    assert cleaned.start_text == segment.start_text


def test_clean_segments_leaves_internal_long_gap_unchanged() -> None:
    segment = TranscriptSegment(
        id=1,
        start=200.0,
        end=207.0,
        start_text="00:03:20.000",
        end_text="00:03:27.000",
        text="前半 後半",
        words=[
            TranscriptWord(word="前", start=200.0, end=200.3, probability=0.99),
            TranscriptWord(word="半", start=200.3, end=200.6, probability=0.99),
            TranscriptWord(word="後", start=205.0, end=205.3, probability=0.02),
            TranscriptWord(word="半", start=205.3, end=205.6, probability=0.99),
        ],
    )

    cleaned = clean_segments([segment])[0]

    assert cleaned.start == segment.start
    assert cleaned.start_text == segment.start_text


def test_clean_segments_merges_only_detected_dense_fragment_run() -> None:
    fragments = [
        _fragment_segment(index + 1, f"word{index + 1}", index * 0.5)
        for index in range(40)
    ]

    cleaned = clean_segments(fragments)

    assert len(cleaned) == 2
    assert [len(segment.words) for segment in cleaned] == [20, 20]
    assert cleaned[0].text.startswith("word1 word2 word3")
    assert cleaned[0].text.endswith("word20")
    assert cleaned[0].start == 0.0
    assert cleaned[0].end == pytest.approx(9.8)
    assert [segment.id for segment in cleaned] == [1, 2]


def test_clean_segments_keeps_punctuation_and_long_pause_as_merge_boundaries() -> None:
    fragments: list[TranscriptSegment] = []
    next_start = 0.0
    for index in range(40):
        text = "stop." if index == 9 else f"word{index + 1}"
        if index == 20:
            next_start += 1.5
        fragments.append(_fragment_segment(index + 1, text, next_start))
        next_start += 0.5

    cleaned = clean_segments(fragments)

    assert any(segment.text.endswith("stop.") for segment in cleaned)
    stop_index = next(index for index, segment in enumerate(cleaned) if segment.text.endswith("stop."))
    assert cleaned[stop_index + 1].text.startswith("word11")
    assert any(
        left.text.endswith("word20") and right.text.startswith("word21")
        for left, right in zip(cleaned, cleaned[1:])
    )


def test_clean_segments_does_not_merge_across_an_internal_word_gap() -> None:
    fragments = [
        _fragment_segment(index + 1, f"word{index + 1}", index * 0.5 + (1.5 if index > 20 else 0.0))
        for index in range(40)
    ]
    fragments[20] = TranscriptSegment(
        id=21,
        start=10.0,
        end=11.8,
        start_text="",
        end_text="",
        text="long pause",
        words=[
            TranscriptWord(word=" long", start=10.0, end=10.2, probability=0.95),
            TranscriptWord(word=" pause", start=11.5, end=11.8, probability=0.95),
        ],
    )

    cleaned = clean_segments(fragments)

    isolated = next(segment for segment in cleaned if segment.text == "long pause")
    assert isolated.start == 10.0
    assert isolated.end == 11.8


def test_clean_segments_does_not_merge_an_isolated_short_exchange() -> None:
    fragments = [
        _fragment_segment(1, "yes", 0.0),
        _fragment_segment(2, "no", 0.5),
        _fragment_segment(3, "okay", 1.0),
    ]

    cleaned = clean_segments(fragments)

    assert [segment.text for segment in cleaned] == ["yes", "no", "okay"]


def test_clean_segments_joins_cjk_fragments_without_spaces() -> None:
    fragments = [
        _fragment_segment(index + 1, character, index * 0.5)
        for index, character in enumerate("これは安全な字幕結合の確認ですこれは安全な字幕結合の確認ですこれは安全な字幕結合の確認ですこれは安全です")
    ]

    cleaned = clean_segments(fragments)

    assert len(cleaned) < len(fragments)
    assert all(" " not in segment.text for segment in cleaned)
