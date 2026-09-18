from app.subtitles import group_display_cues, write_display_subtitles
from app import cli, config
from app.web.server import create_app
from fastapi.testclient import TestClient


def cue(start, end, text):
    return {"start": start, "end": end, "original": text, "translation": text}


def test_grouping_preserves_text_and_range_and_breaks_at_silence():
    source = [cue(0, 2, "one"), cue(2, 4, "two"), cue(4, 6, "three"),
              cue(6, 8, "four"), cue(11, 13, "five")]
    grouped = group_display_cues(source)
    assert [(c["start"], c["end"]) for c in grouped] == [(0, 8), (11, 13)]
    assert " ".join(c["original"] for c in grouped) == "one two three four five"
    assert " ".join(c["translation"] for c in grouped) == "one two three four five"


def test_grouping_respects_text_length_and_sentence_boundary():
    assert len(group_display_cues([cue(0, 4, "字" * 40), cue(4, 8, "词" * 40)])) == 2
    grouped = group_display_cues([cue(0, 3, "句子"), cue(3, 6, "结束。"), cue(6, 8, "下一句")])
    assert grouped[0]["end"] == 6


def test_ass_uses_readable_style_and_escapes_override_commands(tmp_path):
    srt = tmp_path / "display.srt"
    ass = tmp_path / "display.ass"
    write_display_subtitles([cue(0.34, 5.98, r"{\an8}text")], srt, ass)
    text = ass.read_text(encoding="utf-8")
    assert "Microsoft YaHei,28" in text
    assert "&H00141414" in text
    assert "&HFF000000" in text
    assert r"{\c&H00FFFFFF&}" in text
    assert r"{\c&H004DD8FF&}" in text
    assert "0:00:00.34,0:00:05.98" in text
    assert r"{\an8}" not in text
    assert "00:00:00,340 --> 00:00:05,980" in srt.read_text(encoding="utf-8")


def test_default_transcription_uses_large_v3_turbo(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_TRANSCRIBER_HOME", str(tmp_path))
    args = cli.build_parser().parse_args(["transcribe", "--input", "sample.wav"])
    options = cli._resolve_options(args, config.load_config())
    assert options["model"] == "large-v3-turbo"


def test_new_video_directory_is_listed_and_default_player_route_is_used(tmp_path, monkeypatch):
    from app.web import routes
    from app.output_layout import media_group_dir
    monkeypatch.setenv("LIVE_TRANSCRIBER_HOME", str(tmp_path))
    group = media_group_dir("sample")
    video = group / "video"
    subtitles = video / "subtitles"
    subtitles.mkdir(parents=True)
    (video / "live_preview.mp4").write_bytes(b"video")
    (subtitles / "display.bilingual.srt").write_text("subtitle", encoding="utf-8")
    result = routes._latest_preview(tmp_path / "outputs" / "media")
    assert result["files"]["display.bilingual.srt"]["path"] == str((subtitles / "display.bilingual.srt").resolve())
    assets = video / "assets"
    assets.mkdir()
    (assets / "manifest.json").write_text('{"transcript_run_id":"sample"}', encoding="utf-8")
    assert routes._preview_transcript_run_id(video, {"sample": {}}) == "sample"
    opened = []
    monkeypatch.setattr(routes, "open_file", lambda path: opened.append(path) or {"path": path})
    with TestClient(create_app()) as client:
        response = client.post("/api/open-file", json={"path": str(video / "live_preview.mp4")})
        assert response.status_code == 200 and opened
