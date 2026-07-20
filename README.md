# 多语言影音研析

[![CI](https://github.com/Nanakari/live-transcriber/actions/workflows/ci.yml/badge.svg)](https://github.com/Nanakari/live-transcriber/actions/workflows/ci.yml)

这是一个在本机运行的多语言影音处理工具。它可以读取 YouTube 等网络视频，或本地音频/视频文件，自动识别原文语言并生成：

- 带时间轴的原文转写稿
- 自然中文翻译与中文字幕
- 基于音频证据的人物 Profile（Markdown）
- 过滤闲聊后的重要要点和一段式全文概括
- 完整学习笔记、词汇、语法与固定表达
- 人工复查清单
- PotPlayer 媒体预览包

默认使用 faster-whisper 进行语音识别、Gemini 进行翻译和内容分析。支持日语、英语、中文、韩语、法语、德语、西班牙语及 Whisper 能识别的其他语言，也支持自动语言检测。

## 快速启动

双击 `start.bat`，或运行：

```powershell
python main.py web --open-browser
```

默认地址为 `http://127.0.0.1:7860`。页面中的 Gemini API Key 仅保存在当前浏览器的本地存储中；也可在项目根目录的 `secrets.local.env` 中配置：

```text
GEMINI_API_KEY=你的密钥
```

## 完整处理

网页中的“完整处理”会按顺序执行：

1. 语音转写
2. 翻译与学习分析（同时生成人物 Profile）
3. 媒体预览

命令行示例：

```powershell
python main.py pipeline --input "D:\media\example.mp4" --language auto --resume
```

处理网络视频：

```powershell
python main.py pipeline --url "https://www.youtube.com/watch?v=..." --language auto --resume
```

## 单项处理

仅转写：

```powershell
python main.py transcribe --input "audio.wav" --language auto --quality high
```

已知语言时可用标准语言代码覆盖自动检测，例如 `--language ja`、`--language en` 或 `--language zh`。

对已有转写稿执行翻译与学习分析：

```powershell
python main.py analyze --input "outputs\media\任务目录\transcripts\任务_transcript.json" --profile multilingual_study --resume
```

分析完成后，同一个分析目录会包含：

```text
character_profile.md  人物性格、喜好、习惯与表达方式档案
video_summary.md      重要要点与全文概括
study_notes.md        完整学习笔记
bilingual.md          双语稿
translation_zh.srt    中文字幕
vocabulary.md         词汇表
grammar.md            语法与固定表达
review.md             人工复查清单
analysis.json         完整结构化分析结果
```

人物 Profile 是翻译与学习分析的一部分，不需要单独运行。它只根据音频中的直接表达和行为证据，整理人物性格表现、喜好与兴趣、价值取向、习惯、沟通风格、社交方式和身份线索，并为每条观察附上依据、时间范围与置信度；不做心理诊断或敏感属性推断。

## 默认配置

主要设置位于 `config.yaml`：

```yaml
transcribe:
  language: "auto"
  quality: "high"

analysis:
  provider: "gemini"
  model: "gemini-3.1-flash-lite"
  target_language: "zh"
  profile: "multilingual_study"
```

公共配置不启用代理。需要本机代理或其他覆盖项时，将
`config.local.example.yaml` 复制为 `config.local.yaml` 后修改；该文件不会被 Git
跟踪。

日语任务仍可使用 `dictionaries/vtuber_terms.txt` 作为默认术语提示；其他语言不会自动加载这份日语词表。

## 输出目录

每个媒体任务独立保存在：

```text
outputs/media/<任务名称>/
  audio/
  transcripts/
  analysis/
  previews/
  thumbnails/
  logs/
```

所有文件都保存在本机。AI 翻译与分析属于初稿，专有名词、反话、多人重叠、口音、方言及疑似 ASR 错误应结合 `review.md` 人工复核。

## 环境要求

- Python 3.10+
- ffmpeg（项目自带 `tools/ffmpeg.exe` 时会优先使用）
- faster-whisper
- yt-dlp
- Gemini API Key（运行翻译与学习分析时需要）

安装依赖：

```powershell
pip install -r requirements.txt
```

如遇问题，可在网页展开“实时详细日志”，或查看对应任务目录和 `outputs/web_jobs/` 下的日志。

## 开发与验证

开发依赖和检查命令：

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m compileall -q app main.py
```

提交改动前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。安全问题和敏感信息处理方式见
[SECURITY.md](SECURITY.md)。

## 许可证

本仓库目前尚未授予开源许可证。除非另有明确说明，否则公开可见不代表允许复制、修改或分发。
