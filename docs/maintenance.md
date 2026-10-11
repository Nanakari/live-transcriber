# 文件整理与维护

## 区分源文件、结果与可重建文件

Git 跟踪 `app/`、`scripts/`、`tests/`、`docs/`、词典与项目配置。`.venv/`、`tools/` 中的工具、`build/`、`dist/`、缓存和用户结果属于本机文件，已被忽略规则隔离。

| 位置 | 内容与处理方式 |
| --- | --- |
| `outputs/media/` | 媒体和用户结果，通过结果页按任务管理 |
| `outputs/cache/analysis/` | 恢复分析需要的缓存；清理会失去复用能力 |
| `outputs/_staging/` | 未完成媒体和失败诊断；先确认对应任务状态 |
| `outputs/cache/player-runtime/` | 可由 PyInstaller 重建的播放器组件；保留可加速再次导出 |
| `.venv/`、`tools/` | 当前源码运行依赖；通过 setup 和工具准备脚本重建 |
| `build/` | 构建／验收产物；旧记录可能仍有追溯价值 |
| `dist/<版本>/` | 可运行的发行程序；仅保留正在使用或需要回归的版本 |
| `archive/` | 历史程序、旧发行包和迁移前备份，按需要保留 |

文件相同或很旧不足以证明可以删除：播放器复制的工具用于独立运行，原始稿与修复稿用于追溯，不同分析批次用于比较。

## 整理错放的开发产物

`organize_workspace` 只处理以下已知位置：

| 原位置 | 目标位置 |
| --- | --- |
| `outputs/qa/` | `build/qa/` |
| `outputs/release/` | `archive/releases/legacy-outputs/` |
| `output/` | `build/legacy-output/` |

```powershell
.\.venv\Scripts\python.exe -m scripts.organize_workspace
.\.venv\Scripts\python.exe -m scripts.organize_workspace --apply
```

默认只预览。应用时拒绝越界路径、符号链接／junction 和已存在的目标，不合并覆盖；迁移前后验证每个文件的 SHA-256。完整清单保存在 `build/maintenance/organization-*.json`，记录原位置、目标、文件大小和哈希。脚本可重复运行，已迁移项目不会再次操作。

迁移只改变位置，保留全部文件内容。需要还原时，在目标与原位置没有新改动的前提下，按清单反向移动并核对哈希。

## 整理旧用户数据

先结束处理任务并关闭服务，再预览导入范围：

```powershell
.\.venv\Scripts\python.exe -m scripts.consolidate_data --import-from "$env:LOCALAPPDATA\LiveTranscriber"
.\.venv\Scripts\python.exe -m scripts.consolidate_data --import-from "$env:LOCALAPPDATA\LiveTranscriber" --apply
```

此兼容脚本分别读取当前数据目录和导入数据目录的 `app.output_dir`，支持相对路径与绝对路径。`--root` 指定目标数据根目录，`--import-from` 指定旧数据根目录，而不是其 `outputs/` 子目录。只整理配置指向的目录，不会自行搬迁切换配置前留下的其他位置。

仅相同内容的冲突文件去重，不同内容保留独立副本；修复旧 JSON 文件引用前在 `archive/migration/<批次>/outputs/` 留存备份（即使结果目录位于项目外）。操作记录位于配置输出目录的 `logs/maintenance/`。脚本在移动前检查所有路径，拒绝相互嵌套的输出根目录、符号链接和 junction。输出目录不能包含数据根目录本身，以免将源码和配置误当成结果。不要将无关目录作为导入源。

`app.migrate_outputs` 是更早的平铺媒体目录迁移入口，只处理配置输出根目录下的 `audio/`、`transcripts/`、`analysis/`、`previews/` 等旧位置；已经位于 `media/` 的任务和元数据引用的外部原始媒体不会再次移动。

## 2026-10-11 结构整理

- 将结果扫描从路由移到 `artifact_catalog`，移除数据迁移对 Web 的反向依赖。
- 拆出 CLI 参数定义、预览字幕处理，保留既有入口和辅助导入。
- 将两份默认配置归并到 `app/defaults.yaml`，随发行包一起提供。
- 删除 5 个无调用的旧辅助函数和失效的局部状态；复用重复的分析结果读取逻辑。
- 统一本地、CI、构建检查入口，显式隔离本机 Skill 测试。
- 将 README 中反复追加的使用、开发和维护内容移入对应文档。
- 修复旧数据整理对自定义输出路径的忽略，以及媒体迁移误移动外部原始音频、重复移动已归档任务的问题。

本次没有把媒体、历史分析、模型、个人配置或迁移备份当成冗余删除。每次本地整理以维护清单为准。
