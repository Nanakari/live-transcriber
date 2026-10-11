# 工程结构与约定

本项目是本机桌面／Web 应用，使用 `main.py` 作为源码和 PyInstaller 的共同入口。代码、随包资源、用户数据、开发产物分开管理。

## 目录职责

```text
live-transcriber/
  main.py                    入口；保留现有启动命令
  app/                       应用实现与运行资源
    cli.py                   转写、分析、预览的流程编排
    cli_args.py              命令行参数声明
    config.py                数据路径、配置覆盖和工具定位
    defaults.yaml            源码与发行包共用的默认配置
    artifact_catalog.py      只读结果扫描与旧目录兼容
    output_layout.py          媒体目录布局及 README 索引
    analysis/                分块、模型请求、校验、修复、缓存、分析导出
    exporters/               原始转写 JSON／Markdown／SRT 导出
    preview.py               音频／视频生成与播放器组装
    preview_subtitles.py     分析数据与字幕关联、时间轴及学习字幕
    subtitles.py             显示字幕分段、合并和 SRT／ASS 渲染
    preview_state.py         播放器与分析版本的一致性
    player_package.py        可独立播放的 ZIP 组装
    floating_overlay.py      从模板生成播放器与启动脚本
    assets/                  原创封面、可独立运行的播放器模板
    web/                     本机 HTTP、任务管理和文件访问边界
      templates/             两种布局的 HTML
      static/                相应 JS 与 CSS
  dictionaries/              随包默认词典
  scripts/                   开发、构建、smoke 和维护命令
  tests/                     回归测试；本机集成标记为 local
  docs/                      使用、开发、结构与维护说明
  .github/                   CI、依赖更新与发行流程
  tools/                     本机下载的媒体工具（生成文件）
  build/                     构建中间文件、验收、开发日志（忽略）
  dist/                      按版本输出的发行包（忽略）
  outputs/                   默认用户结果与缓存（忽略，可配置）
  archive/                   历史程序、迁移备份、旧发行文件（忽略）
```

根目录保留入口、配置示例、依赖清单、PyInstaller spec、README 和项目治理文件。这些都是项目级文件。Python 包从 `app/` 导入；模板、词典和启动器都按明确的资源根目录定位。

## 依赖方向

Web 路由接收请求并调用任务管理、领域模块；CLI 编排下载、ASR、分析、预览；结果扫描与数据迁移直接依赖文件布局和配置。领域模块不得反向导入 `app.web.routes`。HTTP 错误、响应对象和删除授权留在 Web 层。

`artifact_catalog` 不启动作业、不删除文件，也不初始化服务器。旧的路由辅助名称仍通过显式导入兼容；新代码调用结果目录模块的公共函数。

`preview_subtitles` 负责分析元数据与 SRT 的关联，`subtitles` 负责显示算法，`preview` 负责媒体编码和组装。原有 `app.preview` 辅助函数导入仍可使用，单一实现位于对应模块。

## 需要分别保留的文件

- `analysis/exporters.py` 输出翻译、学习和分析结果；`exporters/` 输出 ASR 转写文档。两者的数据模型和职责不同。
- `assets/floating_subtitle_overlay.pyw` 必须能在导出的播放器中独立运行；`floating_overlay.py` 是生成脚本和启动器的应用代码。模板不依赖 `app` 包。
- `classic.*` 与 `workbench.*` 是产品保留的两套界面，两者共用后端；旧版拥有更多高级操作。
- `start.bat` 是正式启动入口；`启动新版.bat` 是兼容快捷入口，仅转发到它。
- `consolidate_data.py` 整理旧结果位置／修复已迁移数据引用；`organize_workspace.py` 只整理错放的开发文件。两者都有明确边界。
- 运行、轻量测试、开发、GPU 四份依赖入口分别服务不同安装场景，版本统一受 `constraints.txt` 管理。

## 后续改动约定

1. 新功能放入已有职责模块；跨流程复用逻辑放在不依赖 HTTP 的应用模块。
2. 默认配置只改 `app/defaults.yaml`；个人配置只改未跟踪的 `config.local.yaml`。保留对旧 `config.yaml` 的覆盖读取。
3. 新的用户结果位置通过 `output_root()` 和 `output_layout` 确定；开发验证产物放到 `build/`。
4. 随包资源更新同时修改 spec，并执行隔离发行 smoke。
5. 修复真实行为时添加回归；涉及桌面、网络或本机安装的检查显式隔离。
6. 公共命令、API、结果目录或配置契约有变更时，更新对应文档及兼容说明。

模块按职责组织，不用固定行数判定合格。`analysis/analyzer.py` 仍承载复杂的校验与恢复流程；后续扩展应沿分块执行、覆盖校验等边界继续拆分，并保留已有失败恢复测试。
