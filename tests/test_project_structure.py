"""Regression checks for boundaries that affect source and portable execution."""
import json
import os
from pathlib import Path
import subprocess
import sys

from app import config


def test_migration_can_import_without_loading_web_or_jobs(tmp_path):
    env = dict(os.environ, LIVE_TRANSCRIBER_HOME=str(tmp_path))
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import app.migrate_outputs; "
         "assert 'fastapi' not in sys.modules; "
         "assert not any(name.startswith('app.web') for name in sys.modules)"],
        cwd=Path(__file__).resolve().parents[1], env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert not list(tmp_path.iterdir())


def test_bundled_defaults_work_without_root_config_and_are_not_mutated(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LIVE_TRANSCRIBER_HOME", str(tmp_path))
    baseline = json.loads(json.dumps(config.DEFAULT_CONFIG))
    loaded = config.load_config()
    assert loaded == baseline
    assert loaded["analysis"]["local_command"] == "codex"
    assert loaded["analysis"]["local_model"]
    loaded["analysis"]["model"] = "modified-by-a-caller"
    assert config.load_config() == baseline
    (tmp_path / "config.local.yaml").write_text("features:\n  summary: false\n", encoding="utf-8")
    assert config.load_config()["features"]["summary"] is False
    assert config.DEFAULT_CONFIG == baseline
