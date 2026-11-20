# AGENTS.md

本文件面向自动化代理，只写从代码里看不出来、又最容易踩的硬约束；完整贡献指南见 [CONTRIBUTING.md](CONTRIBUTING.md)，插件开发规范主入口是 [docs/Plugin_Development.md](docs/Plugin_Development.md)。

## 仓库边界

- 上游是 [jxxghp/MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins)；本仓库是裁剪版，只保留自有插件源码与索引，只提交自有插件改动。
- `docs/` 是上游文档快照，只整体同步，不夹带进插件改动；`docs/Plugin_Development.md` 是唯一完整主指南，`V3_Plugin_Adaptation.md` 只讲迁移，`V2_Plugin_Development.md` 仅供维护历史插件。
- `plugins/` 为空是正常状态，不要用空目录反推结构缺失，也不要补写占位插件或脚手架。
- 每个插件的新增或修改都必须开独立新分支进行；写完并自检通过后，只合入**自己仓库**的 `main` 分支；严禁合入或推送到 fork 的上游原仓（`jxxghp/MoviePilot-Plugins`）。
- 创建 PR 时，PR 描述必须按 `.github/pull_request_template.md` 模板填写，并勾选实际执行过的检查项。

## 插件改动同步项

- 五项缺一即视为未完成：插件源码 `plugins.{vN}/<plugin_id>/__init__.py`（目录名为主类名小写）、索引条目（v1/v2/v3 分别为 `package.json`/`package.v2.json`/`package.v3.json`，键为插件类名而非目录名）、`icons/` 图标文件名（大小写与磁盘一致）、根 `README.md` 插件清单、`tests/vN/<plugin_id>/` 用例。
- 版本硬约束：`plugin_version` = 索引 `version` = `history` 首项；`history` 严格语义版本降序且只追加；旧代同名条目 `"v3": false`；V3 副本版本为 `{旧代主版本+1}.0.0`。细则见 [CONTRIBUTING.md](CONTRIBUTING.md)。
- `package*.json` 只做局部插入/替换，禁止整体格式化或重排键顺序。
- 本地是 Windows 开发、Linux 容器运行：换行用 LF，路径分隔符与文件名大小写按 Linux 处理。
- 仓库可能在 UNC 共享路径上：把虚拟环境指到本地盘，配置见 CONTRIBUTING 的「环境准备」。

## 宿主钩子判定

`has_page`（前端是否出现「查看数据」入口）由宿主 `ObjectUtils.check_method` 判定，规则是**方法体是否全部由占位语句组成**（`pass`、纯 docstring、`...`、`raise NotImplementedError`），与返回值无关：

- 没有详情页的插件，`get_page` 必须保持 `docstring + pass`。
- 写成 `return None` 会被视为「已实现」，前端就会放出数据页入口，点开只显示「此插件没有详情页面」。

同一条规则也适用于 `get_command`、`get_api`、`get_module` 等其他钩子：返回 `[]`/`{}` 属于已实现，只有占位方法体才算未实现。

## 测试硬规则

- 用例只放 `tests/vN/<plugin_id>/`，文件名 `test_*.py`，不要放进插件目录；`pyproject.toml` 的 `[tool.pytest.ini_options]` 是唯一权威配置，不要新建 `pytest.ini`。
- 各代必须在独立会话中运行，否则同名插件包会冲突；用路径驱动决定注入哪一代，命令见 [CONTRIBUTING.md](CONTRIBUTING.md) 与 [tests/README.md](tests/README.md)。
- 运行测试需要 MoviePilot 后端位于本仓库同级目录，或用 `MOVIEPILOT_BACKEND_PATH` 指定。
- 插件自身告警要改代码，不要加进 `filterwarnings` 白名单。

## 代码校验

- 新增或修改 Python 文件后必须自己跑过 `uv run ruff check .`、`uv run ruff format --check .`、`uv run pyright`；`pyproject.toml` 的 `[tool.ruff]` 与 pyright 配置是唯一权威风格与类型配置。
- 中文 docstring 首行以 ASCII `.` 结尾，写成全角 `。` 会报 `D400`/`D415`；禁用 `ruff check --fix --unsafe-fixes` 修它（会产出 `。.`）。
- 不要用编辑器扩展自带的 ruff，版本可能与 `uv.lock` 不一致。
- pyright 中宿主 `app.*` 一律视为 `Any`（`reportMissingImports` 已关闭）；不要造假 stub 或强转类型去消除 `Unknown`。
- 改过 `package*.json` 或插件版本后，先本地跑版本预检再推送；完整自检清单见 CONTRIBUTING 的「提交前自检」。

## 文档

- 根 `README.md` 保留插件清单、市场使用、来源与许可等用户向结构，只增补修正，不无必要重写；`docs/` 是上游快照，不当作本仓库日常维护内容，`docs/faq/` 命名与索引规则只在同步上游快照时适用。

## 参考

- 完整贡献指南：[CONTRIBUTING.md](CONTRIBUTING.md)
- V3 插件开发（唯一完整主指南）：[docs/Plugin_Development.md](docs/Plugin_Development.md)
- 单测说明：[tests/README.md](tests/README.md)
