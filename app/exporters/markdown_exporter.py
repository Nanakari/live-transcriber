from __future__ import annotations

from pathlib import Path

from app.schemas import TranscriptDocument


def export_markdown(document: TranscriptDocument, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    meta = document.meta
    lines = [
        "# 语音转写结果",
        "",
        f"- 标题：{meta.title}",
        f"- 来源：{meta.source_url}",
        f"- 输入文件：{meta.input_file}",
        f"- 指定语言：{meta.language}",
        f"- 检测语言：{meta.detected_language}",
        f"- 模型：{meta.model}",
        f"- quality：{meta.quality}",
        f"- device：{meta.device}",
        f"- compute_type：{meta.compute_type}",
        f"- run_id：{meta.run_id}",
        "",
        "## Transcript",
        "",
    ]
    for segment in document.segments:
        lines.extend([f"### {segment.start_text} - {segment.end_text}", "", segment.text, ""])
    output_path.write_text("\n".join(lines), encoding="utf-8")
