# Transfer Image Sidecar

将与媒体文件同名前缀的图片（fanart / poster）和 nfo 一并转移的 V2 插件。

当前版本：0.1.0

本插件目标是：在文件整理/转移过程中，自动把与媒体文件同前缀的艺术图（fanart）、封面图（poster）以及对应的 .nfo 文件按规则一并整理到目标目录，减少手动复制与整理工作。

---

## 适合谁

- 希望在转移（整理）媒体文件时自动携带同前缀的艺术图与封面图的用户。
- 本实现目前面向本地文件系统（LocalStorage），若需要非本地存储（S3、rclone 等）需额外扩展。

---

## 行为概述

- 监听 TransferComplete 事件；在整理完成后根据事件数据把同前缀的图片与 nfo 一并复制/移动到目标目录。
- 图片文件匹配规则（严格模式）：
  - 命名为 `<stem><sep>fanart.<ext>` 或 `<stem><sep>poster.<ext>`。
  - sep 限定为 `-`、`_`、空格或 `.`。
  - 支持图片扩展名：.jpg, .jpeg, .png, .webp。
- 图片的整理方式随 transfer_type：
  - 若 transfer_type 为 move，则图片随之移动（shutil.move）。
  - 否则图片以 copy 方式复制到目标目录（shutil.copy2）。
- nfo 文件始终以复制方式（copy）整理到目标目录（无论 transfer_type）。

替换规则（启发式）：
- 若转移的某一季第 1 集（S01E01）包含 fanart，则把该 fanart 复制为该剧的 series-level fanart（父目录下的 fanart.*）。
- 对于每一季，使用该季第一集的 poster 作为季级封面（会复制为 season{N}-poster.ext，放在同一目录的启发式位置）。
- 每一集的封面（poster）会保持为当前集的 poster（直接复制到目标集目录）。
- 以上替换采用简单目录启发式（episode -> parent -> series）；如需精确目标位置，请在配置或扩展中调整。

---

## 快速上手

1. 将目录 `plugins.v2/transfer_image_sidecar` 放入 MoviePilot 的插件路径并重启 MoviePilot，或在 DEV 模式下启用自动重载。
2. 在插件配置页启用插件（enabled = true）。
3. 发起一次整理，检查目标目录是否已把同名前缀的 fanart/poster/nfo 一并整理进来。

---

## 配置项（示例）

本插件目前只有简单启用开关（可在插件 init 中扩展更多配置）：

- enabled: true/false（默认 true）

未来可能加入的配置项（可按需扩展）：
- image_exts: 允许的图片扩展名
- enable_series_fanart_replace: 是否启用 S01E01->series fanart 替换（默认启用）
- season_poster_pattern: 季级 poster 命名格式

---

## 限制与注意事项

- 当前实现基于本地文件系统（shutil）。对非本地存储（如 S3、FTP、远端 NAS 支持的 StorageBase）尚未实现存储后端抽象，复杂场景需要将逻辑迁移到 StorageBase 的 source_oper/target_oper 接口。
- 替换规则使用目录层级的简单启发式，若你的库布局特殊（例如采用自定义的季目录或外置 metadata 存放位置），需要调整代码的目标路径逻辑。
- 插件在 TransferComplete 事件后处理；如果需要在转移流程的早期（transfer() 阶段）执行以保证并发或事务一致性，可把处理迁移到 transfer 钩子。

---

## 实例

场景：
- 源目录：
  - MyShow.S01E01.mkv
  - MyShow.S01E01-fanart.jpg
  - MyShow.S01E01-poster.jpg
  - MyShow.S01E01.nfo
- 整理后目标目录：
  - MyShow.S01E01.mkv
  - MyShow.S01E01-fanart.jpg (copy/move)
  - MyShow.S01E01-poster.jpg (copy/move)
  - MyShow.S01E01.nfo (copied)
- 若存在 S01E01-fanart.jpg，则插件还会把它复制为上级 series 的 fanart.jpg（启发式位置）。

---

## 进一步阅读与扩展

- 插件实现文件：`plugins.v2/transfer_image_sidecar/__init__.py`
- 若需对接非本地存储，请参阅其他插件如何使用 StorageBase（例如 `plugins.v2/blurayremux` 中的 source_oper/target_oper 使用样例）。

如果你希望我把插件改为：
- 在 transfer() 阶段处理（转移期间直接操作 source_oper/target_oper），或
- 支持 StorageBase（非本地存储），

请告诉我需要支持的 storage 后端类型（例如 S3、rclone、NAS SMB），我可以据此扩展实现。