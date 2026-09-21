from __future__ import annotations

from pathlib import Path
import json
import re

from app.utils import format_srt_timestamp

from .schemas import (
    AnalysisDocument,
    ChunkAnalysisResult,
    FixedExpressionItem,
    GrammarItem,
    ReviewItem,
    VocabularyItem,
)
from .repairs import collect_effective_review_items, effective_original, write_repair_log


def export_analysis_json(document: AnalysisDocument, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(document.model_dump_json(indent=2), encoding="utf-8")


def export_bilingual_markdown(document: AnalysisDocument, output_path: Path) -> None:
    _export_natural_translation_document(document, output_path, title="双语自然翻译")


def export_translation_srt(document: AnalysisDocument, output_path: Path) -> None:
    blocks: list[str] = []
    index = 1
    for chunk in document.chunks:
        for line in chunk.bilingual_lines:
            text = line.translation_zh or f"{line.original} [需要复查]"
            blocks.append(f"{index}\n{format_srt_timestamp(line.start)} --> {format_srt_timestamp(line.end)}\n{text}")
            index += 1
    output_path.write_text("\n\n".join(blocks) + ("\n" if blocks else ""), encoding="utf-8")


def export_vocabulary_markdown(document: AnalysisDocument, output_path: Path) -> None:
    lines = ["# 生词表", ""]
    lines.extend(_vocabulary_lines(_collect_vocabulary(document)))
    output_path.write_text("\n".join(lines), encoding="utf-8")


def export_grammar_markdown(document: AnalysisDocument, output_path: Path) -> None:
    grammar_items, expression_items = _collect_grammar_and_expressions(document)
    lines = ["# 语法与固定表达", "", "## 语法", ""]
    lines.extend(_grammar_lines(grammar_items))
    lines.extend(["## 固定表达/口语表达", ""])
    lines.extend(_expression_lines(expression_items))
    output_path.write_text("\n".join(lines), encoding="utf-8")


def export_review_markdown(document: AnalysisDocument, output_path: Path) -> None:
    lines = ["# 人工复查清单", ""]
    lines.extend(_review_lines(document))
    output_path.write_text("\n".join(lines), encoding="utf-8")


def export_repaired_transcript_srt(document: AnalysisDocument, output_path: Path) -> None:
    """Export source-language subtitles using only accepted repairs."""
    lines = [
        line
        for chunk in sorted(document.chunks, key=lambda item: item.start)
        for line in sorted(chunk.bilingual_lines, key=lambda item: item.start)
    ]
    blocks = []
    for index, line in enumerate(lines, start=1):
        text = effective_original(line) or line.original
        blocks.append(
            f"{index}\n{format_srt_timestamp(line.start)} --> {format_srt_timestamp(line.end)}\n{text}"
        )
    output_path.write_text("\n\n".join(blocks) + ("\n" if blocks else ""), encoding="utf-8")


def export_repaired_transcript_markdown(document: AnalysisDocument, output_path: Path) -> None:
    lines = ["# 修复后原文转写", "", "> 原始 ASR 转录保留在 transcripts/ 目录；本文件只采用已通过阈值的自动修复。", ""]
    for chunk in sorted(document.chunks, key=lambda item: item.start):
        for line in sorted(chunk.bilingual_lines, key=lambda item: item.start):
            lines.extend([
                f"## {format_srt_timestamp(line.start)} - {format_srt_timestamp(line.end)}",
                "",
                effective_original(line) or line.original,
                "",
            ])
    output_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def export_combined_study_markdown(document: AnalysisDocument, output_path: Path) -> None:
    vocabulary = _collect_vocabulary(document)
    grammar, expressions = _collect_grammar_and_expressions(document)
    review_lines = _review_lines(document)
    lines = [
        "# 学习资料",
        "",
        "本文件汇总本次分析提取的重点词汇、语法、固定表达和人工复查项。完整逐段原文与中文翻译请参见同目录的 `bilingual.md`。",
        "",
        "## 内容概览",
        "",
        f"- 生词：{len(vocabulary)} 条",
        f"- 语法：{len(grammar)} 条",
        f"- 固定表达：{len(expressions)} 条",
        f"- 人工复查：{sum(1 for line in review_lines if line.startswith('## '))} 条",
        "",
        "## 生词",
        "",
    ]
    lines.extend(_vocabulary_lines(vocabulary))
    lines.extend(["## 语法", ""])
    lines.extend(_grammar_lines(grammar))
    lines.extend(["## 固定表达/口语表达", ""])
    lines.extend(_expression_lines(expressions))
    lines.extend(["## 人工复查清单", ""])
    lines.extend(review_lines)
    output_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _collect_vocabulary(document: AnalysisDocument) -> list[VocabularyItem]:
    seen: set[str] = set()
    items: list[VocabularyItem] = []
    for chunk in document.chunks:
        for item in chunk.vocabulary:
            key = item.word.strip()
            if key and key not in seen:
                seen.add(key)
                items.append(item)
    return items


def _collect_grammar_and_expressions(
    document: AnalysisDocument,
) -> tuple[list[GrammarItem], list[FixedExpressionItem]]:
    grammar_items: list[GrammarItem] = []
    expression_items: list[FixedExpressionItem] = []
    seen_grammar: set[str] = set()
    seen_expr: set[str] = set()
    for chunk in document.chunks:
        for item in chunk.grammar:
            key = item.pattern.strip()
            if key and key not in seen_grammar:
                seen_grammar.add(key)
                grammar_items.append(item)
        for item in chunk.fixed_expressions:
            key = item.expression.strip()
            if key and key not in seen_expr:
                seen_expr.add(key)
                expression_items.append(item)
    return grammar_items, expression_items


def _collect_review_items(document: AnalysisDocument) -> list[tuple[str, ReviewItem]]:
    return collect_effective_review_items(document)


def _vocabulary_lines(items: list[VocabularyItem]) -> list[str]:
    if not items:
        return ["- 本次未提取到可靠的重点词汇。", ""]
    lines: list[str] = []
    for item in items:
        lines.extend(
            [
                f"### {item.word}",
                "",
                f"- 读音：{item.reading or '未提供'}",
                f"- 意思：{item.meaning_zh}",
                f"- 词性：{item.part_of_speech or '未提供'}",
                f"- 等级：{item.level}",
                f"- 例句：{item.example_original or '未提供'}",
                f"- 例句译文：{item.example_zh or '未提供'}",
                "",
            ]
        )
    return lines


def _grammar_lines(items: list[GrammarItem]) -> list[str]:
    if not items:
        return ["- 本次未提取到可靠的重点语法。", ""]
    lines: list[str] = []
    for item in items:
        lines.extend(
            [
                f"### {item.pattern}",
                "",
                f"- 说明：{item.explanation_zh}",
                f"- 重要度：{item.importance}",
                f"- 例句：{item.example_original or '未提供'}",
                f"- 例句译文：{item.example_zh or '未提供'}",
                "",
            ]
        )
    return lines


def _expression_lines(items: list[FixedExpressionItem]) -> list[str]:
    if not items:
        return ["- 本次未提取到可靠的固定或口语表达。", ""]
    lines: list[str] = []
    for item in items:
        lines.extend(
            [
                f"### {item.expression}",
                "",
                f"- 意思：{item.meaning_zh}",
                f"- 用法：{item.usage_note_zh or '未提供'}",
                f"- 例句：{item.example_original or '未提供'}",
                f"- 例句译文：{item.example_zh or '未提供'}",
                "",
            ]
        )
    return lines


def _review_lines(document: AnalysisDocument) -> list[str]:
    lines: list[str] = []
    for chunk_id, item in _collect_review_items(document):
        time_text = ""
        if item.start is not None and item.end is not None:
            time_text = f"{format_srt_timestamp(item.start)} - {format_srt_timestamp(item.end)}"
        lines.extend(
            [
                f"## {chunk_id} {time_text}".rstrip(),
                "",
                f"- 原文（ASR）：{item.original or '未提供'}",
                *(
                    [
                        f"- 修复候选：{item.corrected_original}",
                        f"- 修复置信度：{item.repair_confidence:.2f}",
                        f"- 修复状态：{'已自动采用' if item.auto_repaired else '待人工确认'}",
                        f"- 修复依据：{item.repair_reason}",
                    ]
                    if item.corrected_original.strip()
                    else []
                ),
                f"- 原因：{item.reason_zh}",
                f"- 类型：{item.risk_type or '未分类'}",
                "",
            ]
        )
    for failed in document.failed_chunks:
        lines.extend([f"## {failed.chunk_id}", "", f"- 失败：{failed.error}", f"- 原始响应：{failed.raw_response_file}", ""])
    if not lines:
        lines.extend(["- 本次未发现需要人工复查的可靠风险。", ""])
    return lines


def export_video_summary_markdown(document: AnalysisDocument, output_path: Path) -> None:
    lines = ["# 视频总结", ""]
    if document.meta.failed_chunks or document.meta.fallback_lines:
        lines.extend(["> 部分内容分析失败，本总结可能遗漏相关时段。", ""])
    if document.video_summary:
        lines.extend(["## 核心要点", ""])
        chunks = {chunk.chunk_id: chunk for chunk in document.chunks}
        for point in document.video_summary.points:
            sources = [chunks[key] for key in dict.fromkeys(point.source_chunk_ids) if key in chunks]
            times = "；".join(f"{format_srt_timestamp(chunk.start)}–{format_srt_timestamp(chunk.end)}" for chunk in sources)
            lines.append(f"- {point.text}" + (f"（{times}）" if times else ""))
    else:
        lines.extend(["> 尚未生成全片总结，以下保留全部可用分段摘要。", "", "## 分段摘要", ""])
        for chunk in sorted(document.chunks, key=lambda item: item.start):
            lines.extend([f"### {format_srt_timestamp(chunk.start)} - {format_srt_timestamp(chunk.end)}", ""])
            if chunk.chunk_summary_zh.strip():
                lines.extend([chunk.chunk_summary_zh.strip(), ""])
            points = _unique_text(chunk.key_points_zh)
            lines.extend(f"- {point}" for point in points)
            if not chunk.chunk_summary_zh.strip() and not points:
                lines.append("- 此分段暂无可用摘要。")
            lines.append("")
    output_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _export_natural_translation_document(
    document: AnalysisDocument,
    output_path: Path,
    *,
    title: str,
) -> None:
    lines = [f"# {title}", ""]
    for start, end, original, translation in _reading_paragraphs(document):
        lines.extend(
            [
                f"## {format_srt_timestamp(start)} - {format_srt_timestamp(end)}",
                "",
                "**原文**",
                "",
                original,
                "",
                "**自然翻译**",
                "",
                translation,
                "",
            ]
        )
    output_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _reading_paragraphs(document: AnalysisDocument) -> list[tuple[float, float, str, str]]:
    source = [
        line
        for chunk in sorted(document.chunks, key=lambda item: item.start)
        for line in sorted(chunk.bilingual_lines, key=lambda item: item.start)
        if line.original.strip() or line.translation_zh.strip()
    ]
    paragraphs: list[tuple[float, float, str, str]] = []
    current = []

    def flush() -> None:
        if not current:
            return
        paragraphs.append(
            (
                current[0].start,
                current[-1].end,
                _join_spoken_text(effective_original(line) for line in current),
                _join_spoken_text(line.translation_zh for line in current),
            )
        )
        current.clear()

    for line in source:
        if current and line.start - current[-1].end > 12:
            flush()
        current.append(line)
        translated = _join_spoken_text(item.translation_zh for item in current)
        span = current[-1].end - current[0].start
        sentence_end = bool(re.search(r"[。！？!?…][”’\"']?$", translated))
        if len(translated) >= 320 or len(current) >= 20 or span >= 90 or (len(translated) >= 170 and sentence_end):
            flush()
    flush()
    return paragraphs


def _join_spoken_text(values) -> str:
    result = ""
    for value in values:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if not text:
            continue
        if result and result[-1].isascii() and result[-1].isalnum() and text[0].isascii() and text[0].isalnum():
            result += " "
        result += text
    return result


PROFILE_CATEGORY_TITLES = {
    "personality": "性格表现",
    "preference": "喜好与兴趣",
    "value": "价值取向与动机",
    "habit": "习惯与行为模式",
    "communication": "表达与沟通风格",
    "social": "社交与互动方式",
    "identity": "身份与背景线索",
}


def build_character_profile(document: AnalysisDocument) -> dict:
    chunks = sorted(document.chunks, key=lambda item: item.start)
    duration = max((chunk.end for chunk in chunks), default=0.0)
    people: dict[str, list[dict]] = {}
    seen: set[tuple[str, str, str]] = set()
    for chunk in chunks:
        for item in chunk.profile_observations:
            label = item.speaker_label.strip() or "主要说话人"
            category = item.category.strip().lower()
            if category not in PROFILE_CATEGORY_TITLES:
                continue
            observation = item.observation_zh.strip()
            evidence = item.evidence_zh.strip()
            key = (label, category, re.sub(r"[\W_]+", "", observation).lower())
            if not observation or not evidence or not key[2] or key in seen:
                continue
            seen.add(key)
            people.setdefault(label, []).append({
                "category": category,
                "observation_zh": observation,
                "evidence_zh": evidence,
                "confidence": max(0.0, min(1.0, float(item.confidence))),
                "start": chunk.start,
                "end": chunk.end,
            })
    return {
        "meta": {
            "version": "character-profile-v1",
            "analysis_run_id": document.meta.run_id,
            "transcript_run_id": document.meta.transcript_run_id,
            "input_file": document.meta.input_file,
            "provider": document.meta.provider,
            "model": document.meta.model,
            "analysis_profile": document.meta.profile,
            "source_language": document.meta.source_language,
            "target_language": document.meta.target_language,
            "duration_seconds": duration,
            "analyzed_chunks": document.meta.succeeded_chunks,
            "total_chunks": document.meta.total_chunks,
            "failed_chunks": document.meta.failed_chunks,
        },
        "people": people,
    }


def export_character_profile_markdown(document: AnalysisDocument, output_path: Path) -> None:
    payload = build_character_profile(document)
    meta = payload["meta"]
    lines = [
        "# 人物 Profile",
        "",
        f"- 输入文件：{meta['input_file']}",
        f"- 源语言：{meta['source_language'] or '自动识别'}",
        f"- 内容时长：{format_srt_timestamp(meta['duration_seconds'])}",
        f"- 分析覆盖：{meta['analyzed_chunks']}/{meta['total_chunks']} 个分段",
        "",
        "> 本档案只整理音频中有直接证据或可谨慎推断的人物特征，不代表心理诊断，也不推断敏感身份属性。",
        "",
    ]
    if payload["people"]:
        for label, observations in payload["people"].items():
            lines.extend([f"## {label}", "", "### 人物概览", ""])
            overview = sorted(observations, key=lambda item: (-item["confidence"], item["start"]))[:6]
            lines.extend(f"- {item['observation_zh']}" for item in overview)
            lines.append("")
            for category, title in PROFILE_CATEGORY_TITLES.items():
                items = [item for item in observations if item["category"] == category]
                if not items:
                    continue
                lines.extend([f"### {title}", ""])
                for item in sorted(items, key=lambda value: (-value["confidence"], value["start"])):
                    confidence = "高" if item["confidence"] >= 0.8 else "中" if item["confidence"] >= 0.55 else "低"
                    time_range = f"{format_srt_timestamp(item['start'])} - {format_srt_timestamp(item['end'])}"
                    lines.extend([
                        f"- **{item['observation_zh']}**（置信度：{confidence}）",
                        f"  - 依据：{item['evidence_zh']}",
                        f"  - 时间：{time_range}",
                    ])
                lines.append("")
    else:
        lines.extend(["## 暂无可靠人物侧写", "", "音频中没有提取到足够明确、带证据的人物特征。", ""])
    if meta["failed_chunks"]:
        lines.extend(["> 注意：部分分段分析失败，本人物档案可能遗漏相应时段。", ""])
    output_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _unique_text(values) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = re.sub(r"[\W_]+", "", text).lower()
        if text and key and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _append_profile_list(lines: list[str], title: str, values: list[str]) -> None:
    lines.extend([f"## {title}", ""])
    lines.extend(f"- {value}" for value in values)
    if not values:
        lines.append("- 暂无可靠结论。")
    lines.append("")


def export_all(
    document: AnalysisDocument,
    output_dir: Path,
) -> dict[str, Path]:
    paths = {
        "analysis_json": output_dir / "analysis.json",
        "bilingual_md": output_dir / "bilingual.md",
        "translation_srt": output_dir / "translation_zh.srt",
        "review_md": output_dir / "review.md",
        "repaired_transcript_srt": output_dir / "repaired_transcript.srt",
        "repaired_transcript_md": output_dir / "repaired_transcript.md",
        "repair_log": output_dir / "repair_log.json",
    }
    export_analysis_json(document, paths["analysis_json"])
    export_bilingual_markdown(document, paths["bilingual_md"])
    export_translation_srt(document, paths["translation_srt"])
    export_review_markdown(document, paths["review_md"])
    export_repaired_transcript_srt(document, paths["repaired_transcript_srt"])
    export_repaired_transcript_markdown(document, paths["repaired_transcript_md"])
    write_repair_log(document, paths["repair_log"])
    if document.meta.study_notes:
        for key, name, exporter in (
            ("vocabulary_md", "vocabulary.md", export_vocabulary_markdown),
            ("grammar_md", "grammar.md", export_grammar_markdown),
            ("study_notes_md", "study_notes.md", export_combined_study_markdown),
        ):
            paths[key] = output_dir / name
            exporter(document, paths[key])
    if document.meta.summary:
        paths["video_summary_md"] = output_dir / "video_summary.md"
        export_video_summary_markdown(document, paths["video_summary_md"])
    if document.meta.character_profile:
        paths["character_profile_md"] = output_dir / "character_profile.md"
        export_character_profile_markdown(document, paths["character_profile_md"])
    return paths
