from pathlib import Path
from unittest.mock import patch

from app import config as config_module


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
