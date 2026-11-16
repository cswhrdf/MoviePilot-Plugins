# 订阅下载分类与标签

订阅触发自动下载时，按订阅目录规则为下载任务写入下载器的分类与标签，让自动下载的任务直接落到预期的分类与标签体系里。

当前版本：0.1.0（基于 MoviePilot3.2以上版本开发）

---

## 能力

- 监听 `DownloadAdded`，在任务创建后按目录规则补写属性：
  - 标签：qBittorrent、Transmission、rTorrent，均为追加合并语义，保留任务原有标签。
  - 分类：仅 qBittorrent（实例提供 `set_torrent_category` 时）；其它下载器只写标签，分类记为 `unsupported`。
- 规则匹配两级：目录规则 > 默认规则；命中空规则时停在当前层级，不会逐字段向下一级回落。
- 标签与任务既有标签、宿主系统标签（`TORRENT_TAG`）合并去重；`keep_system_tag` 关闭时移除系统标签。
- 配置页按宿主实时目录生成目录规则行，并提示完整键冲突与已失效目录键。
- 处理记录详情页展示时间、任务 hash、标题、目录键、规则层级、分类、标签、状态与原因，最多保留最近 200 条。

---

## 限制与注意事项

- 后置写入：只在 `DownloadAdded` 之后补写分类与标签，不参与下载器创建任务的参数生成；分类按规则覆盖（qBittorrent 语义），标签按追加合并。
- 只写属性，不搬目录：不移动、重命名、删除媒体文件，也不改变宿主订阅的搜索、过滤、媒体分类与整理流程。
- 分类仅 qBittorrent：其它下载器不写分类，避免误改；此时结果可能是 `unsupported`（部分不支持，非成功）。
- 空分类不写：规则里分类为空时不调用分类写入，避免把任务分类清空。
- 空媒体跳过：事件缺少媒体信息或媒体类型为空时直接跳过，不调用宿主目录匹配（宿主对空媒体会返回首个目录）。
- 保存路径二次确认：事件 `context.save_path` 可读时用宿主按保存路径复核目录，与目录配置不一致则跳过。
- 7 天去重：同一任务 hash 在 7 天内只处理一次；`failed` 不登记去重，可重试；`dry_run` 会登记去重。
- `dry_run`：只记录拟写入的分类与标签，不调用下载器，状态为 `dry_run` 并显式标注非成功。
- 与 `downloadsitetag` 类插件共存：本插件只追加标签、只覆盖分类；其它插件写入的标签不会被删除，但分类字段以最后写入者为准，建议避免多个插件同时写分类。
- 旧配置里的 `category_rules` 已废弃：加载时直接忽略，不参与匹配，也不会写回配置；配置页会提示该项不再使用，
  目录分类与标签请直接在各目录行中设置。

---

## 配置项

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 启用插件；关闭后不处理任何下载事件 |
| `trigger` | `subscribe` | `subscribe` 仅处理来源为 `Subscribe\|` 的下载，`all` 处理全部下载 |
| `apply_tags` | `true` | 是否写入标签 |
| `apply_category` | `true` | 是否写入分类 |
| `keep_system_tag` | `true` | 是否保留宿主系统标签 `TORRENT_TAG` |
| `dry_run` | `false` | 仅记录不写入 |
| `default_rule` | 空 | 未命中目录规则时使用的 `category` 与 `tags` |
| `directory_rules` | 空 | 以目录键为键的 `{category, tags}` 规则，配置页按宿主目录逐行生成 |

---

## 目录键与优先级

- 目录行按宿主目录 `priority` 升序排列，数值小者优先，同优先级保持宿主配置顺序。
- 规则键三级回退：`storage:download_path` 完整键 → 目录名 `name` → `#序号`。
- `#序号` 会随目录增删漂移，不建议长期依赖；调整宿主目录后请留意配置页的「已失效目录键」提示。
- 完整键重复时按 priority 升序首条生效，其余重复项在配置页标记为不生效，且不提供编辑入口。

---

## 状态与异常降级

- 记录状态：`ok`（成功）、`unsupported`（部分不支持，非成功）、`failed`（失败）、`skipped`（已跳过）、`dry_run`（仅记录未写入，非成功）。
- 写入失败按 2 秒、5 秒间隔重试，共 3 次尝试；只有 `failed` 会重试。
- 读不到任务原标签时不盲写覆盖（Transmission 先读后写），记 `failed` 并允许下次重试。
- 插件内所有异常都在事件处理器内收敛并记录，不向宿主事件总线抛出，不会中断订阅与下载。
- 插件数据（处理记录、去重表）读写失败时降级为内存态并记 warning，不影响宿主运行。

---

## 详情页

详情页是只读的处理记录区块。

- 记录按时间倒序展示，包含时间、任务 hash、标题、目录键、规则层级、分类、标签、状态与原因。
- 状态图例与状态分布合并展示，状态为 `ok`、`unsupported`、`failed`、`skipped`、`dry_run`。
- 页面不使用表单 `model`/`v-model`，也没有任何按钮或写操作，不读也不改下载器。

---

## 宿主依赖与版本下界

- 要求 MoviePilot `>=3.2.0`：该版本的 `DownloadAdded` 事件才提供当前形态的 `hash` 与 `context`（含 `media_info`、`save_path`），`app.sdk.services` 导出与系统标签配置键也稳定，低版本字段与导出不稳定。
- 宿主接口优先使用稳定面：`app.sdk.plugin`、`app.sdk.config`、`app.sdk.logging`、`app.sdk.events`、`app.sdk.services`。
- 例外依赖（已评审接受，不在 `app.sdk` 稳定面）：`app.application.directory.DirectoryHelper`（目录匹配与按保存路径复核）、`app.chain.download.DownloadChain`（下载器实例不可用时的回退写入通道）、`app.schemas.system.TransferDirectoryConf` 与 `app.schemas.types` 的数据结构和枚举。

---

## 安装与启用

1. 把 `plugins.v3/subscribedownloadattr/` 放入 MoviePilot 插件路径并重启，或在 `PLUGIN_AUTO_RELOAD=true` 下等待自动重载；
2. 在插件配置页开启「启用插件」；
3. 建议先开「仅记录不写入」，触发一次订阅下载，在「查看数据」中确认目录键、分类与标签符合预期后再关闭。
