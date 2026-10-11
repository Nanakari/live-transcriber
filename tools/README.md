# 本机媒体工具

这里存放 `scripts/prepare_tools.py` 下载的 ffmpeg、ffplay、ffprobe 和许可证。
二进制与下载生成的 `licenses/` 不提交 Git；版本、来源与校验和由
`scripts/media_tools.py` 统一定义。

从项目根目录补齐工具：

```powershell
.\.venv\Scripts\python.exe -m scripts.prepare_tools
```

发行构建使用 `build/release-assets/` 中独立准备的工具和许可证。
本目录不是用户媒体或构建日志的存放位置。
