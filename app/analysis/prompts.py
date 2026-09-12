from __future__ import annotations

import json
from copy import deepcopy

from .chunker import chunk_to_prompt_payload
from .schemas import AnalysisChunk, AnalysisSegment


PROMPT_VERSION = "multilingual-study-v9-structured-study-notes"

SYSTEM_PROMPT = """你是一个多语言影音文本的中文自然翻译与内容提炼助手。
你的主任务是生成按时间对齐的自然中文翻译，并提取少量真正重要的视频要点。
翻译要结合相邻句子的语境自然表达，不要直译腔，不要添加翻译说明、语法说明或括号注释。
源语言可能是任意语言或多语言混用。不要编造上下文；专有名词不确定时采用保守表达。
必须返回合法 JSON，不要返回 Markdown，不要使用代码块。"""


JSON_SCHEMA_HINT = {
    "chunk_id": "chunk_0000",
    "start": 0.0,
    "end": 120.0,
    "chunk_summary_zh": "中文概括",
    "key_points_zh": ["本段值得记住的内容要点"],
    "content_importance": 3.0,
    "bilingual_lines": [
        {
            "segment_id": 1,
            "translation_zh": "结合上下文得到的自然中文翻译",
        }
    ],
    "vocabulary": [
        {
            "word": "值得学习的源语言词语",
            "reading": "读音或假名；不确定时留空",
            "meaning_zh": "中文释义",
            "part_of_speech": "词性",
            "example_original": "来自本段的例句",
            "example_zh": "例句译文",
            "level": "intermediate",
        }
    ],
    "grammar": [
        {
            "pattern": "本段值得学习的语法句型",
            "explanation_zh": "中文说明",
            "example_original": "来自本段的例句",
            "example_zh": "例句译文",
            "importance": "medium",
        }
    ],
    "fixed_expressions": [
        {
            "expression": "本段值得学习的固定表达或口语表达",
            "meaning_zh": "中文含义",
            "usage_note_zh": "使用说明",
            "example_original": "来自本段的例句",
            "example_zh": "例句译文",
        }
    ],
    "review_items": [
        {
            "segment_id": 1,
            "reason_zh": "需要人工复核的原因",
            "risk_type": "ASR/专有名词/歧义/翻译",
        }
    ],
    "profile_observations": [
        {
            "speaker_label": "主要说话人；能可靠区分时可写人物称呼",
            "category": "personality/preference/value/habit/communication/social/identity",
            "observation_zh": "对人物性格、喜好、价值取向、习惯或表达方式的具体观察",
            "evidence_zh": "支持该观察的原文事实或行为；注明是直接表达还是谨慎推断",
            "confidence": 0.8,
        }
    ],
}


def _build_full_chunk_prompt(
    chunk: AnalysisChunk,
    *,
    profile: str,
    source_language: str,
    target_language: str,
) -> str:
    payload = chunk_to_prompt_payload(chunk)
    return f"""请分析下面这个影音转写 chunk。

profile: {profile}
source_language: {source_language}
target_language: {target_language}

要求：
1. bilingual_lines 必须逐一覆盖输入的每个 segment_id，顺序和数量完全一致；每项只返回 segment_id 和 translation_zh。
2. translation_zh 必须联系前后文进行自然意译，读起来像正常中文；不要逐词硬译，不要添加直译、说明、点评、读音或括号注释。
3. 输入可能被 ASR 切得很碎。每个 segment 仍需单独返回，但翻译措辞要与相邻 segment 连贯，不能把句子碎片机械翻译成生硬中文。
4. chunk_summary_zh 只用一句话客观概括本段主题，不逐句复述。
5. key_points_zh 最多 2 条，只保留对理解整个视频有帮助的主题、事实、观点、经历、结论或后续计划；寒暄、重复和无信息量过场返回空列表。
6. content_importance 使用 0-5 分，衡量本段对理解整个视频主题的重要性，而不只衡量是否为正式通知。
7. 只返回合法 JSON，字段结构必须匹配下面示例，不要输出示例以外的字段。
8. profile_observations 只记录有原文证据的人物特点，每段最多 4 条；没有可靠证据时返回空列表。
9. profile_observations 的 category 只能使用 personality / preference / value / habit / communication / social / identity，confidence 为 0-1。
10. 不得推断敏感属性或做心理诊断；无法可靠区分多人时统一写“主要说话人”。
11. vocabulary 只提取本段中真正值得学习的词语，每段最多 8 个；不要为了凑数罗列普通寒暄、重复词或不确定的 ASR 片段；没有合适内容时返回空列表。
12. grammar 和 fixed_expressions 只提取本段中有代表性的语法句型或固定/口语表达，每类最多 4 个；必须给出本段原文例句和中文说明；没有合适内容时返回空列表。
13. vocabulary 的 word、grammar 的 pattern、fixed_expressions 的 expression 必须保留源语言形式；专有名词只有在确实有学习价值时才列出。
14. review_items 只记录可能存在 ASR 错误、专有名词不确定、语义歧义或翻译风险的片段，每段最多 8 条；segment_id 必须来自输入，reason_zh 要具体；没有可靠风险时返回空列表。
15. 学习资料宁缺毋滥，不要编造词义、语法或复查问题；所有学习资料字段都必须是数组。

JSON 结构示例：
{json.dumps(JSON_SCHEMA_HINT, ensure_ascii=False, indent=2)}

输入 chunk：
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""


def build_repair_prompt(raw_text: str) -> str:
    return f"""下面的模型输出不是合法 JSON，或字段结构不符合要求。请修复语法与字段结构，保留原内容含义，返回一个合法 JSON 对象。
不要添加 Markdown，不要使用代码块，不要解释。

原始输出：
{raw_text}
"""


def build_missing_lines_prompt(
    chunk: AnalysisChunk,
    segments: list[AnalysisSegment],
    *,
    source_language: str,
    target_language: str,
) -> str:
    payload = {
        "chunk_id": chunk.chunk_id,
        "start": chunk.start,
        "end": chunk.end,
        "segments": [segment.model_dump() for segment in segments],
    }
    return f"""上一次翻译遗漏了部分 segments。请只补译下面列出的所有片段。

source_language: {source_language}
target_language: {target_language}

要求：
1. bilingual_lines 必须逐一覆盖输入的每个 segment_id，数量必须完全一致。
2. 不得合并、跳过、概括或新增片段。
3. segment_id/start/end/original 必须原样返回。
4. translation_zh 必须结合相邻片段语境使用自然中文，不要直译，不要添加说明。
5. 只返回合法 JSON 对象，不要返回 Markdown。

返回结构：
{{
  "chunk_id": "{chunk.chunk_id}",
  "start": {chunk.start},
  "end": {chunk.end},
  "bilingual_lines": [
    {{
      "segment_id": 1,
      "translation_zh": "自然中文"
    }}
  ]
}}

待补译 segments：
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""


def build_chunk_prompt(chunk: AnalysisChunk, *, profile: str, source_language: str,
                       target_language: str, character_profile: bool = False,
                       summary: bool = True, study_notes: bool = True) -> str:
    prompt = _build_full_chunk_prompt(chunk, profile=profile, source_language=source_language,
                                      target_language=target_language)
    schema = deepcopy(JSON_SCHEMA_HINT)
    excluded: list[str] = []
    if not character_profile:
        schema.pop("profile_observations")
        excluded.extend(("8.", "9.", "10."))
    if not summary:
        for key in ("chunk_summary_zh", "key_points_zh", "content_importance"):
            schema.pop(key)
        excluded.extend(("4.", "5.", "6."))
    if not study_notes:
        for key in ("vocabulary", "grammar", "fixed_expressions", "review_items"):
            schema.pop(key)
        excluded.extend(("11.", "12.", "13.", "14.", "15."))
    prompt = prompt.replace(json.dumps(JSON_SCHEMA_HINT, ensure_ascii=False, indent=2),
                            json.dumps(schema, ensure_ascii=False, indent=2))
    return "\n".join(line for line in prompt.splitlines() if not line.startswith(tuple(excluded)))
