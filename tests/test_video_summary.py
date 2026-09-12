import json

import pytest

from app.analysis.summary import generate_video_summary
from app.analysis.exporters import export_all
from app.analysis.schemas import AnalysisDocument
from app.utils import RunLogger
from test_analysis_profiles import _document


def test_summary_receives_every_chunk_and_is_saved_with_sources(tmp_path):
    document = _document()
    class Client:
        def generate_json_text(self, prompt, **kwargs):
            for chunk in document.chunks:
                assert chunk.chunk_id in prompt
                assert chunk.chunk_summary_zh in prompt
                assert all(point in prompt for point in chunk.key_points_zh)
            return json.dumps({"points": [{"text": "下周五举行纪念直播，另有生活闲聊。", "source_chunk_ids": [c.chunk_id for c in document.chunks]}]})
    generate_video_summary(document, Client(), RunLogger(tmp_path / "run.log"))
    paths = export_all(document, tmp_path)
    saved = AnalysisDocument.model_validate_json(paths["analysis_json"].read_text(encoding="utf-8"))
    assert saved.video_summary == document.video_summary
    assert not saved.video_summary_error
    text = paths["video_summary_md"].read_text(encoding="utf-8")
    assert "核心要点" in text and "00:02:00" in text


@pytest.mark.parametrize("response", ["bad json", '{"points": []}', '{"points":[{"text":"invented","source_chunk_ids":["unknown"]}]}'])
def test_invalid_summary_falls_back_without_losing_exports(tmp_path, response):
    class Client:
        def generate_json_text(self, *args, **kwargs):
            return response
    document = _document()
    generate_video_summary(document, Client(), RunLogger(tmp_path / "run.log"))
    paths = export_all(document, tmp_path)
    assert document.video_summary is None and document.video_summary_error
    assert paths["translation_srt"].exists()
    text = paths["video_summary_md"].read_text(encoding="utf-8")
    assert "纪念直播" in text and "今天早上吃了面包" in text


def test_summary_disabled_makes_no_request(tmp_path):
    document = _document()
    document.meta.summary = False
    class Client:
        def generate_json_text(self, *args, **kwargs):
            pytest.fail("summary disabled must not make requests")
    generate_video_summary(document, Client(), RunLogger(tmp_path / "run.log"))
    assert "video_summary_md" not in export_all(document, tmp_path)


def test_summary_network_failure_preserves_partial_warning(tmp_path):
    document = _document()
    document.meta.failed_chunks = 1
    class Client:
        def generate_json_text(self, *args, **kwargs):
            raise RuntimeError("network failed")
    generate_video_summary(document, Client(), RunLogger(tmp_path / "run.log"))
    paths = export_all(document, tmp_path)
    text = paths["video_summary_md"].read_text(encoding="utf-8")
    assert "部分内容分析失败" in text and "分段摘要" in text
    assert document.video_summary_error == "network failed"
