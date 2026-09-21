from pathlib import Path
from unittest.mock import patch

from app import config as config_module
from app import cli as cli_module


def test_load_config_merges_untracked_local_overrides(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(
        "network:\n  proxy: ''\nanalysis:\n  target_language: zh\n",
        encoding="utf-8",
    )
    (tmp_path / "config.local.yaml").write_text(
        "network:\n  proxy: http://127.0.0.1:7897\n",
        encoding="utf-8",
    )

    with patch.object(config_module, "project_root", return_value=tmp_path):
        loaded = config_module.load_config()

    assert loaded["network"]["proxy"] == "http://127.0.0.1:7897"
    assert loaded["analysis"]["target_language"] == "zh"


def test_default_proxy_is_portable(tmp_path: Path) -> None:
    with patch.object(config_module, "project_root", return_value=tmp_path):
        loaded = config_module.load_config()

    assert loaded["network"]["proxy"] == ""


def test_local_analysis_defaults_and_repair_threshold_are_bounded(tmp_path: Path) -> None:
    with patch.object(config_module, "project_root", return_value=tmp_path):
        loaded = config_module.load_config()

    assert loaded["analysis"]["local_timeout_seconds"] == 360
    assert loaded["analysis"]["auto_repair_enabled"] is True
    assert loaded["analysis"]["auto_repair_threshold"] == 0.80


def test_cli_local_defaults_do_not_change_gemini_timeout(monkeypatch, tmp_path: Path) -> None:
    config = config_module.load_config()
    config["analysis"]["provider"] = "local"
    config["analysis"].pop("local_timeout_seconds", None)
    config["analysis"].pop("auto_repair_threshold", None)
    monkeypatch.setattr(cli_module, "load_config", lambda: config)
    monkeypatch.setenv("LIVE_TRANSCRIBER_SKILL_MODE", "1")

    captured = {}

    def fake_run_analysis(options):
        captured["options"] = options
        return {
            "dry_run": True,
            "run_id": "offline-contract",
            "output_dir": tmp_path,
            "dry_run_file": tmp_path / "chunks.json",
        }

    monkeypatch.setattr(cli_module, "run_analysis", fake_run_analysis)
    args = cli_module.build_parser().parse_args(["analyze", "--input", str(tmp_path / "transcript.json")])

    cli_module.analyze_task(args)

    options = captured["options"]
    assert options.local_timeout_seconds == 360
    assert options.request_timeout_seconds == 300
    assert options.auto_repair_threshold == 0.80
