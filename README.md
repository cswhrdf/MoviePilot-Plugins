# MoviePilot 插件仓库（个人 fork）

本仓库 fork 自 [官方插件仓库](https://github.com/jxxghp/MoviePilot-Plugins)，用于维护自用插件，
并作为自建插件市场源。相对上游只保留自有插件，其余插件均已移除。

## 与上游的关系

| 项 | 说明 |
|---|---|
| 上游 | [jxxghp/MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins)，插件市场与开发规范的权威来源 |
| 本仓库 | 只保留自有插件的源码与索引，目录结构和索引格式沿用上游 |
| `docs/` | 上游文档快照，供对照开发规范使用，不是本仓库要维护的内容 |
| 提交范围 | 只提交自有插件的改动；上游文档整体同步时单独提交 |

## 插件清单

### [转移图片挂载文件](plugins.v3/transferimagesidecar/README.md)

在媒体整理/转移完成后，自动同步处理同目录图片与 NFO 文件  
在转移到目标目录后，继续检查与媒体文件同名的 poster、fanart 以及 .nfo，并按规则整理到目标位置，减少手动补充海报和艺术图的重复工作。

V3 实现位于 `plugins.v3/transferimagesidecar/`；`plugins.v2/transferimagesidecar/` 为历史实现，其索引条目已声明 `"v3": false`。

## 作为插件市场使用

在 MoviePilot 的 `PLUGIN_MARKET` 中填入本仓库地址即可，多个地址用英文逗号分隔：

```text
https://github.com/cswhrdf/MoviePilot-Plugins
```

插件市场只读取仓库的 `main` 分支，因此改动需要合并进 `main` 才会生效。

## 仓库结构

```text
MoviePilot-Plugins/
├── plugins.v3/              # V3 插件源码（当前实现）
├── plugins.v2/              # V2 插件源码（历史实现，供旧宿主使用）
├── plugins/                 # v1 插件目录；当前为空
├── package.v3.json          # V3 插件市场索引
├── package.v2.json          # V2 插件市场索引
├── package.json             # v1 插件市场索引；当前为空对象
├── tests/v3/<plugin_id>/    # 插件单测，按代与插件 ID 分目录
├── icons/                   # 插件图标，全仓共享、扁平存放
├── docs/                    # 上游文档快照
├── pyproject.toml           # 开发依赖与 ruff / pyright / pytest 配置
├── uv.lock                  # 开发依赖锁定
├── AGENTS.md                # 仓库约定（提交、版本、代码风格）
└── .github/                 # CI 工作流与校验脚本
```

新增插件时，插件目录、索引条目、`tests/v3/<plugin_id_lower>/` 与根 README 的插件清单要一起改；
旧代索引中的同名条目声明 `"v3": false`，避免 V3 回退到旧合同实现。

## 开发与校验

开发依赖在 `pyproject.toml` 的 `[dependency-groups] dev` 中，安装：

```powershell
uv sync
```

提交前依次执行：

```powershell
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

单测要求 MoviePilot 后端位于本仓库同级目录（或通过 `MOVIEPILOT_BACKEND_PATH` 指定），
且各代必须分会话运行：

```powershell
uv run pytest tests/v3
```

CI 会重跑 ruff 与 pyright，并执行插件版本门禁（`plugin-gate.yml` 校验 `plugin_version` 与索引 `version` 是否一致）。

> 若仓库位于 UNC 共享路径，pyright 会因 Node 无法解析 UNC 路径而失败，把虚拟环境指到本地盘即可：
> `$env:UV_PROJECT_ENVIRONMENT = "$env:LOCALAPPDATA\mpp-venv"`。

## 版本规则

- 插件目录名必须是插件主类名的小写形式，主类定义在目录的 `__init__.py`。
- 索引键是插件类名（如 `TransferImageSidecar`），不是目录名。
- `plugin_version`、索引 `version` 和 `history` 首项必须一致，版本历史按语义版本降序排列。
- V3 专用实现的大版本等于旧代主版本加一（如 `0.1.3 -> 1.0.0`），由 `check_plugin_versions.py` 硬校验。
- `icon` 只写 `icons/` 下的文件名，大小写必须与磁盘一致。

其余提交与代码风格约定见 [AGENTS.md](AGENTS.md)。

## 文档索引

`docs/` 是上游文档快照，用于对照开发规范：

| 场景 | 文档 |
|---|---|
| 开发 V3 插件（唯一完整主指南） | [MoviePilot 插件开发指南（V3）](docs/Plugin_Development.md) |
| V2 插件迁移到 V3 | [V2 插件迁移到 V3](docs/V3_Plugin_Adaptation.md) |
| 插件 API 返回与前端调用 | [插件 API 专题](docs/V3_API_Response_Adaptation.md) |
| 仓库索引与发布 | [仓库与发布指南](docs/Repository_Guide.md) |
| 按具体功能查示例 | [FAQ 索引](docs/FAQ.md) |
| 单测说明 | [插件仓测试说明](tests/README.md) |

## 相关仓库

- [MoviePilot](https://github.com/jxxghp/MoviePilot)：负责插件加载、事件分发、API、服务与数据。
- [MoviePilot-Frontend](https://github.com/jxxghp/MoviePilot-Frontend)：负责配置页、详情页与联邦组件渲染。

## 致谢

目录结构、索引格式与 `docs/` 内容均来自 [官方插件仓库](https://github.com/jxxghp/MoviePilot-Plugins)，
版权归原作者所有，详见 [LICENSE](LICENSE)。