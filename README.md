# 影音转写

本机运行的影音处理工具：导入视频链接或本地文件，生成**原文转写、中文翻译、总结和学习笔记**。完整处理默认生成音频播放器和对齐 Gemini Live Translator compact 样式的底部悬挂字幕框；视频字幕预览改为按需生成；人物档案可通过旧版界面或 CLI 按需生成。

语音识别使用 faster-whisper，在本机运行。默认 Web/API 流程的翻译、总结和学习笔记使用 Gemini；只有通过 `live-transcriber` Skill 调用时，才会进入受门控的本地 Codex provider。没有 Gemini API Key 也可以仅转写。

## Windows 下载运行

1. 在 [Releases](https://github.com/Nanakari/live-transcriber/releases) 下载 `LiveTranscriber-版本-windows-x64.zip`，完整解压。
2. 双击 `start.bat`，浏览器将打开本机页面，默认地址 `http://127.0.0.1:7860`。
3. 在设置中填写自己的 Gemini API Key，或选择“仅转写”。
4. 导入文件或链接，在结果页阅读、播放和导出。

发行包包含 Python/Tk、ffmpeg、ffplay、ffprobe、yt-dlp 和 Node.js。保留整个文件夹及 `_internal/`；GitHub 的 Source code ZIP 是源码归档。

发行包以 CPU 为兼容基线，不包含 CUDA 库和语音模型。首次转写需联网下载模型，缓存完整后可离线仅转写。CPU 上首次使用建议选“快速”。

## Windows 源码安装

需要 Python 3.10–3.12，推荐 3.12，并启用 Python Launcher。

```powershell
git clone https://github.com/Nanakari/live-transcriber.git
cd live-transcriber
powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1
.\start.bat
```

安装脚本创建 `.venv/`、安装约束版本的依赖，并准备经过 SHA-256 校验的 FFmpeg 工具，不覆盖已有非空工具。视频站点可能需要 Node.js 22 LTS、代理或登录 cookies，可在设置中配置。

需要 NVIDIA 加速时，安装兼容驱动后运行 `setup.ps1 -Gpu`。自动模式尝试 GPU，失败时明确提示并改用 CPU；“仅 GPU”模式保留错误。

Linux/macOS 可安装 `requirements.txt` 并通过 CLI 运行，自行准备 ffmpeg、ffplay、ffprobe 和 JavaScript 运行时。正式发行包和桌面集成以 Windows 为目标。

## 文档入口

- [使用指南](docs/user-guide.md)：处理流程、播放器、命令行、配置、数据位置和旧版界面。
- [工程结构](docs/architecture.md)：模块职责、依赖方向、资源位置和后续开发约定。
- [开发与发布](docs/development.md)：安装开发环境、统一检查、播放器 smoke、Windows 打包与 CI。
- [文件整理与维护](docs/maintenance.md)：生成文件分类、历史目录整理与可追溯迁移。
- [贡献约定](CONTRIBUTING.md)与[安全说明](SECURITY.md)。

默认配置统一位于 `app/defaults.yaml`；个人覆盖放在数据目录的 `config.local.yaml`。默认用户结果在 `outputs/`，开发产物在 `build/`，发行文件在 `dist/`。`LIVE_TRANSCRIBER_HOME` 和 `app.output_dir` 可调整数据位置，详见使用指南。

## 开发检查

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1 -Dev
.\.venv\Scripts\python.exe -m scripts.check
```

本地、CI 和发行构建使用相同检查入口。真实音频和独立发行包验证见开发指南。

## 许可证

项目代码使用 [MIT](LICENSE)。第三方工具和依赖保留各自许可证，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。转写与分析结果请结合原音频和复查清单使用。
