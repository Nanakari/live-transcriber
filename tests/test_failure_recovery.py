import json

import pytest

from app import cli
from app.analysis import analyzer, gemini_client
from app.analysis.gemini_client import PermanentAnalysisError
from app.schemas import TranscriptDocument, TranscriptMeta, TranscriptSegment


@pytest.fixture
def recovery_input(monkeypatch, tmp_path):
    monkeypatch.setenv("LIVE_TRANSCRIBER_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "analysis:\n  max_segments_per_chunk: 2\n  request_interval_seconds: 0\n",
        encoding="utf-8",
    )
    path = tmp_path / "sample_transcript.json"
    path.write_text(TranscriptDocument(
        meta=TranscriptMeta(created_at="2026-10-11", run_id="sample", language="en"),
        segments=[TranscriptSegment(id=i, start=i-1, end=i, start_text="", end_text="", text=f"Source {i}")
                  for i in range(1, 7)],
    ).model_dump_json(), encoding="utf-8")
    return path


def response(*ids, summary=""):
    return json.dumps({"bilingual_lines": [{"segment_id": i, "translation_zh": f"译文{i}"} for i in ids],
                       "chunk_summary_zh": summary})


def analyze(path):
    return cli.analyze_task(cli.build_parser().parse_args(["analyze", "--input", str(path), "--resume"]))


def test_403_requests_once_and_still_exports_recoverable_results(recovery_input, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "offline-test-key")
    calls = []
    class Denied:
        status_code = 403
        text = "permission denied"
    monkeypatch.setattr(gemini_client.requests, "post", lambda *a, **kw: calls.append(a) or Denied())
    monkeypatch.setattr(gemini_client.time, "sleep", lambda _: pytest.fail("403 must not sleep"))
    result = analyze(recovery_input)
    assert len(calls) == 1
    assert result["exit_code"] == 1
    assert result["document"].meta.quality_status == "failed"
    assert result["document"].meta.failed_chunks == 3
    assert result["document"].meta.fallback_lines == 6
    assert "HTTP 403" in result["document"].analysis_error
    assert (result["output_dir"] / "translation_zh.srt").is_file()


def test_permanent_error_retains_success_and_partial_chunk_for_resume(recovery_input, monkeypatch):
    replies = iter([response(1, 2, summary="摘要"), response(3)])
    calls = []
    class Client:
        def generate_json_text(self, prompt, **kw):
            calls.append(prompt)
            try:
                return next(replies)
            except StopIteration:
                raise PermanentAnalysisError("HTTP 403")
    monkeypatch.setattr(analyzer, "_make_client", lambda *_: Client())
    first = analyze(recovery_input)
    assert len(calls) == 3  # Initial chunk, next chunk, then failed coverage; no later request/summary.
    assert first["document"].meta.fallback_lines == 3
    assert first["document"].chunks[1].bilingual_lines[0].translation_zh == "译文3"
    assert first["exit_code"] == 1

    replies = iter([response(4), response(5, 6), '{"points":[{"text":"摘要","source_chunk_ids":["chunk_0000"]}]}'])
    calls.clear()
    recovered = analyze(recovery_input)
    assert recovered["exit_code"] == 0
    assert recovered["document"].meta.skipped_chunks == 1
    assert recovered["document"].meta.fallback_lines == 0
    assert "Source 4" in calls[0] and "Source 3" not in calls[0]
    assert [line.translation_zh for c in recovered["document"].chunks for line in c.bilingual_lines] == [f"译文{i}" for i in range(1, 7)]


def test_permanent_summary_error_sets_failure_but_preserves_translations(recovery_input, monkeypatch):
    replies = iter([response(1, 2, summary="摘要"), response(3, 4), response(5, 6)])
    class Client:
        def generate_json_text(self, *a, **kw):
            try:
                return next(replies)
            except StopIteration:
                raise PermanentAnalysisError("HTTP 401")
    monkeypatch.setattr(analyzer, "_make_client", lambda *_: Client())
    result = analyze(recovery_input)
    assert result["exit_code"] == 1
    assert result["document"].analysis_error == "HTTP 401"
    assert result["document"].meta.fallback_lines == 0
    assert "译文6" in (result["output_dir"] / "translation_zh.srt").read_text(encoding="utf-8")
