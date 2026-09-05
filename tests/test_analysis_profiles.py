from pathlib import Path

from app.analysis.exporters import (
    export_all,
    export_character_profile_markdown,
    export_video_summary_markdown,
)
from app.analysis.schemas import (
    AnalysisDocument,
    AnalysisMeta,
    ChunkAnalysisResult,
    ProfileObservation,
)


def _document() -> AnalysisDocument:
    meta = AnalysisMeta(
        character_profile=True,
        run_id="analysis-test",
        input_file="sample.wav",
        transcript_run_id="transcript-test",
        provider="gemini",
        model="test-model",
        profile="multilingual_study",
        source_language="ja",
        target_language="zh",
        chunk_minutes=2,
        max_segments_per_chunk=28,
        prompt_version="test",
        total_chunks=2,
        succeeded_chunks=2,
    )
    important = ChunkAnalysisResult(
        chunk_id="chunk_0001",
        start=0,
        end=120,
        chunk_summary_zh="开场介绍后，主要说话人公布了下周活动安排。",
        key_points_zh=["下周五将举行纪念直播，开始时间为晚上八点。"],
        content_importance=4.8,
        profile_observations=[
            ProfileObservation(
                speaker_label="主要说话人",
                category="preference",
                observation_zh="喜欢解谜游戏",
                evidence_zh="本人直接表示最近最喜欢解谜类游戏。",
                confidence=0.95,
            )
        ],
    )
    casual = ChunkAnalysisResult(
        chunk_id="chunk_0002",
        start=120,
        end=240,
        chunk_summary_zh="随后聊到当天的天气和早餐，整体是轻松闲聊。",
        key_points_zh=["今天早上吃了面包。"],
        content_importance=1.0,
    )
    return AnalysisDocument(meta=meta, chunks=[important, casual])


def test_video_summary_filters_chat_and_adds_full_overview(tmp_path: Path) -> None:
    output = tmp_path / "video_summary.md"
    export_video_summary_markdown(_document(), output)
    text = output.read_text(encoding="utf-8")
    assert "## 要点总结" in text
    assert "### 1. 00:00:00,000 - 00:02:00,000" in text
    assert "纪念直播" in text
    assert "今天早上吃了面包" not in text
    assert "## 全文概括" in text
    assert "天气和早餐" in text


def test_character_profile_contains_evidence_without_json(tmp_path: Path) -> None:
    output = tmp_path / "character_profile.md"
    export_character_profile_markdown(_document(), output)
    text = output.read_text(encoding="utf-8")
    assert "# 人物 Profile" in text
    assert "## 主要说话人" in text
    assert "### 喜好与兴趣" in text
    assert "喜欢解谜游戏" in text
    assert "本人直接表示" in text

    all_dir = tmp_path / "all"
    all_dir.mkdir()
    paths = export_all(_document(), all_dir)
    assert "character_profile_md" in paths
    assert "task_profile_json" not in paths
    assert not (all_dir / "task_profile.json").exists()
