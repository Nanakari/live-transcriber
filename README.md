# 影音转写

本机运行的影音处理工具：导入视频链接或本地文件，生成**原文转写、中文翻译、总结和学习笔记**。完整处理默认生成音频播放器和对齐 Gemini Live Translator compact 样式的底部悬挂字幕框；视频字幕预览改为按需生成；人物档案可通过旧版界面或 CLI 按需生成。

语音识别使用 faster-whisper，在本机运行。默认 Web/API 流程的翻译、总结和学习笔记使用 Gemini；只有通过 `live-transcriber` Skill 调用时，才会进入受门控的本地 Codex provider。没有 Gemini API Key 也可以仅转写。

## 下载运行（Windows）

1. 在 [Releases](https://github.com/Nanakari/live-transcriber/releases) 下载 `LiveTranscriber-版本-windows-x64.zip`，完整解压。
2. 双击 `start.bat`，浏览器将打开本机页面，默认地址为 `http://127.0.0.1:7860`。
3. 在“设置”中填写自己的 Gemini API Key；或先点击“仅转写”。
4. 导入文件或链接，点击“开始处理”，在结果页阅读或导出。

发行包包含 Python/Tkinter 运行时、ffmpeg、ffplay、ffprobe、yt-dlp 和 Node.js，无需另装 Python 即可运行音频字幕播放器。保留整个文件夹及 `_internal` 子目录。GitHub 自动提供的 **Source code ZIP 是源码，不是可执行程序包**。

Windows 发行包以 CPU 为兼容基线，不包含 CUDA 运行库或语音模型。首次转写需要联网下载模型并留出足够磁盘空间；页面显示下载阶段，失败后可重新开始。缓存完整后，仅转写可以离线使用。高质量模型在 CPU 上可能较慢，首次使用建议选择“快速”。

## 源码安装（Windows）

需要 Python 3.10–3.12，推荐 Python 3.12。安装 Python 时启用 Python Launcher。

```powershell
git clone https://github.com/Nanakari/live-transcriber.git
cd live-transcriber
powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1
.\start.bat
```

安装脚本会创建 `.venv`、安装约束版本的依赖，并从 imageio-ffmpeg wheel 准备 ffmpeg；不会覆盖已有的 `tools/ffmpeg.exe`。

需要 NVIDIA 加速时，先安装兼容的 NVIDIA 驱动，再执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1 -Gpu
```

自动模式优先尝试可用 GPU，失败后会明确提示并改用 CPU；“仅 GPU”模式保留错误，不自动切换。视频站点的验证可能需要 Node.js、代理或登录 cookies，可在设置中配置。源码模式可安装 Node.js 22 LTS；发行包自带 Node.js。

使用已存在的 Python 安装创建环境：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1 -Python "C:\Python312\python.exe"
```

Linux/macOS 可安装 `requirements.txt` 并通过 CLI 运行，ffmpeg 和 JavaScript 运行时需自行准备；当前正式发行包和桌面集成以 Windows 为目标。

## 简洁的处理流程

- 首页只有一个媒体入口，默认生成转写、翻译、总结、学习笔记及音频播放器；源语言默认自动识别，视频字幕预览按需选择。
- “更多选项”可设置网络视频的开始/结束时间；人物档案可从旧版界面或 CLI 开启。
- 精简界面可阅读转写、总结和学习笔记，导出原文 Markdown/SRT/JSON、总结、学习笔记及音频播放器包。完整双语稿及其他分析文件保存在任务目录。
- 历史任务的“更多操作”支持重新分析、重试失败分段、生成音频播放器或按需生成通用字幕视频。播放使用隐藏的 ffplay，不依赖 PotPlayer。
- 密钥默认保留在当前标签页；勾选“在此浏览器记住密钥”后保存在该浏览器本地存储。不要在公共电脑上记住密钥。
- 部分翻译失败会显示“部分完成”，占位字幕明确标记“翻译暂缺”，已有结果保留以便重试。

## 数据、模型与更新

数据目录优先使用 `LIVE_TRANSCRIBER_HOME`。源码运行及位于本项目 `dist` 下的 EXE 共用项目目录；单独解压到其他位置的 Windows 发行包使用 `%LOCALAPPDATA%\LiveTranscriber`。设置中的“环境与诊断”显示实际数据和模型缓存位置。

```text
数据目录/
  config.local.yaml       可选的本机配置覆盖
  secrets.local.env       可选的 GEMINI_API_KEY=... 配置
  models/                 模型缓存（可用 HF_HOME 覆盖）
  outputs/media/媒体任务/
    audio/                源媒体副本与中间音频
    transcripts/          原始稿、清理稿、原文字幕
    analysis/             每次分析的独立结果
    video/                最终 MP4、悬挂字幕框；subtitles/ 存放字幕，assets/ 存放封面和日志
    thumbnails/
    logs/
```

使用 `LIVE_TRANSCRIBER_HOME` 可以指定独立数据目录。程序更新时替换发行文件夹即可，用户数据不在发行目录里。旧版项目中的结果不会自动移动；可设置该环境变量指向旧项目目录继续读取，但请先检查旧配置中的代理和设备设置。

公共配置 `config.yaml` 的代理为空；个人配置放进 `config.local.yaml`，不要提交密钥、cookies、媒体、输出或模型。Windows 包使用内置默认值和数据目录中的覆盖配置。

可在 `features` 中配置 `summary`、`study_notes` 和 `character_profile`；网页默认请求完整核心结果，CLI 可按下列选项覆盖。人物档案开关参与提示词、缓存和导出，关闭时不会请求该部分内容。

分析默认采用“高置信度自动修复 + 原文可回溯”：只有模型明确提供源语言修复候选且 `repair_confidence >= 0.80` 时，修复才会进入 `repaired_transcript.srt` 和播放器；原始 ASR 保留在 `transcripts/`，每条候选的原文、修复文、置信度和状态保存在 `repair_log.json` 与 `review.md`。本地 Skill 分析 worker 的默认单块超时为 360 秒；超时后应保留已完成缓存、缩小失败分块并使用 `--resume` 定向恢复。可在 `analysis.auto_repair_enabled`、`analysis.auto_repair_threshold` 或 CLI 的 `--no-auto-repair`、`--auto-repair-threshold` 中调整。

完整流程结束后清理中间 WAV，源 M4A 保留至用户主动清理，以便重新生成预览。旧配置中的 `delete_source_m4a_after_preview` 不再生效。默认流程只在 `audio/` 生成播放器和双语时间轴，不编码 MP4；需要视频时使用 `--preview-mode video`。`.cmd` 启动器可直接打开悬挂字幕，不要求 Windows 预先关联 `.pyw` 文件。音频模式提供播放/暂停、进度拖动、10 秒快进/后退和键盘快捷键。

开启总结时，分段分析完成后会额外请求一次全片总结，汇总所有分段摘要和要点，并标注来源时间段；复用分段缓存时也会重新生成全片总结。若最终总结请求失败，转写、翻译和学习资料仍保留，`video_summary.md` 会明确标记并列出全部可用分段摘要。

## 命令行

```powershell
# 默认转写、翻译、总结、学习笔记，并生成音频播放器
.\.venv\Scripts\python.exe main.py pipeline --input "D:\media\sample.mp4" --resume

# 显式生成视频字幕预览
.\.venv\Scripts\python.exe main.py pipeline --input "D:\media\sample.mp4" --preview-mode video --resume

# 仅转写，不需要 Gemini Key
.\.venv\Scripts\python.exe main.py transcribe --input "D:\media\sample.wav" --device cpu

# 使用已有转写稿，按需增加人物档案
.\.venv\Scripts\python.exe main.py analyze --input "转写稿路径.json" --character-profile --resume

# 只生成文档，跳过视频生成
.\.venv\Scripts\python.exe main.py pipeline --url "视频链接" --modules transcribe,analyze
```

`--resume` 当前复用**相同内容和分析参数下成功的分段/全片总结缓存**，不会跳过一次新 pipeline 的下载或转写。要继续已有任务，请从历史任务重新分析，或直接使用 `analyze`。改变人物档案选项会使用独立缓存并重新分析。分析结果还会记录 `quality_status`：无风险为 `complete`，有人工复查项但流程完成为 `complete_with_warnings`，存在缺失翻译为 `partial`。

CLI 分析退出码：`0` 成功，`1` 失败，`2` 部分完成。源语言支持自动检测与 Whisper 语言代码；翻译和学习资料的目标语言目前为中文。

## 开发、构建与发布

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1 -Dev
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q app scripts main.py
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1 -Version 0.2.0
```

构建需要 Node.js。脚本校验本机 Node.js 与官方 Windows x64 校验和，收集工具与许可证，用 PyInstaller 文件夹模式打包，再在隔离数据目录和最小 PATH 下检查 Web、静态资源、Whisper 导入、yt-dlp、ffmpeg 和 Node.js。通过后生成 ZIP 和 SHA-256 校验文件。

CI 覆盖 Windows / Ubuntu 和 Python 3.10 / 3.12。推送 `v*` 标签会触发 Windows 构建并创建 **草稿 Release**，检查附件和说明后再发布；手动触发 workflow 只上传构建附件。本地生成文件不等于已经发布到 GitHub。

当前 Web 服务只允许本机监听，不包含远程文件上传、多用户鉴权或服务器部署能力。更多开发约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

项目代码使用 [MIT](LICENSE) 许可证。依赖和工具保留各自的许可证，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。AI 转写和分析结果建议结合原音频与复查清单使用。

## 精简界面与旧版完整界面

右上角“切换旧版界面”可打开旧版完整操作布局；旧版右上角可切回精简界面。浏览器记住所选布局。两种布局运行同一个新版后端，任务列表和已有结果共用，切换不取消正在运行的任务。这是界面切换，不是运行旧 EXE 或回退算法。

| 功能 | 精简界面 | 旧版完整界面 |
| --- | --- | --- |
| 转写、中文翻译、总结、学习笔记 | 默认核心流程 | 完整流程和独立模块 |
| 人物档案 | 使用旧版界面或 CLI | 分析选项中勾选 |
| 音频播放器／悬挂字幕 | 默认完整流程、结果中的更多操作 | 独立模块，手选音频和字幕 |
| 字幕视频 | `--preview-mode video` 或结果中的更多操作 | 独立模块，手选音频、字幕、封面和分辨率 |
| 独立分析已有转写稿 | 从历史结果重新分析 | 可手动选择 transcript.json |
| 检查分段、试跑 1／3 段 | 不单独展示 | 保留 |
| Beam、计算类型、词级时间戳、浏览器 Cookies | 使用默认值或部分设置 | 保留高级参数 |
| 单文件下载、删除历史结果 | 保留 | 使用精简界面操作 |
| 旧版主题 | 简洁中性样式 | 保留双主题配色和布局，插画换为中性渐变 |

旧版的转写、字幕、分析文件仍可被读取；缓存格式已更新，旧缓存可能需要重新分析。项目中的源码和 EXE 共用 outputs；独立发行包可用 `LIVE_TRANSCRIBER_HOME` 指向同一数据根目录。单纯切换界面不会迁移其他目录的数据。


## 统一目录与旧文件整理

两个页面只负责交互，统一调用 `app/web/routes.py`、`app/web/jobs.py` 和同一组处理模块；不会分别生成“新版结果”和“旧版结果”。

```text
outputs/
  media/<媒体任务>/
    audio/         音频
    transcripts/   原文 JSON、SRT、Markdown
    analysis/      每次分析结果、分段和诊断
    video/         最终 MP4、悬挂字幕框与字幕子目录
    thumbnails/    媒体封面
    logs/          该媒体的转写日志
  cache/analysis/  共用分析缓存
  logs/web_jobs/   网页任务日志
  logs/maintenance/ 迁移记录
  _staging/       尚未完成的导入及失败任务诊断
archive/          本机旧程序、旧素材和迁移前备份（不提交 GitHub）
dist/<版本>/      当前打包程序
output/           开发验证截图和构建日志（不属于用户结果）
```

根目录统一使用 `start.bat`；`启动新版.bat` 是同一入口的本机快捷脚本。旧 EXE 仅供归档，正常使用请在新程序右上角切换页面。

整理旧目录时，先预览，再执行；仅相同内容的冲突文件会去重，不同内容保留独立副本。更新旧 JSON 中失效的输出文件路径之前会在 archive/migration 备份。

```powershell
.\.venv\Scripts\python.exe scripts\consolidate_data.py --import-from "$env:LOCALAPPDATA\LiveTranscriber"
.\.venv\Scripts\python.exe scripts\consolidate_data.py --import-from "$env:LOCALAPPDATA\LiveTranscriber" --apply
```

运行整理脚本前，请结束处理任务并关闭服务。不要把其他无关目录作为导入来源。

## v0.2.3 更新与已知限制

- 源 M4A 保留至主动清理，便于重建字幕视频。
- 全片总结覆盖所有分段摘要，并标注来源时间；失败时保留分段摘要。
- 改进下载重试、JSON 修复、字幕路径转义和仅转写结果查看／导出。
- 可选字幕视频使用静态封面和 25 fps；连续短句合并为约 5–9 秒的显示字幕，已有较长单句保留原时长，细分原始字幕保留。原文使用白色、中文使用暖黄色，配深色细描边，不使用字幕背景框；不包含原视频画面。
- 旧版本已删除的源音频不会自动恢复；ASR 和翻译仍需按复查清单核验。

## 新的默认设置与文件入口

默认转写质量为 high，模型为 large-v3-turbo，并启用词级时间戳。可以手动选择快速 small；已保存的个人质量设置仍可覆盖默认值。

每个媒体任务的 README.md 提供原文、最新总结、学习笔记和默认音频模式入口；显式选择视频模式后再提供字幕视频和视频悬挂字幕框。audio/ 保留源音频、音频模式双语字幕和独立音频播放器；video/ 仅在生成视频模式时使用；细分字幕放在 subtitles/，封面、日志和关联数据放在 assets/，不再重复复制分析文档。旧 previews/potplayer 目录仍可读取。
