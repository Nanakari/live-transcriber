# 开发与发布

## 环境与依赖

使用 Python 3.10–3.12，推荐 3.12。开发检查及 Windows 构建需要 Node.js 22 LTS。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1 -Dev
# 如需指定已有 Python
powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1 -Dev -Python "C:\Python312\python.exe"
```

安装脚本创建 `.venv/` 并准备 FFmpeg 工具。`scripts/media_tools.py` 是工具版本、下载地址和 SHA-256 的唯一来源，源码安装与发行构建共用它。安装不会覆盖已有非空工具。补齐本地工具：

```powershell
.\.venv\Scripts\python.exe -m scripts.prepare_tools
```

| 文件 | 用途 |
| --- | --- |
| `requirements.txt` | 完整运行依赖，包含 ASR、下载与 Web |
| `requirements-test.txt` | 默认测试用的轻量依赖，无需下载语音模型 |
| `requirements-dev.txt` | 组合运行、测试依赖，增加 PyInstaller |
| `requirements-gpu.txt` | 完整运行依赖加 Windows CUDA 库 |
| `constraints.txt` | 所有安装入口共用的直接依赖版本约束 |
| `pyproject.toml` | pytest 收集范围、导入路径和 marker 约定 |

`constraints.txt` 未锁定全部传递依赖，不是完整离线锁文件。更新依赖后需验证 Windows 和 Ubuntu CI，以及 Windows 发行包。当前应用使用源码入口或 PyInstaller 发布，不提供 wheel 安装流程。

## 统一检查

从根目录运行：

```powershell
.\.venv\Scripts\python.exe -m scripts.check
```

该入口按顺序运行 pytest、Node 前端回归和 Python 编译检查；CI 与构建脚本使用相同入口。pytest 默认只收集 `tests/`，不会递归扫描构建产物或用户数据。

本机 Skill 集成属于显式开启的检查，以免 CI 或另一位开发者的环境影响默认测试：

```powershell
.\.venv\Scripts\python.exe -m scripts.check --run-local
```

默认回归使用临时目录和模拟外部调用。真实音频、桌面、视频编码与打包依赖用独立 smoke 脚本验证：

```powershell
# 短暂播放低音量测试音，检查字幕窗、暂停、续播和跳转
.\.venv\Scripts\python.exe -m scripts.smoke_audio_player
# 无音频设备时使用模拟音频；这不代表真实设备播放验证
.\.venv\Scripts\python.exe -m scripts.smoke_audio_player --dummy-audio
# 导出并迁移播放器 ZIP，检查独立运行组件
.\.venv\Scripts\python.exe -m scripts.smoke_player_export
```

音频 smoke 报告保存在 `build/player-smoke/`。补齐工具后，应为已有任务重新生成播放器，让 ffplay/ffprobe 进入其 `assets/`。缺少 ffplay 时生成任务会明确失败。

## Windows 构建

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1 -Version dev
```

构建先运行统一检查，再通过 `scripts/release_assets.py` 准备经过校验的工具和许可证。`live_transcriber.spec` 打包应用、独立播放器、默认配置、Web 静态资源和词典；随后复制 README、docs 与许可证，执行隔离 smoke，最后生成 ZIP 和 SHA-256。

构建中间目录为 `build/<版本>/`，共享工具准备目录为 `build/release-assets/`，发布目录为 `dist/<版本>/`。省略版本参数时使用 `dev`。新增运行资源时同时维护 spec；不要依赖开发机 PATH、根目录未跟踪文件或个人配置。`scripts/smoke_release.py` 在独立数据目录和最小 PATH 下检查 CLI、Web、静态资源、ASR 导入、媒体工具、视频生成与播放器运行组件，不下载模型或调用付费分析 API。

Web 检查使用持久连接并完整读取响应；失败时保留 `build/smoke-release/` 下的服务日志和失败请求路径。

现有发布脚本会覆盖同名本地版本产物；本地验证请使用独立版本名。代码变更后必须重新构建，旧 `dist/` 程序不会自动更新。

## CI 与发布流程

- `.github/workflows/ci.yml`：Windows / Ubuntu、Python 3.10 / 3.12 的检查矩阵。
- `release.yml`：`v*` 标签触发 Windows 构建并创建草稿 Release；手动运行只上传构建附件。
- `publish-verified.yml`：显式运行后核验构建与 CI、草稿提交、ZIP 和校验和，再发布。

本地构建不代表已经发布。提交前先看 `git diff`，确认没有密钥、私有媒体、结果、模型或生成文件。模块职责和资源位置见 [工程结构](architecture.md)。
