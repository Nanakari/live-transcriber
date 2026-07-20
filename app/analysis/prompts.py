from __future__ import annotations

import json

from .chunker import chunk_to_prompt_payload
from .schemas import AnalysisChunk


PROMPT_VERSION = "multilingual-study-v5-character-profile"

SYSTEM_PROMPT = """你是一个多语言影音文本的中文翻译、内容分析与学习笔记助手。
你的主任务是生成按时间对齐的双语文本：保留原文，给出自然中文翻译和偏直译版本。
源语言可能是任意语言或多语言混用。学习分析只挑真正有价值的词汇、语法、固定表达、语气、文化语境和疑似 ASR 错误。
不要编造上下文，不确定的人名、作品名、游戏名或梗必须标注不确定。
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
            "start": 0.0,
            "end": 2.5,
            "original": "原始 ASR 文本",
            "translation_zh": "自然中文翻译，适合字幕和阅读",
            "literal_zh": "偏直译版本，方便学习原文结构",
            "brief_note": "很短的说明",
            "asr_suspect": False,
            "asr_issue": "",
            "confidence": 0.9,
        }
    ],
    "vocabulary": [
        {
            "word": "词",
            "reading": "日语用假名读音，不要罗马字；英语可为空或给读音提示",
            "meaning_zh": "中文意思",
            "part_of_speech": "词性",
            "example_original": "来自本 chunk 的例句",
            "example_zh": "例句中文",
            "level": "basic/intermediate/advanced/slang/vtuber_term",
        }
    ],
    "grammar": [
        {
            "pattern": "语法模式",
            "explanation_zh": "简洁中文说明",
            "example_original": "来自本 chunk 的例句",
            "example_zh": "例句中文",
            "importance": "low/medium/high",
        }
    ],
    "fixed_expressions": [
        {
            "expression": "固定表达",
            "meaning_zh": "中文意思",
            "usage_note_zh": "用法说明",
            "example_original": "来自本 chunk 的例句",
            "example_zh": "例句中文",
        }
    ],
    "tone_notes": ["语气、吐槽、反问、撒娇、直播口癖等简短说明"],
    "content_tags": ["访谈/课程/会议/娱乐/游戏/新闻/日常对话等内容标签"],
    "speaker_notes": ["只描述文本中有依据的说话人角色、互动关系或明显说话风格，不猜真实身份"],
    "context_notes": ["文化背景、领域知识、节目或作品语境；不确定时明确标注"],
    "task_requirements": ["后续转写、翻译、字幕和人工复核需要特别注意的具体事项"],
    "vtuber_context": ["兼容旧版的直播文化字段；仅在确有 VTuber/直播语境时填写，否则返回空列表"],
    "profile_observations": [
        {
            "speaker_label": "主要说话人；能可靠区分时可写人物称呼",
            "category": "personality/preference/value/habit/communication/social/identity",
            "observation_zh": "对人物性格、喜好、价值取向、习惯或表达方式的具体观察",
            "evidence_zh": "支持该观察的原文事实或行为；注明是直接表达还是谨慎推断",
            "confidence": 0.8,
        }
    ],
    "review_items": [
        {
            "segment_id": 1,
            "start": 0.0,
            "end": 2.5,
            "original": "需要复查的原文",
            "reason_zh": "为什么需要复查",
            "risk_type": "asr/proper_noun/tone/translation",
        }
    ],
    "learning_value": 3.0,
}


def build_chunk_prompt(
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
1. bilingual_lines 必须覆盖输入 segments，保持 segment_id/start/end 对齐。
2. translation_zh 使用自然中文，适合字幕和阅读。
3. literal_zh 偏直译，帮助学习原文结构。
4. brief_note 必须很短，不要写长篇解释。
5. 只从本 chunk 原文提取 vocabulary / grammar / fixed_expressions，不要凭空扩展。
6. vocabulary 最多 8 项，优先保留口语、固定搭配、领域术语和中高级词；不要大量收录基础词，除非它在本句有特殊用法。
7. reading 应使用源语言常用读音表示方式；日语使用平假名/片假名，不要写罗马字。无法确定时留空，不要乱编。
8. grammar 最多 5 项，不要强行分析每一句。
9. fixed_expressions 最多 5 项，优先保留固定搭配、惯用表达、口语表达。
10. review_items 最多 6 项，只放确实值得人工复查的内容。
11. 疑似 ASR 错误、人名/作品名/游戏名不确定、反话或语气不确定时，写入 review_items。
12. 空文本或低价值文本也不要静默删除，可在 brief_note 标注 low_value。
13. 只返回合法 JSON，字段结构必须匹配下面示例。
14. chunk_summary_zh 用 1-3 句客观概括本段实际内容，用于最终形成整段音频的一段式全文概括；即使是过场或闲聊也只需极简说明，不要逐句复述。
15. key_points_zh 只提取真正重要且值得通知用户的内容，例如正式通知、告知、计划或日程变化、明确决定、规则要求、关键事实、重要结论和实质性说明。普通寒暄、游戏过程闲聊、情绪反应、重复内容、无结论的杂谈必须返回空列表。每条必须是信息完整的 1-2 句话。
16. content_importance 使用 0-5 分：正式通知、重大变化或关键决定为 4.5-5；重要事实、结论或实质性说明为 3.5-4.4；一般讨论为 2-3.4；闲聊、过场和重复内容不高于 1.5。
17. profile_observations 用于建立人物侧写。只记录有原文证据的性格表现、明确表达的喜好或厌恶、价值取向、行为习惯、沟通风格、社交互动方式和身份线索。每段最多 6 条；没有可靠证据时返回空列表。
18. profile_observations 的 category 只能使用 personality / preference / value / habit / communication / social / identity；confidence 为 0-1。evidence_zh 必须写明事实依据，并区分“本人直接表达”与“根据本段表现谨慎推断”。
19. 不得根据声音或只言片语推断年龄、疾病、心理障碍、性取向、宗教、政治立场等敏感属性，不做心理诊断。无法可靠区分多人时统一写“主要说话人”，不要猜真实身份。
20. content_tags / speaker_notes / context_notes / task_requirements 仍用于内容理解和学习资料，内容要具体、简短且有原文依据。

JSON 结构示例：
{json.dumps(JSON_SCHEMA_HINT, ensure_ascii=False, indent=2)}

输入 chunk：
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""


def build_repair_prompt(raw_text: str) -> str:
    return f"""下面的模型输出不是合法 JSON。请只修复格式，保留原内容含义，返回一个合法 JSON 对象。
不要添加 Markdown，不要使用代码块，不要解释。

原始输出：
{raw_text}
"""
