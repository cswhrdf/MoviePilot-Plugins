# 插件市场

MoviePilot 插件仓库，目录结构、索引格式与文档体系均沿用
[MoviePilot 官方插件仓库](https://github.com/jxxghp/MoviePilot-Plugins)。

## 从这里开始

| 场景 | 文档 |
|---|---|
| 开发一个新的 V3 插件 | [MoviePilot 插件开发指南（V3）](docs/Plugin_Development.md) |
| 把 V2 插件迁移到 V3 | [V2 插件迁移到 V3](docs/V3_Plugin_Adaptation.md) |
| 维护仓库索引或发布版本 | [仓库与发布指南](docs/Repository_Guide.md) |
| 按具体功能查示例 | [常见问题](docs/FAQ.md) |
| 插件 API 返回与前端调用 | [插件 API 专题](docs/V3_API_Response_Adaptation.md) |
| 继续维护 V2 插件 | [V2 插件开发指南](docs/V2_Plugin_Development.md)（历史参考） |

当前开发目标是 V3。`docs/Plugin_Development.md` 是唯一完整主指南，覆盖目录、最小骨架、
生命周期、稳定 SDK、配置与数据、数据库事务、页面、事件、API、服务、测试和发布；
不要在《V3 插件迁移》《V2 插件开发》等多份文档之间自行拼接流程。

## 仓库负责什么

本仓库不是独立运行时：

- `MoviePilot` 负责插件加载、事件分发、API、服务、数据、工作流和 Agent 运行时。
- `MoviePilot-Frontend` 负责配置页、详情页、仪表板和 Vue 联邦组件渲染。
- 本仓库负责插件源码、市场索引、图标、测试、文档和发布流程。

## 当前目录

```text
MoviePilot-Plugins/
├── plugins.v3/          # V3 专用插件，新插件放这里
├── tests/v3/            # V3 插件测试
├── plugins.v2/          # V2 插件
├── package.v2.json      # V2 插件市场索引
├── plugins/             # v1 插件；当前为空
├── package.json         # v1 插件市场索引；当前为空对象
├── icons/               # 插件图标，全仓共享、扁平存放
├── docs/                # 开发、迁移、FAQ 和发布文档
├── tests/               # 全部插件单测，说明见 tests/README.md
└── .github/             # CI、Release 工作流与校验脚本
```

V3 添加插件时需同步插件目录、`package.v3.json` 条目与 `tests/v3/<plugin_id_lower>/`；
旧代索引中的同名条目声明 `"v3": false`，避免 V3 回退到旧合同实现。

## 插件清单

### [转移图片挂载文件](plugins.v3/transferimagesidecar/README.md)

在媒体整理/转移完成后，自动同步处理同目录图片与 NFO 文件  
在转移到目标目录后，继续检查与媒体文件同名的 poster、fanart 以及 .nfo，并按规则整理到目标位置，减少手动补充海报和艺术图的重复工作。

V3 实现位于 `plugins.v3/transferimagesidecar/`；`plugins.v2/transferimagesidecar/` 为历史实现，其索引条目已声明 `"v3": false`。

## 最重要的提交规则

- 插件目录名必须是插件主类名的小写形式，主类定义在目录的 `__init__.py`。
- 索引键是插件类名（如 `TransferImageSidecar`），不是目录名。
- `plugin_version`、索引 `version` 和最新 `history` 必须一致；版本历史按语义版本降序排列。
- `icon` 字段只写 `icons/` 下的文件名，大小写必须与磁盘一致。
- 新增类和方法需要补充说明职责的注释。
- V3 新代码优先使用 `app.sdk`，不要新增对宿主内部目录布局的无必要依赖。
- 宿主数据通过 Oper、Chain 或稳定 SDK 访问；数据库事务装饰器只用于插件自有表。
- 插件运行数据写入插件数据目录，不要写回源码目录。
- V3 第三方依赖写入插件 `pyproject.toml`，不提交插件 `uv.lock`；V1/V2 保留 `requirements.txt`。
- 测试放在仓库根 `tests/<vN>/<plugin_id>/`，不要放进插件源码目录。
- 提交前运行 Python 编译、版本门禁、相关测试和 `git diff --check`。

## 第三方插件仓库

第三方仓库建议 fork 本项目并保留相同目录和索引结构。MoviePilot 插件市场只读取
GitHub 仓库的 `main` 分支；仓库地址通过 `PLUGIN_MARKET` 配置，多个地址用逗号分隔。

请勿开发用于破解 MoviePilot 用户认证，或提供色情、赌博等违法违规内容的插件。

## 常用链接

- [完整 V3 开发指南](docs/Plugin_Development.md)
- [V2 插件迁移到 V3](docs/V3_Plugin_Adaptation.md)
- [插件 API 专题](docs/V3_API_Response_Adaptation.md)
- [仓库与发布指南](docs/Repository_Guide.md)
- [FAQ 索引](docs/FAQ.md)
- [插件仓测试说明](tests/README.md)
- [MoviePilot-Frontend V3 模块联邦指南](https://github.com/jxxghp/MoviePilot-Frontend/blob/v3/docs/module-federation-guide.md)