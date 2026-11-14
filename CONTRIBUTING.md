# 贡献与开发指南

本文件面向人类贡献者：说明仓库结构、开发环境、插件新增与修改流程、版本与索引契约、测试、代码风格以及提交前自检。自动化代理的专用硬约束见 [AGENTS.md](AGENTS.md)，插件开发规范的权威内容在上游 `docs/Plugin_Development.md`。

## 仓库与上游

- 上游是 [jxxghp/MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins)，插件市场与开发规范的权威来源。
- 本仓库是上游的裁剪版：只保留自有插件的源码与索引，其余插件均已移除；目录结构与索引格式沿用上游，方便继续对照上游规范。
- 提交范围：一次提交只处理自有插件的改动；上游文档整体同步时单独提交，不要混进插件改动。
- [MoviePilot](https://github.com/jxxghp/MoviePilot) 提供插件加载、事件分发、API、服务与数据；[MoviePilot-Frontend](https://github.com/jxxghp/MoviePilot-Frontend) 负责配置页、详情页与联邦组件渲染；排查行为差异时先判断问题落在哪一侧。
- `docs/` 是上游文档快照，用来对照开发规范，不是本仓库另行维护的指南体系。
- 对照规范以 `docs/Plugin_Development.md` 为主线：它是唯一完整主指南；`docs/V3_Plugin_Adaptation.md` 只讲从 V2 迁移到 V3；`docs/V2_Plugin_Development.md` 仅供维护历史插件参考。

## 目录结构

| 路径 | 用途 |
|---|---|
| `plugins/` | v1 插件源码，索引为 `package.json`；当前为空对象 |
| `plugins.v2/` | v2 插件源码，索引为 `package.v2.json`；当前只保留 `plugins.v2/transferimagesidecar/` 历史实现 |
| `plugins.v3/` | v3 插件源码，索引为 `package.v3.json`；当前自有实现在 `plugins.v3/transferimagesidecar/` |
| `icons/` | 全仓库共享图标库，扁平存放，不按插件分子目录 |
| `docs/` | 上游文档快照，供对照开发规范；`docs/faq/` 为 01–22 编号的功能集成示例，`docs/FAQ.md` 为索引入口 |
| `tests/` | pytest 用例，按代分目录 `tests/<vN>/<plugin_id>/`；`run.py` 为全量分代入口，`conftest.py` 按本次目标选择插件代际，`_bootstrap.py` 负责把后端加入 `sys.path` |
| `pyproject.toml` | 开发依赖（`[dependency-groups] dev`）与 ruff / pyright / pytest 配置 |
| `uv.lock` | 开发依赖锁定 |
| `.github/` | CI 工作流（`lint.yml`、`plugin-gate.yml`、`release.yml` 等）与版本门禁脚本 `.github/scripts/check_plugin_versions.py` |
| `.githooks/pre-push` | 本地推送前的索引与版本预检钩子 |
| `README.md` | 用户向说明：插件清单、作为插件市场使用、来源与许可 |
| `CONTRIBUTING.md` | 本文件：人类贡献者的开发、测试与提交指南 |
| `AGENTS.md` | 面向自动化代理的仓库硬约束清单 |

`plugins/` 当前为空属于正常状态，**不要用空目录反推结构缺失**，也不要在其中补写占位插件或脚手架。

## 环境准备

- 开发依赖清单在 `pyproject.toml` 的 `[dependency-groups] dev`，锁定版本在 `uv.lock`，安装用 `uv sync`；本仓库命令统一以 `uv run` 执行，避免用到别的环境里的工具版本。
- 本地开发环境通常是 Windows，但插件运行于 Linux 容器：换行一律用 LF，路径分隔符与大小写按 Linux 处理。
- 仓库可能位于 UNC 共享路径上：此时 `uv sync` 只能把 wheel 整份复制进 `.venv`（跨文件系统无法硬链接，安装很慢），pyright 也会因 Node 无法解析 UNC 模块路径直接报 `EISDIR: lstat 'UNC'` 退出；把虚拟环境指到本地盘可同时解决两者：

```powershell
$env:UV_PROJECT_ENVIRONMENT = "$env:LOCALAPPDATA\mpp-venv"
```

## 新增或修改插件

一次插件改动必须同时覆盖下面五处，缺一即视为未完成：

1. **插件源码**：`plugins.{vN}/<plugin_id>/__init__.py`（可另含 `README.md`）；目录名必须是主类名的小写形式，类 `TransferImageSidecar` 对应目录 `transferimagesidecar`。
2. **索引条目**：v1 用 `package.json`、v2 用 `package.v2.json`、v3 用 `package.v3.json`；键是插件类名（如 `TransferImageSidecar`），不是目录名。
3. **图标**：条目的 `icon` 只写 `icons/` 下的文件名（如 `movie.jpg`），不要带目录前缀；文件名大小写必须与磁盘完全一致，部署环境为 Linux，大小写敏感。
4. **根 `README.md`**：在「插件清单」小节加或改一条，指向该插件自己的 `README.md` 相对路径，并附一句话描述。
5. **测试**：在 `tests/v3/<plugin_id>/` 或 `tests/v2/<plugin_id>/` 下补用例；用例放仓库测试目录，不要放进插件目录。

## 版本与索引

- `plugin_version`（插件类级属性）、索引条目 `version` 与 `history` 首项三者必须一致。
- `history` 是历史记录：按语义版本严格降序排列，新版本只追加，不改写已有条目。
- 旧代索引中的同名条目声明 `"v3": false`（如 `package.v2.json` 的 `TransferImageSidecar`），避免 V3 回退到旧合同实现。
- V3 副本的版本必须为 `{旧代主版本+1}.0.0`，例如旧代 `0.1.3` 对应 V3 `1.0.0`。
- `package*.json` 是本仓库最大的 diff 敏感文件：只做局部插入或替换，不要整体格式化、重排键顺序或改缩进风格。
- `.github/scripts/check_plugin_versions.py` 校验上述契约：V3 索引中的每一条都会校验（语义版本、`history` 首项与降序，以及 V3 发布约定中的版本跃迁与 `history` 顺序）；旧代索引只校验 `release: true` 的条目，未发布条目不参与校验。
- `.github/workflows/release.yml` 在生成 tag 与资产名前先运行该脚本校验；`.github/workflows/plugin-gate.yml` 与 `.githooks/pre-push` 在 PR 与推送前跑同一预检。

## 宿主钩子与占位实现

`has_page`（前端是否出现「查看数据」入口）由宿主 `ObjectUtils.check_method` 判定，规则是**方法体是否全部由占位语句组成**（`pass`、纯 docstring、`...`、`raise NotImplementedError`），与返回值无关：

- 没有详情页的插件，`get_page` 必须保持 `docstring + pass`。
- 写成 `return None` 会被视为「已实现」，前端就会放出数据页入口，点开只显示「此插件没有详情页面」。

同一条规则也适用于 `get_command`、`get_api`、`get_module` 等其他钩子：返回 `[]`/`{}` 属于已实现，只有占位方法体才算未实现。

## 测试

- `pyproject.toml` 的 `[tool.pytest.ini_options]` 是唯一权威测试配置：`testpaths = tests`，用例放 `tests/v1|v2|v3/<plugin_id>/`，文件名为 `test_*.py`，已注册 `v1`/`v2`/`v3` marker；仓库不再保留独立的 `pytest.ini`，也不要新建。
- 测试需要 MoviePilot 后端位于本仓库的同级目录，或通过环境变量 `MOVIEPILOT_BACKEND_PATH` 指定其路径；`tests/_bootstrap.py` 负责把后端加入 `sys.path`。
- 各代必须在**独立会话**中运行，否则同名插件包会冲突；`tests/run.py` 会按代依次起独立子进程。
- 全量运行：`uv run python tests/run.py`。
- 只跑某一代时用路径驱动，路径即决定 `tests/conftest.py` 注入哪一代环境，例如 `uv run pytest tests/v2`。
- `[tool.pytest.ini_options]` 中保留的 `filterwarnings` 只针对主程序依赖链和三方库；插件自身产生的告警应去修代码，不要往白名单里加。
- 更细的运行方式、后端准备与常见报错见 [tests/README.md](tests/README.md)。

## 代码风格

- `pyproject.toml` 的 `[tool.ruff]` 是唯一权威风格配置：`line-length = 120`，`select` 包含 `E/F/W/B/D/UP/C90/N/SIM/I/ASYNC`。
- `D` 组只认 ASCII 句末标点：中文 docstring 首行必须以 `.` 结尾，写成全角 `。` 会报 `D400`/`D415`；仓库现有插件源码（如 `plugins.v2/`）就是这个写法，可参照。
- 不要用 `ruff check --fix --unsafe-fixes` 修 `D400`/`D415`，它会在 `。` 后再补一个 `.`，产出 `。.`；按上一条规则手改。
- 超长字符串用括号隐式拼接分行，避免 `E501`。
- 不要用编辑器扩展自带的 ruff，版本可能和 `uv.lock` 不一致；统一走 `uv run ruff`。
- 新增或修改 Python 文件后必须自己跑一遍 `ruff check` 并通过，不要指望编辑器保存时才暴露问题。
- 类型检查用 pyright：宿主 API（`app.*`）不在本仓库，`reportMissingImports` 已关闭，导入的宿主符号一律视为 `Any`；不要为了消除 `Unknown` 去造假 stub 或强转类型。

## 提交前自检

```powershell
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

- 改了 `package*.json` 或插件版本后，先本地跑一次版本预检再推送：`uv run python .github/scripts/check_plugin_versions.py package.json package.v2.json package.v3.json`。
- `.github/workflows/lint.yml` 会在 PR 上重跑上面三条命令，`.github/workflows/plugin-gate.yml` 与 `.githooks/pre-push` 负责索引与版本预检。
- 提交前可用 `git diff --check` 自查行尾空白等问题，这只是自查项，不是强制门禁。
- PR 模板与检查项以仓库现有模板为准；本文件不另设 commit message 格式与分支命名约定。

## 文档写作

- 中英混排：正文用中文，代码、字段名、路径保持英文原样。
- 更新根 `README.md` 时保留插件清单、市场使用、来源与许可等既有用户向结构；只在原本有 `[toc]` 时才保留它，不暗示当前必须有；只增补与修正，不无必要地重写。
- 新增 `docs/faq/` 条目沿用现有 `NN-kebab-case-title.md` 编号命名，并同步该目录索引；该规则只在同步上游文档快照时适用，不改变 `docs/` 不是本仓库日常维护内容的定位。

## 参考

- 使用与安装：[README.md](README.md)
- 自动化代理的仓库硬约束：[AGENTS.md](AGENTS.md)
- V3 插件开发（唯一完整主指南）：[docs/Plugin_Development.md](docs/Plugin_Development.md)
- V2 插件迁移到 V3：[docs/V3_Plugin_Adaptation.md](docs/V3_Plugin_Adaptation.md)
- 插件 API 返回与前端调用：[docs/V3_API_Response_Adaptation.md](docs/V3_API_Response_Adaptation.md)
- 仓库索引与发布：[docs/Repository_Guide.md](docs/Repository_Guide.md)
- v2 维护：[docs/V2_Plugin_Development.md](docs/V2_Plugin_Development.md)
- 功能集成示例：[docs/FAQ.md](docs/FAQ.md) · [docs/faq/](docs/faq/)
- 单测说明：[tests/README.md](tests/README.md)
