import json
from pathlib import Path

from app import cli
from app.analysis import analyzer
from app.analysis.gemini_client import PermanentAnalysisError
from app.schemas import TranscriptDocument, TranscriptMeta, TranscriptSegment


def options(path):
    return analyzer.AnalyzeOptions(
        input_file=path, provider="gemini", profile="multilingual_study",
        source_language="en", target_language="zh", model="test", fallback_model="",
        api_key_env="TEST_KEY", chunk_minutes=3, max_segments_per_chunk=1,
        temperature=0, max_retries=6, invalid_json_retries=3, retry_backoff_seconds=10,
        request_timeout_seconds=1, request_interval_seconds=0, cache_enabled=True,
        limit_chunks=None, dry_run=False, resume=False, debug=False,
    )


def test_permanent_error_preserves_completed_chunks_and_stops_requests(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_TRANSCRIBER_HOME", str(tmp_path))
    transcript = TranscriptDocument(
        meta=TranscriptMeta(created_at="2026-10-11", run_id="test", language="en"),
        segments=[TranscriptSegment(id=i, start=i-1, end=i, start_text="", end_text="", text=f"text {i}")
                  for i in range(1, 4)],
    )
    path = tmp_path / "input.json"
    path.write_text(transcript.model_dump_json(), encoding="utf-8")

    class Client:
        calls = 0

        def generate_json_text(self, prompt, **kwargs):
            self.calls += 1
            if self.calls > 1:
                raise PermanentAnalysisError("HTTP 403")
            return json.dumps({"bilingual_lines": [{"segment_id": 1, "translation_zh": "保留译文"}],
                               "chunk_summary_zh": "已有摘要"})

    client = Client()
    monkeypatch.setattr(analyzer, "_make_client", lambda *a: client)
    result = analyzer.run_analysis(options(path))
    document = result["document"]
    assert client.calls == 2  # No third chunk request and no summary request.
    assert document.meta.quality_status == "failed"
    assert document.analysis_error == "HTTP 403"
    assert document.chunks[0].bilingual_lines[0].translation_zh == "保留译文"
    assert len(list((tmp_path / "outputs/cache/analysis").glob("chunk_*.json"))) == 1
    assert (result["output_dir"] / "translation_zh.srt").is_file()
    monkeypatch.setattr(cli, "run_analysis", lambda _: result)
    args = cli.build_parser().parse_args(["analyze", "--input", str(path)])
    assert cli.analyze_task(args)["exit_code"] == 1


def test_permanent_coverage_error_retains_fresh_successful_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_TRANSCRIBER_HOME", str(tmp_path))
    transcript = TranscriptDocument(
        meta=TranscriptMeta(created_at="2026-10-11", run_id="test", language="en"),
        segments=[TranscriptSegment(id=i, start=i-1, end=i, start_text="", end_text="", text=f"text {i}")
                  for i in range(1, 4)],
    )
    path = tmp_path / "input.json"
    path.write_text(transcript.model_dump_json(), encoding="utf-8")

    class Client:
        calls = 0

        def generate_json_text(self, *args, **kwargs):
            self.calls += 1
            if self.calls > 1:
                raise PermanentAnalysisError("HTTP 401")
            return json.dumps({"bilingual_lines": [{"segment_id": 1, "translation_zh": "保留成功行"}]})

    client = Client()
    monkeypatch.setattr(analyzer, "_make_client", lambda *a: client)
    config = options(path)
    config.max_segments_per_chunk = 3
    result = analyzer.run_analysis(config)
    assert client.calls == 2
    assert result["document"].chunks[0].bilingual_lines[0].translation_zh == "保留成功行"
    assert result["document"].meta.fallback_lines == 2
    cached = list((tmp_path / "outputs/cache/analysis").glob("chunk_*.json"))
    assert len(cached) == 1
    assert "保留成功行" in cached[0].read_text(encoding="utf-8")
