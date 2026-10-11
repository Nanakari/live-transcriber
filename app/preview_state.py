from __future__ import annotations

import hashlib
import json
from pathlib import Path


def preview_identity(subtitle: Path) -> dict[str, str]:
    """Include repairs/metadata as well as translated text in the identity."""
    digest = hashlib.sha256()
    for path in (subtitle, subtitle.parent / "analysis.json"):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        if path.is_file():
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return {"analysis_subtitle": str(subtitle.resolve()), "analysis_fingerprint": digest.hexdigest()}


def preview_is_current(directory: Path, subtitle: Path) -> bool:
    try:
        manifest = json.loads((directory / "assets" / "manifest.json").read_text(encoding="utf-8"))
        identity = preview_identity(subtitle)
        return subtitle.is_file() and all(manifest.get(key) == value for key, value in identity.items())
    except (OSError, ValueError, AttributeError):
        return False


def invalidate_preview(directory: Path) -> None:
    """Do not advertise a partially overwritten package as a completed build."""
    path = directory / "assets" / "manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            manifest = {}
    except (OSError, ValueError):
        manifest = {}
    manifest.pop("analysis_fingerprint", None)
    manifest["state"] = "incomplete"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
