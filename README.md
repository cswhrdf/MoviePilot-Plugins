# MoviePilot 插件仓库（个人 fork）

本仓库 fork 自 [官方插件仓库](https://github.com/jxxghp/MoviePilot-Plugins)，用于维护自用插件并作为自建插件市场源；相对上游只保留自有插件，其余插件均已移除。

## 插件清单

### [转移图片挂载文件](plugins.v3/transferimagesidecar/README.md)

在媒体整理/转移完成后，自动同步处理同目录图片与 NFO 文件；转移到目标目录后，继续检查与媒体文件同名的 poster、fanart 以及 .nfo，并按规则整理到目标位置，减少手动补充海报和艺术图的重复工作。

### [订阅下载分类与标签](plugins.v3/subscribedownloadattr/README.md)

订阅触发自动下载时，按订阅目录规则为下载任务写入下载器的分类与标签；标签支持 qBittorrent、Transmission、rTorrent，分类仅 qBittorrent，并提供 dry_run 预演、7 天去重与处理记录详情页。

## 作为插件市场使用

在 MoviePilot 的 `PLUGIN_MARKET` 中填入本仓库地址：

```text
https://github.com/cswhrdf/MoviePilot-Plugins
```


## 来源与许可

本仓库基于 [官方插件仓库](https://github.com/jxxghp/MoviePilot-Plugins)，目录结构与索引格式均沿用上游；`docs/` 为上游文档快照，供对照开发规范使用，不是本仓库日常维护的内容。上游内容版权归原作者所有，详见 [LICENSE](LICENSE)。

贡献与开发指南见 [CONTRIBUTING.md](CONTRIBUTING.md)；自动化代理的仓库约束见 [AGENTS.md](AGENTS.md)。
