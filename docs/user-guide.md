# 使用指南

安装入口见 [README](../README.md)，本地开发与打包见 [开发指南](development.md)。

## 处理与恢复

- 导入媒体链接或本地文件，默认生成转写、中文翻译、总结、学习笔记和音频字幕播放器。视频字幕预览按需生成；人物档案可通过旧版界面或 CLI 开启。
- 默认质量为 `high`，使用 `large-v3-turbo` 并启用词级时间戳。需要更快处理时可选择 `fast` / `small`；已保存的个人设置仍可覆盖默认值。
- “更多选项”可以设置网络媒体的开始／结束时间。源语言默认自动识别，翻译和学习资料的目标语言为中文。
- 历史任务的“更多操作”支持重新分析、重试失败分段、重新生成音频播放器和生成字幕视频。每个媒体目录的 README 提供结果入口。
- 分段失败会显示“部分完成”和“翻译暂缺”。已完成结果及分析缓存保留，可修正配置后恢复。
- 密钥、权限或请求配置错误会停止后续分析请求，保存已有结果并以失败状态结束；超时、限流和临时服务错误仍按重试策略处理。
- `--resume` 复用内容和分析参数一致的成功缓存。它不会跳过新 pipeline 的下载与转写；继续已有任务应使用历史任务的重新分析或 `analyze`。

分析会先校验模型返回的原文、时间戳与段号，再补齐省略字段。错配内容会定向重试，仍失败的内容不会作为成功译文缓存。升级缓存格式或改变人物档案等分析参数后，需要重新请求分析；已有转写和分析文件保留。

CLI 分析退出码为 `0` 成功、`1` 失败、`2` 部分完成。结果中的 `quality_status` 分别用 `complete`、`complete_with_warnings`、`partial` 表示完成、需复查、缺失翻译。

## 播放器与导出

完整处理默认生成轻量音频播放器，使用隐藏的 ffplay 播放，支持播放／暂停、进度拖动、前后跳转 10 秒和键盘操作。`.cmd` 启动器可直接打开字幕窗，无需为 `.pyw` 配置文件关联。

重新分析后，页面会校验播放器使用的分析版本。过期或没有版本记录的播放器需要重新生成，才能播放或导出。文件仍保留在原目录中。

“完整音频播放器包（ZIP）”包含音频、字幕、播放器、Python/Tk 运行组件及 ffplay/ffprobe。完整解压后可以在 Windows 运行，不依赖原项目目录或系统 Python。发行包直接复用内置组件；源码模式首次导出需要安装 `requirements-dev.txt`，并用 PyInstaller 构建、缓存播放器。

文档 ZIP 与播放器 ZIP 是不同导出入口。页面提供原文 Markdown/SRT/JSON、总结和学习笔记；完整双语稿和分析诊断保存在媒体任务目录。

可选视频模式使用静态封面和 25 fps，不包含源视频画面。连续短句合并成约 5–9 秒的显示字幕；原文白色、中文暖黄色，配细描边。原始细分字幕保留在 `subtitles/`。

## 配置与数据位置

应用默认配置只有一份：源码中的 `app/defaults.yaml`，发行包中位于 `_internal/app/defaults.yaml`。两者使用同一资源。个人修改写入数据目录的 `config.local.yaml`，可参照 [配置示例](../config.local.example.yaml)。原有数据目录中的 `config.yaml` 仍可作为兼容覆盖配置读取。

覆盖顺序为：内置默认值 → 数据目录 `config.yaml`（如有）→ `config.local.yaml`。密钥默认只保留在当前浏览器标签页；勾选“在此浏览器记住密钥”后写入本地存储。也可通过 `GEMINI_API_KEY` 或数据目录的 `secrets.local.env` 提供密钥。

数据根目录优先使用 `LIVE_TRANSCRIBER_HOME`。源码与项目 `dist/` 内的 EXE 共用项目目录；独立解压的 Windows 包使用 `%LOCALAPPDATA%\LiveTranscriber`。设置中的“环境与诊断”显示实际位置。

`app.output_dir` 统一决定转写、分析、预览、共享缓存和任务日志的位置，网页历史、下载、删除均使用它。相对路径以数据根目录为基准，也支持项目外的绝对路径。修改配置不会自动搬迁旧结果；查看原目录结果时需切回原配置。

```text
数据目录/
  config.local.yaml
  secrets.local.env
  models/                        模型缓存，可用 HF_HOME 覆盖
  outputs/                       可用 app.output_dir 修改
    media/<媒体任务>/
      README.md                  结果入口
      audio/                     源音频、播放器、音频双语字幕
        assets/                  播放器组件、版本清单、日志
      transcripts/               原始稿、清理稿、原文字幕
      analysis/<分析批次>/       翻译、总结、学习笔记与诊断
      video/                     按需生成的 MP4 和字幕窗
        subtitles/               视频字幕
        assets/                  封面、版本清单、日志
      thumbnails/                媒体封面
      logs/                      媒体转写日志
    cache/analysis/              可恢复的分析缓存
    cache/player-runtime/        可重建的播放器运行组件
    logs/web_jobs/               Web 任务日志
    logs/maintenance/            数据迁移记录
    _staging/                    未完成导入及失败任务诊断
```

完整流程完成后清理中间 WAV，保留源 M4A 供重新生成预览。旧配置 `delete_source_m4a_after_preview` 已不生效；旧版本已经删除的音频不会自动恢复。用户数据整理方式见 [维护说明](maintenance.md)。

更新独立发行包时替换程序文件夹即可。通过 `LIVE_TRANSCRIBER_HOME` 可继续使用旧数据目录；先检查原有代理和设备设置。

## 分析选项

`features` 支持 `summary`、`study_notes`、`character_profile`。人物档案开关参与提示词、缓存和导出；关闭时不会请求该内容。

只有分析模型明确提供源语言修复候选，且置信度至少为 `0.80`，才会自动写入 `repaired_transcript.srt` 和播放器。原始 ASR 保留在 `transcripts/`；候选原文、修复文、置信度和状态记录在 `repair_log.json`、`review.md`。可用 `analysis.auto_repair_enabled`、`analysis.auto_repair_threshold`，或 CLI 的 `--no-auto-repair`、`--auto-repair-threshold` 调整。

本地 Skill 分析单块默认超时 360 秒。超时后保留缓存、缩小失败分块，并用 `--resume` 定向恢复。普通 Web/API 流程使用 Gemini；本地 Codex provider 由 Skill 门控。

开启总结后，会汇总所有分段摘要和要点生成全片总结，并标注来源时间。复用分段缓存时仍会进行全片总结步骤；成功且参数一致的总结缓存可复用。最终总结失败时，转写、翻译和学习资料保留，`video_summary.md` 标记失败并列出可用分段摘要。

## 命令行

从项目根目录运行：

```powershell
# 完整处理，默认生成音频播放器
.\.venv\Scripts\python.exe main.py pipeline --input "D:\media\sample.mp4" --resume

# 按需生成字幕视频
.\.venv\Scripts\python.exe main.py pipeline --input "D:\media\sample.mp4" --preview-mode video --resume

# 仅转写，无需 Gemini Key
.\.venv\Scripts\python.exe main.py transcribe --input "D:\media\sample.wav" --device cpu

# 分析已有转写，可增加人物档案
.\.venv\Scripts\python.exe main.py analyze --input "转写稿路径.json" --character-profile --resume

# 只生成文档
.\.venv\Scripts\python.exe main.py pipeline --url "视频链接" --modules transcribe,analyze
```

## 界面选择

右上角可切换精简界面和旧版完整界面。两种界面共享后端、任务与结果，切换不会取消任务，也不会启动旧 EXE。浏览器会记住所选布局。

| 功能 | 精简界面 | 旧版完整界面 |
| --- | --- | --- |
| 转写、翻译、总结、学习笔记 | 默认核心流程 | 完整流程和独立模块 |
| 人物档案 | 通过旧版或 CLI | 分析选项中勾选 |
| 音频播放器、字幕视频 | 结果中的更多操作 | 独立模块，可手选媒体、字幕、封面和分辨率 |
| 分析已有稿件 | 历史任务重新分析 | 手动选择 transcript.json |
| 分段检查、试跑 1／3 段 | 不单独展示 | 保留 |
| Beam、计算类型、词级时间戳、Cookies | 默认值或部分设置 | 保留高级设置 |
| 单文件下载、删除结果 | 保留 | 使用精简界面操作 |

旧版转写、字幕、分析和 `previews/potplayer` 目录仍可读取。页面切换不搬迁其他目录的数据。Web 服务只监听本机，当前没有远程文件上传、多用户鉴权或服务器部署功能。
