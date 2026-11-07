# Transfer Image Sidecar

将与媒体文件同名前缀的图片（fanart / poster）和 nfo 一并转移的 V3 插件。

当前版本：1.0.0（要求 MoviePilot `>=3.0.0`）

本插件目标是：在文件整理/转移过程中，自动把与媒体文件同前缀的艺术图（fanart）、封面图（poster）以及对应的 .nfo 文件按规则一并整理到目标目录，减少手动复制与整理工作。

---

## 适合谁

- 希望在转移（整理）媒体文件时自动携带同前缀的艺术图与封面图的用户。
- 本实现目前面向本地文件系统（LocalStorage），若需要非本地存储（S3、rclone 等）需额外扩展。

---

## 行为概述

- 监听 `TransferComplete` 事件；在整理完成后按配置的延迟窗口把同前缀的图片与 nfo 一并复制/移动到目标目录。
- 同一批次内完全重复的事件只入队一次，但会刷新等待窗口，避免重复处理。
- 图片文件匹配规则：命名为 `<stem><sep><后缀名>.<ext>`，`sep` 限定为 `-`、`_`、空格或 `.`，支持 `.jpg`、`.jpeg`、`.png`、`.webp`。
- 图片的整理方式随 `transfer_type`：`move` 随之移动，`hardlink` / `symlink` 建链，其余按 `copy` 复制。
- nfo 文件始终解析 XML 后写入目标目录，并按「NFO 删除标签」配置过滤指定标签。
- 「NFO 删除标签」为多选 chips：点选常用候选或手动输入后回车添加，标签数量多时也不会挤在单行里。

替换规则：

- 对配置指定的季/集（默认 S01E01）提升 `poster` / `fanart` 到媒体级目录，可用「启用媒体海报转移」关闭。
- 每季使用配置集（默认第 1 集）的 poster 作为季级封面，输出 `<目录>/poster.ext` 与 `seasonXX-poster.ext`，可用「启用转移季海报」关闭。

---

## 快速上手

1. 把 `plugins.v3/transferimagesidecar/` 放入 MoviePilot 的插件路径并重启，或在 `PLUGIN_AUTO_RELOAD=true` 下等待自动重载。
2. 在插件配置页启用插件。
3. 发起一次整理，检查目标目录是否已把同名前缀的 fanart/poster/nfo 一并整理进来。

---

## 配置项

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 启用插件 |
| `delay_seconds` | `20` | 转移完成后等待刮削/整理的延迟秒数 |
| `enable_first_episode_promotion` | `true` | 是否把指定季/集的 poster/fanart 提升到媒体目录 |
| `media_poster_season` | `1` | 媒体级海报来源季 |
| `media_poster_episode` | `1` | 媒体级海报来源集 |
| `generate_season_posters` | `true` | 是否生成季级海报 |
| `season_poster_episode` | `1` | 季海报来源集 |
| `poster_asset_name` | `poster` | 集封面匹配后缀名 |
| `fanart_asset_name` | `fanart` | 集艺术图匹配后缀名 |
| `nfo_remove_tags` | 空 | 需从 NFO 中删除的 XML 标签名；配置页为多选 chips，可点选常用候选或自行输入，留空表示不删除 |

---

## 限制与注意事项

- 当前实现基于本地文件系统。对非本地存储（S3、FTP、远端 NAS）尚未抽象存储后端，复杂场景需迁移到 `StorageBase` 的 `source_oper` / `target_oper` 接口。
- 替换规则使用目录层级的简单启发式，若库布局特殊（自定义季目录或外置 metadata 位置），需要调整目标路径逻辑。
- 处理发生在 `TransferComplete` 之后；若需在 `transfer()` 阶段保证并发或事务一致性，应改为该钩子实现。

---

## 实例

源目录：

```text
MyShow.S01E01.mkv
MyShow.S01E01-fanart.jpg
MyShow.S01E01-poster.jpg
MyShow.S01E01.nfo
```

整理后目标目录：

```text
MyShow.S01E01.mkv
MyShow.S01E01-fanart.jpg   # copy / move / hardlink / symlink
MyShow.S01E01-poster.jpg   # copy / move / hardlink / symlink
MyShow.S01E01.nfo          # 解析后按标签配置过滤写入
```

若命中所配置的季/集，还会把 poster/fanart 写入媒体级 `poster.jpg` / `fanart.jpg`。

---

## 迁移说明

本插件由 V2 实现迁移而来，版本从 `0.1.3` 跃迁到 `1.0.0`：

- 宿主接口改用 `app.sdk.events`、`app.sdk.logging`、`app.sdk.plugin` 稳定入口。
- 修复「启用媒体海报转移」开关未生效的问题。
- `init_plugin()` 与 `stop_service()` 会取消延迟计时器并清空待处理队列，配置重载不再残留线程。
- 事件去重逻辑从类方法提取为模块级函数，避免分身实例重命名类时硬编码类名。
- 「NFO 删除标签」从逗号分隔文本框改为 chips 多选，并继续兼容历史字符串配置。

V2 历史实现保留在 `plugins.v2/transferimagesidecar/`，其索引条目声明 `"v3": false`，V3 宿主只加载本实现。
