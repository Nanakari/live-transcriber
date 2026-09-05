from __future__ import annotations

from pathlib import Path
import json
import re

from app.utils import format_srt_timestamp

from .schemas import AnalysisDocument, ChunkAnalysisResult, VocabularyItem, GrammarItem, FixedExpressionItem


def export_analysis_json(document: AnalysisDocument, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(document.model_dump_json(indent=2), encoding="utf-8")


def export_bilingual_markdown(document: AnalysisDocument, output_path: Path) -> None:
    lines = [
        "# 双语转写与学习笔记",
        "",
        f"- 输入文件：{document.meta.input_file}",
        f"- Provider：{document.meta.provider}",
        f"- Model：{document.meta.model}",
        f"- Profile：{document.meta.profile}",
        f"- Chunks：{document.meta.succeeded_chunks}/{document.meta.total_chunks}",
        "",
    ]
    if document.meta.fallback_lines:
        lines.extend(
            [
                f"> 注意：有 {document.meta.fallback_lines} 条字幕在自动补译后仍未获得中文翻译，"
                "已保留原文并标记为“翻译暂缺”，时间轴没有留空。",
                "",
            ]
        )
    for chunk in document.chunks:
        lines.extend([f"## {chunk.chunk_id} {format_srt_timestamp(chunk.start)} - {format_srt_timestamp(chunk.end)}", ""])
        if chunk.chunk_summary_zh:
            lines.extend([f"**概括：** {chunk.chunk_summary_zh}", ""])
        for item in chunk.bilingual_lines:
            lines.extend(
                [
                    f"### {format_srt_timestamp(item.start)} - {format_srt_timestamp(item.end)}",
                    "",
                    f"原文：{item.original}",
                    "",
                    f"自然译：{item.translation_zh}",
                    "",
                    f"直译：{item.literal_zh}",
                    "",
                ]
            )
            if item.brief_note:
                lines.extend([f"说明：{item.brief_note}", ""])
            if item.asr_suspect:
                lines.extend([f"ASR 复查：{item.asr_issue or '疑似 ASR 错误'}", ""])
        if chunk.vocabulary:
            lines.extend(["**词汇：**", ""])
            for vocab in chunk.vocabulary:
                lines.append(f"- {vocab.word}（{vocab.reading}）：{vocab.meaning_zh}")
            lines.append("")
        if chunk.grammar or chunk.fixed_expressions:
            lines.extend(["**语法/表达：**", ""])
            for grammar in chunk.grammar:
                lines.append(f"- {grammar.pattern}：{grammar.explanation_zh}")
            for expr in chunk.fixed_expressions:
                lines.append(f"- {expr.expression}：{expr.meaning_zh}")
            lines.append("")
    output_path.write_text("\n".join(lines), encoding="utf-8")


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
    seen: set[str] = set()
    items: list[VocabularyItem] = []
    for chunk in document.chunks:
        for item in chunk.vocabulary:
            key = item.word.strip()
            if key and key not in seen:
                seen.add(key)
                items.append(item)
    lines = ["# 生词表", ""]
    for item in items:
        lines.extend(
            [
                f"## {item.word}",
                "",
                f"- 读音：{item.reading}",
                f"- 意思：{item.meaning_zh}",
                f"- 词性：{item.part_of_speech}",
                f"- 等级：{item.level}",
                f"- 例句：{item.example_original}",
                f"- 例句译文：{item.example_zh}",
                "",
            ]
        )
    output_path.write_text("\n".join(lines), encoding="utf-8")


def export_grammar_markdown(document: AnalysisDocument, output_path: Path) -> None:
    grammar_items: list[GrammarItem] = []
    expression_items: list[FixedExpressionItem] = []
    seen_grammar: set[str] = set()
    seen_expr: set[str] = set()
    for chunk in document.chunks:
        for item in chunk.grammar:
            if item.pattern not in seen_grammar:
                seen_grammar.add(item.pattern)
                grammar_items.append(item)
        for item in chunk.fixed_expressions:
            if item.expression not in seen_expr:
                seen_expr.add(item.expression)
                expression_items.append(item)

    lines = ["# 语法与固定表达", "", "## 语法", ""]
    for item in grammar_items:
        lines.extend(
            [
                f"### {item.pattern}",
                "",
                f"- 说明：{item.explanation_zh}",
                f"- 重要度：{item.importance}",
                f"- 例句：{item.example_original}",
                f"- 例句译文：{item.example_zh}",
                "",
            ]
        )
    lines.extend(["## 固定表达/口语表达", ""])
    for item in expression_items:
        lines.extend(
            [
                f"### {item.expression}",
                "",
                f"- 意思：{item.meaning_zh}",
                f"- 用法：{item.usage_note_zh}",
                f"- 例句：{item.example_original}",
                f"- 例句译文：{item.example_zh}",
                "",
            ]
        )
    output_path.write_text("\n".join(lines), encoding="utf-8")


def export_review_markdown(document: AnalysisDocument, output_path: Path) -> None:
    lines = ["# 人工复查清单", ""]
    for chunk in document.chunks:
        for item in chunk.review_items:
            time_text = ""
            if item.start is not None and item.end is not None:
                time_text = f"{format_srt_timestamp(item.start)} - {format_srt_timestamp(item.end)}"
            lines.extend(
                [
                    f"## {chunk.chunk_id} {time_text}",
                    "",
                    f"- 原文：{item.original}",
                    f"- 原因：{item.reason_zh}",
                    f"- 类型：{item.risk_type}",
                    "",
                ]
            )
    for failed in document.failed_chunks:
        lines.extend([f"## {failed.chunk_id}", "", f"- 失败：{failed.error}", f"- 原始响应：{failed.raw_response_file}", ""])
    output_path.write_text("\n".join(lines), encoding="utf-8")


def export_combined_study_markdown(document: AnalysisDocument, output_path: Path) -> None:
    lines = [
        "# 完整学习笔记",
        "",
        f"- 输入文件：{document.meta.input_file}",
        f"- Model：{document.meta.model}",
        f"- Chunks：{document.meta.succeeded_chunks}/{document.meta.total_chunks}",
        "",
    ]
    for index, chunk in enumerate(document.chunks, start=1):
        lines.extend(
            [
                f"## 第 {index} 段 {format_srt_timestamp(chunk.start)} - {format_srt_timestamp(chunk.end)}",
                "",
            ]
        )
        if chunk.chunk_summary_zh:
            lines.extend([f"**段落概括：** {chunk.chunk_summary_zh}", ""])

        for item in chunk.bilingual_lines:
            if not item.original.strip() and not item.translation_zh.strip():
                continue
            lines.extend(
                [
                    f"### {format_srt_timestamp(item.start)} - {format_srt_timestamp(item.end)}",
                    "",
                    f"原文：{item.original}",
                    "",
                    f"中文：{item.translation_zh or '[翻译暂缺]'}",
                    "",
                ]
            )

        if chunk.vocabulary:
            lines.extend(["### 词汇", ""])
            for item in chunk.vocabulary:
                lines.append(
                    f"- {item.word}"
                    f"{f'（{item.reading}）' if item.reading else ''}：{item.meaning_zh}"
                    f"。例：{item.example_original}"
                )
            lines.append("")

        grammar_lines: list[str] = []
        for item in chunk.grammar:
            grammar_lines.append(f"- {item.pattern}：{item.explanation_zh}。例：{item.example_original}")
        for item in chunk.fixed_expressions:
            grammar_lines.append(f"- {item.expression}：{item.meaning_zh}。{item.usage_note_zh}")
        if grammar_lines:
            lines.extend(["### 语法与固定表达", "", *grammar_lines, ""])

        if chunk.review_items:
            lines.extend(["### 复查清单", ""])
            for item in chunk.review_items:
                time_text = ""
                if item.start is not None and item.end is not None:
                    time_text = f"{format_srt_timestamp(item.start)} - {format_srt_timestamp(item.end)} "
                lines.append(f"- {time_text}{item.original}：{item.reason_zh}")
            lines.append("")

    if document.failed_chunks:
        lines.extend(["## 失败 chunk", ""])
        for failed in document.failed_chunks:
            lines.extend([f"- {failed.chunk_id}：{failed.error}", ""])

    output_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def export_video_summary_markdown(document: AnalysisDocument, output_path: Path) -> None:
    important_chunks = [
        chunk for chunk in document.chunks
        if chunk.content_importance >= 3.5 and any(point.strip() for point in chunk.key_points_zh)
    ]
    highlights = sorted(
        sorted(important_chunks, key=lambda chunk: (-chunk.content_importance, chunk.start))[:12],
        key=lambda chunk: chunk.start,
    )
    full_summary_parts = _unique_text(
        re.sub(r"\s+", " ", chunk.chunk_summary_zh).strip()
        for chunk in sorted(document.chunks, key=lambda item: item.start)
        if chunk.chunk_summary_zh.strip()
    )
    full_summary = " ".join(full_summary_parts)

    duration = max((chunk.end for chunk in document.chunks), default=0.0)
    lines = [
        "# 视频内容总结",
        "",
        f"- 视频时长：{format_srt_timestamp(duration)}",
        f"- 分析模型：{document.meta.model}",
        f"- 有效分段：{document.meta.succeeded_chunks}/{document.meta.total_chunks}",
        "",
        "## 要点总结",
        "",
    ]
    if highlights:
        for index, chunk in enumerate(highlights, start=1):
            lines.extend([
                f"### {index}. {format_srt_timestamp(chunk.start)} - {format_srt_timestamp(chunk.end)}",
                "",
            ])
            details = [point.strip() for point in chunk.key_points_zh if point.strip()]
            for point in details:
                lines.append(f"- {point}")
            if details:
                lines.append("")
    else:
        lines.append("- 本段影音中没有提取到足够明确的重要通知、告知、决定或关键内容。")

    lines.extend(["", "## 全文概括", ""])
    lines.append(full_summary or "暂未生成可靠的全文概括。")

    if document.failed_chunks:
        lines.extend([
            "",
            "> 部分片段分析失败，本总结可能遗漏对应时段的内容。",
        ])
    output_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


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


def export_all(document: AnalysisDocument, output_dir: Path) -> dict[str, Path]:
    paths = {
        "analysis_json": output_dir / "analysis.json",
        "bilingual_md": output_dir / "bilingual.md",
        "translation_srt": output_dir / "translation_zh.srt",
        "review_md": output_dir / "review.md",
    }
    export_analysis_json(document, paths["analysis_json"])
    export_bilingual_markdown(document, paths["bilingual_md"])
    export_translation_srt(document, paths["translation_srt"])
    export_review_markdown(document, paths["review_md"])
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
