"""CLI arguments shared by source and packaged entry points."""
from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="多语言影音转写与内容研析工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    transcribe = subparsers.add_parser("transcribe", help="下载/提取音频并转写")
    transcribe.add_argument("--url", help="YouTube 直播回放或普通视频 URL")
    transcribe.add_argument("--input", help="本地音频或视频文件")
    transcribe.add_argument("--output-dir", help="输出目录，默认读取应用配置")
    transcribe.add_argument("--quality", choices=["fast", "accurate", "cpu_safe", "high"], help="质量模式")
    transcribe.add_argument("--model", help="覆盖 quality profile 的模型名称或本地模型路径")
    transcribe.add_argument("--device", choices=["auto", "cuda", "cpu"], help="运行设备")
    transcribe.add_argument("--compute-type", choices=["int8_float16", "float16", "int8"], help="CTranslate2 compute_type")
    transcribe.add_argument("--language", default=None, help="识别语言，默认 auto 自动检测；也可指定 ja、en、zh 等语言代码")
    transcribe.add_argument("--beam-size", type=int, help="beam size")

    vad = transcribe.add_mutually_exclusive_group()
    vad.add_argument("--vad-filter", dest="vad_filter", action="store_true", default=None, help="启用 VAD")
    vad.add_argument("--no-vad-filter", dest="vad_filter", action="store_false", help="关闭 VAD")

    words = transcribe.add_mutually_exclusive_group()
    words.add_argument("--word-timestamps", dest="word_timestamps", action="store_true", default=None, help="启用词级时间戳")
    words.add_argument("--no-word-timestamps", dest="word_timestamps", action="store_false", help="关闭词级时间戳")

    previous = transcribe.add_mutually_exclusive_group()
    previous.add_argument(
        "--condition-on-previous-text",
        dest="condition_on_previous_text",
        action="store_true",
        default=None,
        help="启用上下文条件",
    )
    previous.add_argument(
        "--no-condition-on-previous-text",
        dest="condition_on_previous_text",
        action="store_false",
        help="关闭上下文条件，减少重复幻觉",
    )

    transcribe.add_argument("--initial-prompt", help="ASR 初始提示词")
    transcribe.add_argument("--terms-file", help="术语文件路径，UTF-8")
    transcribe.add_argument("--download-start", help="YouTube 部分下载开始时间，例如 90、1:30、5：2：3；默认从开头")
    transcribe.add_argument("--download-end", help="YouTube 部分下载结束时间，例如 180、3:00、6：0：0；默认到结尾")
    transcribe.add_argument("--proxy", help="传给 yt-dlp 的代理地址，例如 http://127.0.0.1:7890")
    transcribe.add_argument("--cookies-from-browser", help="让 yt-dlp 读取浏览器 cookies，例如 edge、chrome、firefox")
    transcribe.add_argument("--cookies", help="Netscape cookies.txt 文件路径")
    transcribe.add_argument("--remote-components", default="ejs:github", help="Allow yt-dlp remote JS challenge components, default ejs:github")
    transcribe.add_argument("--keep-temp", action="store_true", help="保留额外临时文件")
    transcribe.add_argument("--debug", action="store_true", help="输出 traceback 和详细日志")

    analyze = subparsers.add_parser("analyze", help="读取 transcript.json，生成双语文本和学习笔记")
    analyze.add_argument("--input", required=True, help="模块一生成的 transcript.json")
    analyze.add_argument("--provider", default=None, choices=["gemini", "local"], metavar="PROVIDER", help=argparse.SUPPRESS)
    analyze.add_argument("--profile", default=None, help="分析 profile，默认 multilingual_study")
    analyze.add_argument("--source-language", default=None, help="源语言，默认从 transcript.json meta 读取")
    analyze.add_argument("--target-language", default=None, help="目标语言，默认 zh")
    analyze.add_argument("--model", default=None, help="Gemini 模型名称")
    analyze.add_argument("--fallback-model", default=None, help="主模型不可用或配额不足时使用的 Gemini 备用模型")
    analyze.add_argument("--chunk-minutes", type=float, default=None, help="每个 chunk 覆盖的分钟数")
    analyze.add_argument("--max-segments-per-chunk", type=int, default=None, help="每个 chunk 最大 segment 数")
    analyze.add_argument("--invalid-json-retries", type=int, default=None, help="JSON 无效时的额外重试次数")
    analyze.add_argument("--limit-chunks", type=int, default=None, help="只处理前 N 个 chunk")
    analyze.add_argument("--local-concurrency", type=int, default=None, help="Skill 内部本地分析并发数，默认 4")
    analyze.add_argument("--auto-repair", action=argparse.BooleanOptionalAction, default=None, help="启用高置信度源语言自动修复；原始 ASR 保留")
    analyze.add_argument("--auto-repair-threshold", type=float, default=None, help="自动修复最低置信度，默认 0.80")
    analyze.add_argument("--dry-run", action="store_true", help="只展示 chunk 切分结果，不调用 API")
    analyze.add_argument("--resume", action="store_true", help="跳过已成功处理的 chunk")
    analyze.add_argument("--debug", action="store_true", help="输出 traceback 和详细日志")

    preview = subparsers.add_parser("preview", help="生成音频播放器包；也可选择生成带烧录字幕的视频")
    preview.add_argument("--audio", required=True, help="音频路径，推荐原始音频；也可使用 clean_16k.wav")
    preview.add_argument("--subtitle", required=True, help="translation_zh.srt 路径")
    preview.add_argument("--cover", help="视频模式封面图路径；音频模式不需要封面")
    preview.add_argument("--output-dir", help="输出目录；默认媒体组的 audio/目录")
    preview.add_argument("--resolution", default="1280x720", help="预览视频分辨率，默认 1280x720")
    preview.add_argument("--subtitle-name", default="live_preview.zh.srt", help="输出中文字幕文件名")
    preview.add_argument("--video-name", default="live_preview.mp4", help="输出视频文件名")
    preview.add_argument("--mode", default="audio", choices=["audio", "video", "potplayer"], help="预览模式，默认 audio；video/potplayer 为可选视频模式")
    preview.add_argument("--debug", action="store_true", help="打印 ffmpeg 命令和错误信息")

    pipeline = subparsers.add_parser("pipeline", help="按选择顺序执行转写、分析、预览")
    pipeline.add_argument("--url", help="YouTube 直播回放或普通视频 URL")
    pipeline.add_argument("--input", help="本地音频或视频文件")
    pipeline.add_argument("--modules", default="transcribe,analyze,preview", help="逗号分隔：transcribe,analyze,preview；默认生成音频播放器包")
    pipeline.add_argument("--transcript", help="跳过转写时使用的 transcript.json")
    pipeline.add_argument("--audio", help="跳过转写时用于预览的音频")
    pipeline.add_argument("--subtitle", help="跳过分析时用于预览的 translation_zh.srt")
    pipeline.add_argument("--cover", help="预览封面图；留空时优先使用同一媒体文件夹的缩略图")
    pipeline.add_argument("--quality", choices=["fast", "accurate", "cpu_safe", "high"], help="质量模式")
    pipeline.add_argument("--model", help="覆盖转写 quality profile 的模型名称或本地模型路径")
    pipeline.add_argument("--device", choices=["auto", "cuda", "cpu"], help="运行设备")
    pipeline.add_argument("--compute-type", choices=["int8_float16", "float16", "int8"], help="CTranslate2 compute_type")
    pipeline.add_argument("--language", default=None, help="识别语言，默认 auto 自动检测；也可指定语言代码")
    pipeline.add_argument("--beam-size", type=int, help="beam size")
    pipeline.add_argument("--download-start", help="YouTube 部分下载开始时间，例如 90、1:30、5：2：3；默认从开头")
    pipeline.add_argument("--download-end", help="YouTube 部分下载结束时间，例如 180、3:00、6：0：0；默认到结尾")
    pipeline.add_argument("--proxy", help="传给 yt-dlp 的代理地址")
    pipeline.add_argument("--cookies-from-browser", help="让 yt-dlp 读取浏览器 cookies")
    pipeline.add_argument("--cookies", help="Netscape cookies.txt 文件路径")
    pipeline.add_argument("--remote-components", default="ejs:github", help="Allow yt-dlp remote JS challenge components")
    pipeline.add_argument("--word-timestamps", dest="word_timestamps", action="store_true", default=None, help="启用词级时间戳")
    pipeline.add_argument("--no-word-timestamps", dest="word_timestamps", action="store_false", help="关闭词级时间戳")
    pipeline.add_argument("--provider", default=None, choices=["gemini", "local"], metavar="PROVIDER", help=argparse.SUPPRESS)
    pipeline.add_argument("--profile", default=None, help="分析 profile")
    pipeline.add_argument("--analysis-model", default=None, help="Gemini 模型名称")
    pipeline.add_argument("--analysis-fallback-model", default=None, help="Gemini 备用模型名称")
    pipeline.add_argument("--limit-chunks", type=int, default=None, help="只处理前 N 个 chunk")
    pipeline.add_argument("--local-concurrency", type=int, default=None, help="Skill 内部本地分析并发数，默认 4")
    pipeline.add_argument("--auto-repair", action=argparse.BooleanOptionalAction, default=None, help="启用高置信度源语言自动修复；原始 ASR 保留")
    pipeline.add_argument("--auto-repair-threshold", type=float, default=None, help="自动修复最低置信度，默认 0.80")
    pipeline.add_argument("--resume", action="store_true", help="跳过已成功处理的 chunk")
    pipeline.add_argument("--resolution", default="1280x720", help="预览视频分辨率")
    pipeline.add_argument("--preview-mode", default="audio", choices=["audio", "video", "potplayer"], help="预览模式，默认 audio；video/potplayer 为可选视频模式")
    pipeline.add_argument("--debug", action="store_true", help="输出 traceback 和详细日志")

    web = subparsers.add_parser("web", help="启动多语言影音研析界面")
    web.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    web.add_argument("--port", type=int, default=7860, help="监听端口，默认 7860")
    web.add_argument("--open-browser", action="store_true", help="启动后打开浏览器")
    web.add_argument("--no-browser", action="store_true", help="启动后不打开浏览器，便于自动化自检")
    for command_parser in (analyze, pipeline):
        for feature in ("character-profile", "summary", "study-notes"):
            command_parser.add_argument(f"--{feature}", action=argparse.BooleanOptionalAction, default=None)
    return parser
