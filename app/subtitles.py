"""Readable display cues, separate from the fine-grained transcription."""
from __future__ import annotations

import re
from pathlib import Path

from .utils import format_srt_timestamp


def _join(parts: list[str]) -> str:
    return " ".join(part.strip() for part in parts if part.strip())


def group_display_cues(cues: list[dict]) -> list[dict]:
    groups: list[dict] = []
    current: list[dict] = []

    def flush():
        if current:
            groups.append({"start": current[0]["start"], "end": current[-1]["end"],
                           "original": _join([c["original"] for c in current]),
                           "translation": _join([c["translation"] for c in current])})
            current.clear()

    for cue in cues:
        if cue["end"] <= cue["start"]:
            continue
        if current:
            span = cue["end"] - current[0]["start"]
            gap = cue["start"] - current[-1]["end"]
            too_long = (len(_join([c["original"] for c in current] + [cue["original"]])) > 100
                        or len(_join([c["translation"] for c in current] + [cue["translation"]])) > 70)
            if gap > 0.8 or gap < -0.01 or span > 9 or too_long:
                flush()
        current.append(cue)
        span = current[-1]["end"] - current[0]["start"]
        if span >= 6 and re.search(r"[。！？!?]$", cue["translation"].strip()):
            flush()
    flush()
    return groups


def write_display_subtitles(cues: list[dict], srt: Path, ass: Path) -> None:
    groups = group_display_cues(cues)
    srt.write_text("\n\n".join(
        f"{index}\n{format_srt_timestamp(c['start'])} --> {format_srt_timestamp(c['end'])}\n"
        + "\n".join(t for t in (c["original"], c["translation"]) if t)
        for index, c in enumerate(groups, 1)
    ) + "\n", encoding="utf-8")
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Microsoft YaHei,28,&H00FFFFFF,&H00FFFFFF,&HA0000000,&HA0000000,0,0,0,0,100,100,0,0,3,2,0,2,60,60,30,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    def timestamp(seconds):
        centis = round(seconds * 100)
        return f"{centis // 360000}:{centis // 6000 % 60:02}:{centis // 100 % 60:02}.{centis % 100:02}"

    def escape(text):
        # Prevent transcript text from being interpreted as ASS style overrides.
        return text.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\n", " ")

    ass.write_text(header + "\n".join(
        f"Dialogue: 0,{timestamp(c['start'])},{timestamp(c['end'])},Default,,0,0,0,,"
        + r"\N".join(escape(t) for t in (c["original"], c["translation"]) if t)
        for c in groups
    ) + "\n", encoding="utf-8")
