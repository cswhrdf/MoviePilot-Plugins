# AGENTS.md

MoviePilot 插件仓库。本文件只写**从代码里看不出来、但容易做错**的约定；详细规范请看文档，不要在此重复。

## 目录语义

| 路径 | 用途 |
|---|---|
| `plugins/` | v1 插件（`package.json` 索引） |
| `plugins.v2/` | v2 插件（`package.v2.json` 索引），当前只保留 `plugins.v2/transferimagesidecar/` 历史实现 |
| `plugins.v3/` | v3 插件（`package.v3.json` 索引），当前自有实现在 `plugins.v3/transferimagesidecar/` |
| `icons/` | 全仓库共享图标库，扁平存放，不按插件分子目录 |
| `docs/` | 开发指南；`docs/faq/` 为 01–22 编号的功能集成示例 |
| `tests/` | pytest 用例，按代分目录 |

本仓库是官方仓库的裁剪版，只保留自有插件。`plugins/` 当前为空属于正常状态，**不要用空目录反推结构缺失**，也不要在其中补写占位插件或脚手架。

文档体系以 V3 为主线：`docs/Plugin_Development.md` 是唯一完整主指南，`docs/V3_Plugin_Adaptation.md` 只讲迁移，`docs/V2_Plugin_Development.md` 仅供维护历史插件参考。新增文档时不要另起一套并行指南。

## 新增/修改插件的同步清单

一次插件改动必须同时覆盖以下几处，缺一即视为未完成：

1. **插件源码**：`plugins.{vN}/<plugin_id>/__init__.py`（另可含 `README.md`），目录名必须是主类名的小写形式
2. **索引条目**：v1 用 `package.json`、v2 用 `package.v2.json`、v3 用 `package.v3.json`；键为插件类名（如 `TransferImageSidecar`），不是目录名
3. **图标**：`icon` 字段只写 `icons/` 下的文件名（如 `movie.jpg`），文件名大小写必须与磁盘完全一致 —— 部署环境为 Linux，大小写敏感
4. **根 `README.md`**：在「插件清单」小节加/改一条，指向该插件自己的 `README.md` 相对路径 + 一句话描述
5. **测试**：`tests/v3/<plugin_id>/` 或 `tests/v2/<plugin_id>/`（按 `pytest.ini` 约定）

索引条目中的 `version` 必须等于 `history` 里最高的版本号，二者不一致会被插件市场判定为版本异常。`history` 中已有条目是历史记录，只追加不改写。V3 副本的大版本必须等于旧代主版本加一（如 `0.1.3 -> 1.0.0`），这是 `.github/scripts/check_plugin_versions.py` 的硬校验。

## 宿主钩子判定

`has_page`（前端是否出现「查看数据」入口）由宿主 `ObjectUtils.check_method` 判定，规则是**方法体是否全部由占位语句组成**（`pass`、纯 docstring、`...`、`raise NotImplementedError`），与返回值无关：

- 没有详情页的插件，`get_page` 必须保持 `docstring + pass`。
- 写成 `return None` 会被视为「已实现」，前端就会放出数据页入口，点开只显示「此插件没有详情页面」。

同一条规则也适用于 `get_command`、`get_api`、`get_module` 等其他钩子：返回 `[]`/`{}` 属于已实现，只有占位方法体才算未实现。

## 测试

`pytest.ini` 是唯一权威配置：`testpaths = tests`，用例放 `tests/v1|v2|v3/<plugin_id>/`，文件 `test_*.py`，已注册 `v1`/`v2`/`v3` marker。

各代必须在**独立会话**中运行，否则同名插件包会冲突：

```powershell
pytest -m v2
```

`pytest.ini` 中保留的 `filterwarnings` 只针对主程序依赖链和三方库；插件自身产生的告警应去修代码，不要往白名单里加。

## 环境陷阱

- 本地开发环境通常是 Windows，但插件运行于 Linux 容器：换行用 LF，路径分隔符与大小写按 Linux 处理。
- `package*.json` 是本仓库最大的 diff 敏感文件。只做局部插入/替换，**不要整体格式化或重排键顺序**。
- `.githooks/pre-push` 与 `.github/workflows/plugin-gate.yml` 会校验索引，改动后本地先跑一次预检再推送。

## 文档写作

- 中英混排，正文中文，代码、字段名、路径保持英文原样。
- 更新 `README.md` 时保留既有标题层级与 `[toc]` 标记，只增补/修正，不重写全文。
- 新增 `docs/faq/` 条目沿用现有 `NN-kebab-case-title.md` 编号命名，并同步 `docs/faq/` 的索引。

## 参考

- 使用与安装：[README.md](README.md)
- v2 开发：[docs/V2_Plugin_Development.md](docs/V2_Plugin_Development.md)
- v3 适配：[docs/V3_Plugin_Adaptation.md](docs/V3_Plugin_Adaptation.md) · [docs/V3_API_Response_Adaptation.md](docs/V3_API_Response_Adaptation.md)
- 仓库维护：[docs/Repository_Guide.md](docs/Repository_Guide.md)
- 功能集成示例：`docs/faq/`
