from __future__ import annotations

import re
import os
from pathlib import Path

import pytest


CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
SKILL_WRAPPER = CODEX_HOME / "skills" / "live-transcriber" / "scripts" / "run_live_transcriber.ps1"


@pytest.mark.skipif(not SKILL_WRAPPER.is_file(), reason="installed live-transcriber Skill is not available")
def test_wrapper_dispatches_local_provider_and_default_repair_threshold() -> None:
    script = SKILL_WRAPPER.read_text(encoding="utf-8-sig")

    assert re.search(r"\[double\]\$AutoRepairThreshold\s*=\s*0\.80", script)
    assert "[ValidateRange(0, 1)]" in script
    assert "$arguments += @('--provider', 'local')" in script
    assert "--auto-repair-threshold" in script
    assert "InvariantCulture" in script
    assert "$arguments += @('--local-concurrency', [string]$LocalConcurrency)" in script
