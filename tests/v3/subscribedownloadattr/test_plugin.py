"""订阅下载分类与标签（SubscribeDownloadAttr）V3 实现的合同单测.

按官方测试规范：通过生产命名空间 app.plugins.subscribedownloadattr 导入插件
（tests/conftest.py 负责注入后端与 plugins.v3/ 插件目录），并用 object.__new__
绕过插件构造，只覆盖不依赖运行时组合根、数据库、网络与下载器的合同：生命周期与
钩子占位、宿主只读适配（伪 SystemConfigOper / 伪 DirectoryHelper），以及
DownloadAdded 事件处理（伪 writer / 伪插件数据存储 / 可注入时间源）。
"""

from __future__ import annotations

import ast
import copy
import importlib
import inspect
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.schemas.system import TransferDirectoryConf
from app.schemas.types import EventType, MediaType, SystemConfigKey
from app.sdk.events import Event
from app.sdk.plugin import _PluginBase

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "plugins.v3/subscribedownloadattr/__init__.py"
PLUGIN_ID = "subscribedownloadattr"
CLASS_NAME = "SubscribeDownloadAttr"

# 基类在本版宿主上的全部抽象方法；补齐后 __abstractmethods__ 才为空
ABSTRACT_METHODS = {"init_plugin", "get_state", "get_api", "get_form", "get_page", "stop_service"}
LEGACY_MODULE_PREFIXES = ("app.core.", "app.helper.", "app.utils.", "app.sdk._legacy")
# 允许的宿主导入：稳定面 + 已评审接受的 DirectoryHelper 例外依赖（不在 app.sdk 稳定面）
ALLOWED_APP_IMPORTS = {
    "app.sdk.plugin",
    "app.sdk.config",
    "app.sdk.logging",
    "app.sdk.events",
    "app.schemas.system",
    "app.schemas.types",
    "app.application.directory",
}

# 配置规范化后的九个键（与规则模型一致）
EXPECTED_CONFIG_KEYS = (
    "enabled",
    "trigger",
    "apply_tags",
    "apply_category",
    "keep_system_tag",
    "dry_run",
    "default_rule",
    "directory_rules",
    "category_rules",
)


def _load_plugin():
    """按生产命名空间导入 V3 插件模块."""
    return importlib.import_module(f"app.plugins.{PLUGIN_ID}")


def _make_plugin():
    """绕过运行时组装实例化插件, 保留类级默认状态."""
    return object.__new__(_load_plugin().SubscribeDownloadAttr)


def _imported_modules() -> list[str]:
    """返回插件源码中出现的所有导入模块名."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return modules


def _is_placeholder_stmt(stmt: ast.stmt) -> bool:
    """复刻宿主 ObjectUtils 的占位语句判定."""
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
        return isinstance(stmt.value.value, str) or stmt.value.value is Ellipsis
    if isinstance(stmt, ast.Raise):
        exc = stmt.exc
        if isinstance(exc, ast.Call):
            exc = exc.func
        return isinstance(exc, ast.Name) and exc.id == "NotImplementedError"
    return False


def _method_body_is_placeholder(method_name: str) -> bool:
    """判断指定方法的方法体是否全部由占位语句组成."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    for cls in [node for node in tree.body if isinstance(node, ast.ClassDef)]:
        if cls.name != CLASS_NAME:
            continue
        for fn in cls.body:
            if isinstance(fn, ast.FunctionDef) and fn.name == method_name:
                return all(_is_placeholder_stmt(stmt) for stmt in fn.body)
    raise AssertionError(f"未找到方法 {method_name}")


def _walk_form(node: Any) -> list[dict[str, Any]]:
    """按原有顺序展开表单树, 返回所有组件节点."""
    nodes: list[dict[str, Any]] = []
    if isinstance(node, dict):
        nodes.append(node)
        for child in node.get("content") or ():
            nodes.extend(_walk_form(child))
    elif isinstance(node, list):
        for child in node:
            nodes.extend(_walk_form(child))
    return nodes


def _form_components(form: Any, name: str) -> list[dict[str, Any]]:
    """返回表单中指定组件名的全部节点, 保持页面顺序."""
    return [node for node in _walk_form(form) if node.get("component") == name]


def _form_models(form: Any) -> list[str]:
    """返回表单所有组件的 model 绑定路径（含重复, 用于校验不重复渲染同一目录键）."""
    return [
        str(node["props"]["model"])
        for node in _walk_form(form)
        if isinstance(node.get("props"), dict) and "model" in node["props"]
    ]


def _form_text(form: Any) -> str:
    """汇总表单所有可读文本（text/title/hint/label）, 用于断言提示与说明可见."""
    parts: list[str] = []
    for node in _walk_form(form):
        props = node.get("props")
        if not isinstance(props, dict):
            continue
        for key in ("text", "title", "hint", "label"):
            value = props.get(key)
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts)


# ---------------- 基类合同与元数据 ----------------


def test_plugin_has_no_abstract_methods():
    """插件必须实现基类全部抽象方法, 才能被宿主实例化."""
    plugin_class = _load_plugin().SubscribeDownloadAttr

    assert set(_PluginBase.__abstractmethods__) == ABSTRACT_METHODS
    assert plugin_class.__abstractmethods__ == frozenset()
    assert set(plugin_class.__dict__) >= ABSTRACT_METHODS

    plugin = object.__new__(plugin_class)
    assert isinstance(plugin, _PluginBase)
    # 本步只补齐必需抽象方法, 不覆盖基类默认的渲染模式
    assert "get_render_mode" not in plugin_class.__dict__


def test_metadata_values():
    """插件元数据必须与已定稿的骨架设计逐项一致."""
    plugin_class = _load_plugin().SubscribeDownloadAttr

    assert plugin_class.plugin_name == "订阅下载分类与标签"
    assert plugin_class.plugin_desc == "订阅自动下载时按目录规则为下载任务写入下载器分类与标签"
    assert plugin_class.plugin_icon == "directory.png"
    assert plugin_class.plugin_version == "0.1.0"
    assert plugin_class.plugin_author == "cswhrdf"
    assert plugin_class.plugin_config_prefix == "subscribedownloadattr_"
    assert plugin_class.plugin_order == 51
    assert plugin_class.auth_level == 1
    # 图标必须真实存在于共享图标库（Linux 部署区分大小写）
    assert (ROOT / "icons" / plugin_class.plugin_icon).is_file()


def test_directory_name_matches_class_name_lower():
    """插件目录名必须是主类名的小写形式, 且类定义位于该目录的 __init__.py."""
    module = _load_plugin()
    source = Path(str(module.__file__)).resolve()

    assert module.__name__ == f"app.plugins.{PLUGIN_ID}"
    assert source.name == "__init__.py"
    assert source.parent.name == module.SubscribeDownloadAttr.__name__.lower()
    assert source.parent.parent.name == "plugins.v3"


# ---------------- 生命周期 ----------------


def test_init_plugin_repeatable():
    """init_plugin 可重复调用, 默认启用, 且非字典配置按空配置容错."""
    plugin = _make_plugin()

    plugin.init_plugin(None)
    assert plugin.get_state() is True
    plugin.init_plugin(None)
    assert plugin.get_state() is True

    wrong_config: Any = "enabled=false"
    plugin.init_plugin(wrong_config)
    assert plugin.get_state() is True


def test_init_plugin_normalizes_config():
    """init_plugin 经 normalize_config 接入配置, 未声明的键不影响启用状态."""
    plugin = _make_plugin()

    plugin.init_plugin({})
    assert plugin.get_state() is True
    assert plugin._config is not None
    assert tuple(plugin._config) == EXPECTED_CONFIG_KEYS
    assert plugin.config_issues == ()

    plugin.init_plugin({"enabled": False})
    assert plugin.get_state() is False
    assert plugin.config_issues == ()

    # 未知键不参与规范化, 也不影响启用状态
    plugin.init_plugin({"enabled": True, "unknown_rule_key": "ignored"})
    assert plugin.get_state() is True
    assert plugin._config is not None
    assert tuple(plugin._config) == EXPECTED_CONFIG_KEYS
    assert plugin.config_issues == ()


def test_init_plugin_reports_config_issues():
    """非法配置不抛异常, 原因进 config_issues 且运行态仍是九个键."""
    plugin = _make_plugin()

    plugin.init_plugin({"dry_run": "not-a-bool", "trigger": "manual"})

    assert plugin.get_state() is True
    assert plugin._config is not None
    assert plugin._config["dry_run"] is False
    assert plugin._config["trigger"] == "subscribe"
    assert len(plugin.config_issues) == 2
    assert all(isinstance(issue, str) for issue in plugin.config_issues)


def test_get_state_follows_enabled():
    """get_state 精确反映 _enabled, 且未初始化的实例默认为停用."""
    module = _load_plugin()
    plugin = object.__new__(module.SubscribeDownloadAttr)

    assert module.SubscribeDownloadAttr._enabled is False
    assert plugin.get_state() is False

    plugin._enabled = True
    assert plugin.get_state() is True

    plugin._enabled = False
    assert plugin.get_state() is False


def test_stop_service_idempotent():
    """stop_service 可重复调用且不抛异常, 停止后状态与配置运行态均被复位."""
    plugin = _make_plugin()
    plugin.init_plugin({"dry_run": True})
    assert plugin.get_state() is True

    plugin.stop_service()
    plugin.stop_service()

    assert plugin.get_state() is False
    assert plugin._config is None
    assert plugin.config_issues == ()


# ---------------- 钩子骨架 ----------------


def test_get_command_empty():
    """插件不注册远程命令, 且保持静态方法形态."""
    module = _load_plugin()
    plugin = object.__new__(module.SubscribeDownloadAttr)

    assert plugin.get_command() == []
    assert isinstance(inspect.getattr_static(module.SubscribeDownloadAttr, "get_command"), staticmethod)


def test_get_api_empty_skeleton():
    """API 列表仅空骨架有效, P1 落地真实 API 时移除空返回并改写本用例."""
    plugin = _make_plugin()

    assert plugin.get_api() == []


def test_get_form_structure():
    """get_form 返回完整表单与九键默认模型, 基础区/默认规则/JSON 文本域组件与 props 明确."""
    plugin = _make_plugin()

    form, model = plugin.get_form()

    assert [node["component"] for node in form] == ["VForm"]
    assert tuple(model) == EXPECTED_CONFIG_KEYS
    assert "_issues" not in model
    assert json.loads(json.dumps(form)) == form
    assert json.loads(json.dumps(model)) == model
    assert model["default_rule"] == {"category": "", "tags": []}
    assert model["directory_rules"] == {}
    assert model["category_rules"] == "[]"

    models = _form_models(form)
    for key in ("enabled", "trigger", "apply_tags", "apply_category", "keep_system_tag", "dry_run"):
        assert key in models
    assert "default_rule.category" in models
    assert "default_rule.tags" in models
    assert "category_rules" in models
    # 表单 model 根键与九键契约对应（无目录行时不存在 directory_rules 绑定路径）
    assert {path.split(".", 1)[0] for path in models} == set(EXPECTED_CONFIG_KEYS) - {"directory_rules"}
    assert len(models) == len(set(models))

    switches = {node["props"]["model"]: node["props"] for node in _form_components(form, "VSwitch")}
    assert set(switches) == {"enabled", "dry_run", "apply_tags", "apply_category", "keep_system_tag"}
    assert switches["enabled"]["label"] == "启用插件"
    assert switches["dry_run"]["label"] == "仅记录不写入"
    assert "不调用下载器" in switches["dry_run"]["hint"]
    assert switches["keep_system_tag"]["label"] == "保留系统标签"
    assert "TORRENT_TAG" in switches["keep_system_tag"]["hint"]

    combos = {node["props"]["model"]: node["props"] for node in _form_components(form, "VCombobox")}
    assert combos["trigger"]["items"] == ["subscribe", "all"]
    assert combos["trigger"]["clearable"] is False
    assert "subscribe" in combos["trigger"]["hint"] and "all" in combos["trigger"]["hint"]
    assert combos["default_rule.tags"]["multiple"] is True
    assert combos["default_rule.tags"]["chips"] is True
    assert combos["default_rule.tags"]["closable-chips"] is True

    texts = {node["props"]["model"]: node["props"] for node in _form_components(form, "VTextField")}
    assert texts["default_rule.category"]["label"] == "默认分类"
    assert "不修改任务分类" in texts["default_rule.category"]["hint"]

    areas = [node["props"] for node in _form_components(form, "VTextarea")]
    assert [props["model"] for props in areas] == ["category_rules"]
    assert areas[0]["rows"] == 10
    assert areas[0]["auto-grow"] is True
    assert "directory" in areas[0]["hint"] and "JSON" in areas[0]["hint"]
    assert "tags" in areas[0]["hint"]


def test_form_roundtrip(monkeypatch):
    """默认模型与规范化配置同构, 原样保存后重新初始化得到等价的规范化配置."""
    module = _load_plugin()
    initial: dict[str, Any] = {
        "enabled": False,
        "trigger": "all",
        "apply_tags": True,
        "apply_category": False,
        "keep_system_tag": False,
        "dry_run": True,
        "default_rule": {"category": "默认", "tags": "默认, hd"},
        "directory_rules": {"/mnt/media:/tv": {"category": "电视剧", "tags": ["tv"]}},
        "category_rules": [
            {"directory": "/mnt/media:/tv", "media_type": "电视剧", "media_category": "动画", "category": "anime"}
        ],
    }
    normalized_initial, initial_issues = module.normalize_config(initial)
    assert initial_issues == []

    _, plugin, _, _ = _prepare(monkeypatch, directories=[_directory("电视剧", "/mnt/media", "/tv", 0)])
    plugin.init_plugin(initial)

    form, model = plugin.get_form()

    assert tuple(model) == EXPECTED_CONFIG_KEYS
    assert tuple(model) == tuple(normalized_initial)
    assert model["default_rule"]["tags"] == ["默认", "hd"]
    assert isinstance(model["default_rule"]["tags"], list)
    assert model["directory_rules"] == normalized_initial["directory_rules"]
    assert isinstance(model["category_rules"], str)
    # 运行期是 list 时按 json.dumps(..., ensure_ascii=False, indent=2) 回填为多行字符串
    assert "\n" in model["category_rules"]
    assert json.loads(model["category_rules"]) == normalized_initial["category_rules"]
    assert _form_models(form)

    # 保存即把页面模型原样回写; category_rules 原始类型不同, 规范化后必须相等
    saved = copy.deepcopy(model)
    saved_issues: list[str] = []
    normalized_saved, saved_issues = module.normalize_config(saved)
    assert saved_issues == []
    assert normalized_saved == normalized_initial
    assert tuple(normalized_saved) == EXPECTED_CONFIG_KEYS

    fresh = object.__new__(module.SubscribeDownloadAttr)
    fresh.systemconfig = _FakeSystemConfig([_directory("电视剧", "/mnt/media", "/tv", 0)])
    fresh.init_plugin(saved)
    assert fresh.get_state() is False
    assert fresh._config == normalized_initial
    assert fresh.config_issues == ()

    # 保存后的 category_rules 仍能在第二级（目录 + 媒体类别）命中
    matched = module.match_rule(
        saved,
        directory_key="/mnt/media:/tv",
        media={"media_type": "电视剧", "media_category": "动画"},
    )
    assert matched["level"] == "category"
    assert matched["rule"]["category"] == "anime"
    assert matched["rule_index"] == 1


def test_category_rules_form_roundtrip(monkeypatch):
    """category_rules 用 VTextarea 绑定 JSON 字符串, 合法原文稳定回填, 非法 JSON 只提示不抛."""
    _, plugin, _, _ = _prepare(monkeypatch, directories=[_directory("电视剧", "/mnt/media", "/tv", 0)])
    text = json.dumps(
        [
            {"directory": "/mnt/media:/tv", "media_type": "电视剧", "category": "anime", "tags": ["anime"]},
            {"media_type": "电影", "category": "无目录键"},
        ],
        ensure_ascii=False,
        indent=2,
    )
    plugin.init_plugin(
        {
            "enabled": False,
            "directory_rules": {"/mnt/media:/tv": {"category": "电视剧"}},
            "category_rules": text,
        }
    )
    assert plugin.config_issues
    assert any("缺少有效的 directory" in issue for issue in plugin.config_issues)

    form, model = plugin.get_form()

    assert [node["props"]["model"] for node in _form_components(form, "VTextarea")] == ["category_rules"]
    assert model["category_rules"] == text
    assert model["enabled"] is False
    assert model["directory_rules"] == {"/mnt/media:/tv": {"category": "电视剧", "tags": []}}
    page_text = _form_text(form)
    assert "配置提示" in page_text
    assert "缺少有效的 directory" in page_text

    # 合法 JSON 原文保存后再取表单, 回填内容稳定
    stable_plugin = object.__new__(_load_plugin().SubscribeDownloadAttr)
    stable_plugin.systemconfig = _FakeSystemConfig([_directory("电视剧", "/mnt/media", "/tv", 0)])
    stable_plugin.init_plugin(copy.deepcopy(model))
    _, stable_model = stable_plugin.get_form()
    assert stable_model["category_rules"] == text

    # 非法 JSON 时 get_form/init_plugin 都不抛, 原文保留供编辑, 其余配置不丢
    plugin.init_plugin({"enabled": False, "category_rules": "{not json", "dry_run": True})
    invalid_form, invalid_model = plugin.get_form()
    assert invalid_model["category_rules"] == "{not json"
    assert invalid_model["dry_run"] is True
    assert any("不是合法 JSON" in issue for issue in plugin.config_issues)
    assert "不是合法 JSON" in _form_text(invalid_form)
    assert json.loads(json.dumps(invalid_form)) == invalid_form
    assert json.loads(json.dumps(invalid_model)) == invalid_model


def test_form_directory_rows_follow_host_config(monkeypatch):
    """目录行来自伪 SystemConfigOper 实时目录, 键/顺序/字段/生效标记与运行期一致."""
    _, plugin, _, oper = _prepare(
        monkeypatch,
        directories=[
            _directory("电视剧", "/mnt/media", "/tv", 5),
            _directory("电影", "/mnt/media", "/movie", 1),
            {
                "name": "电视剧副本",
                "storage": "/mnt/media",
                "download_path": "/tv",
                "priority": 9,
                "media_type": "电影",
                "media_category": "动画",
            },
        ],
    )
    plugin.init_plugin({"directory_rules": {"/mnt/media:/tv": {"category": "电视剧", "tags": "tv"}}})
    calls_before = len(oper.calls)

    form, model = plugin.get_form()

    assert len(oper.calls) == calls_before  # 配置页不重新读取宿主目录
    models = _form_models(form)
    assert "directory_rules./mnt/media:/movie.category" in models
    assert "directory_rules./mnt/media:/movie.tags" in models
    assert "directory_rules./mnt/media:/tv.category" in models
    assert "directory_rules./mnt/media:/tv.tags" in models
    # 重复键的非生效项不重复渲染编辑入口, 避免静默覆盖首条
    assert models.count("directory_rules./mnt/media:/tv.category") == 1
    assert models.count("directory_rules./mnt/media:/tv.tags") == 1
    # 有目录行时九个配置键全部有 model 绑定, 且每个路径只渲染一次
    assert {path.split(".", 1)[0] for path in models} == set(EXPECTED_CONFIG_KEYS)
    assert len(models) == len(set(models))

    directory_rows = [
        node["props"]["text"]
        for node in _form_components(form, "VAlert")
        if isinstance(node["props"].get("text"), str) and node["props"]["text"].startswith("目录 #")
    ]
    assert [text.split("：", 1)[0] for text in directory_rows] == ["目录 #1", "目录 #2", "目录 #3"]
    assert "电影" in directory_rows[0] and "/mnt/media:/movie" in directory_rows[0]
    assert "priority：1" in directory_rows[0]
    assert "media_type/media_category：-" in directory_rows[0]
    assert "电视剧" in directory_rows[1] and "/mnt/media:/tv" in directory_rows[1]
    assert "priority：5" in directory_rows[1]
    assert "media_type/media_category：电影/动画" in directory_rows[2]
    assert "priority：9" in directory_rows[2]

    page_text = _form_text(form)
    assert "规则键 /mnt/media:/tv 不生效" in page_text
    assert "目录键冲突" in page_text
    assert "priority 升序首条" in page_text

    # 默认模型只保留配置里真实存在的规则, 不因重复行重复键或额外造键
    assert model["directory_rules"] == {"/mnt/media:/tv": {"category": "电视剧", "tags": ["tv"]}}
    assert json.loads(json.dumps(form)) == form
    assert json.loads(json.dumps(model)) == model


def test_form_directory_row_key_matches_runtime_key(monkeypatch):
    """目录行 model 键与运行期目录键同源同序, storage 缺失回退目录名后仍能命中目录级规则."""
    module, plugin, _, _ = _prepare(
        monkeypatch,
        directories=[
            {"name": "电视目录", "storage": None, "download_path": "/tv", "priority": 2},
            _directory("电影", "/mnt/media", "/movie", 1),
        ],
    )
    plugin.init_plugin(
        {
            "directory_rules": {
                "电视目录": {"category": "电视剧", "tags": "tv, 高清"},
                "/mnt/media:/movie": {"category": "电影"},
            }
        }
    )

    form, model = plugin.get_form()

    keys = plugin.directory_keys
    assert keys is not None
    # 表单行键与运行期键逐条相同, 且顺序来自 priority 升序后的同一序列
    assert [entry["key"] for entry in keys["entries"]] == ["/mnt/media:/movie", "电视目录"]
    models = _form_models(form)
    for key in ("/mnt/media:/movie", "电视目录"):
        assert f"directory_rules.{key}.category" in models
        assert f"directory_rules.{key}.tags" in models

    # 名字回退键保存后仍能在 P0-03 的目录级匹配中命中, 与运行期 directory_key 一致
    normalized_saved, saved_issues = module.normalize_config(copy.deepcopy(model))
    assert saved_issues == []
    matched = module.match_rule(
        normalized_saved,
        directory_key="电视目录",
        media={"media_type": "电影", "media_category": ""},
    )
    assert matched["level"] == "directory"
    assert matched["rule"] == {"category": "电视剧", "tags": ["tv", "高清"]}
    assert module.directory_key(plugin.directory_entries[1], 2) == "电视目录"


def test_form_shows_stale_and_conflict_notes(monkeypatch):
    """失效目录键与完整键冲突在页面可见且带状态, 优先级/#序号说明与九键模型不受影响."""
    _, plugin, _, _ = _prepare(
        monkeypatch,
        directories=[
            _directory("电视剧", "/mnt/media", "/tv", 3),
            _directory("电视剧副本", "/mnt/media", "/tv", 4),
        ],
    )
    plugin.init_plugin(
        {
            "directory_rules": {
                "/mnt/media:/tv": {"category": "电视剧"},
                "/mnt/media:/old": {"category": "旧目录"},
            }
        }
    )

    form, model = plugin.get_form()

    page_text = _form_text(form)
    assert "已失效目录键（1 个）" in page_text
    assert "/mnt/media:/old → 已失效" in page_text
    assert "目录键冲突：/mnt/media:/tv" in page_text
    assert "电视剧(priority=3, #1)" in page_text
    assert "电视剧副本(priority=4, #2)" in page_text
    assert "首条（#1）生效" in page_text
    assert "数值小者优先" in page_text
    assert "#序号 会随目录增删漂移" in page_text
    assert "目录 + 媒体类别 > 目录 > 默认规则" in page_text

    # 失效键仍在默认模型中保留, 保存后可原样回写
    assert model["directory_rules"]["/mnt/media:/old"] == {"category": "旧目录", "tags": []}
    assert tuple(model) == EXPECTED_CONFIG_KEYS
    assert "_issues" not in model


def test_get_form_empty_directories_and_no_side_effects(monkeypatch):
    """空目录仍返回基础区/默认规则/文本域, 且不写宿主配置、不触发下载器与持久化."""
    module, plugin, _, oper = _prepare(monkeypatch, directories=[])
    plugin.init_plugin({"default_rule": {"category": "默认", "tags": "a, b"}})
    calls_before = len(oper.calls)
    store = _bind_store(plugin)
    writer_calls = _install_writer(module, monkeypatch, _FakeWriter())

    form, model = plugin.get_form()

    assert model["directory_rules"] == {}
    assert model["default_rule"] == {"category": "默认", "tags": ["a", "b"]}
    assert isinstance(model["category_rules"], str)
    assert tuple(model) == EXPECTED_CONFIG_KEYS
    page_text = _form_text(form)
    assert "当前宿主目录配置中没有 download_path 非空的目录" in page_text
    assert "默认分类" in page_text and "目录 + 媒体类别规则" in page_text

    assert len(oper.calls) == calls_before
    assert store.get_calls == [] and store.save_calls == []
    assert writer_calls == []
    assert json.loads(json.dumps(form)) == form
    assert json.loads(json.dumps(model)) == model


def test_get_module_is_placeholder():
    """get_module 必须保持占位方法体, 宿主才会视为未实现."""
    plugin = _make_plugin()

    assert plugin.get_module() is None
    assert _method_body_is_placeholder("get_module") is True


def test_get_page_is_implemented(monkeypatch):
    """get_page 方法体不再是占位, 空记录时返回 VAlert 空态且不抛异常."""
    _, plugin, fake_logger, _ = _prepare(monkeypatch)
    store = _bind_store(plugin)

    page = plugin.get_page()

    # 宿主按「方法体是否全为占位语句」判定 has_page, 本插件确有详情页
    assert _method_body_is_placeholder("get_page") is False
    assert page is not None
    assert [node["component"] for node in page] == ["VAlert"]
    assert page[0]["props"]["type"] == "info"
    assert page[0]["props"]["title"] == "暂无处理记录"
    assert "本页只读" in page[0]["props"]["text"]
    assert json.loads(json.dumps(page)) == page
    # 空态只读记录, 不写插件数据
    assert store.get_calls == ["records"]
    assert store.save_calls == []
    assert fake_logger.warnings == []


def test_plugin_only_uses_stable_v3_imports():
    """插件只能从 app.sdk 稳定入口与已评审例外路径导入, 不得回退到旧宿主路径."""
    modules = _imported_modules()

    for name in modules:
        assert not name.startswith(LEGACY_MODULE_PREFIXES), name
    app_imports = {name for name in modules if name.startswith("app.")}
    assert app_imports <= ALLOWED_APP_IMPORTS, app_imports


# ---------------- 宿主只读适配：伪宿主对象 ----------------


_UNSET = object()


class _FakeSystemConfig:
    """伪宿主系统配置读取器: 记录读取次数并返回可替换的目录列表."""

    def __init__(self, value: Any = None, error: Exception | None = None) -> None:
        self.value = value
        self.error = error
        self.calls: list[Any] = []

    def get(self, key: Any = None) -> Any:
        """记录一次读取并返回预设值或抛出预设异常."""
        self.calls.append(key)
        if self.error is not None:
            raise self.error
        return self.value


class _FakeLogger:
    """伪宿主 logger: 只记录 warning 文本, 不落盘."""

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, message: Any, *args: Any, **kwargs: Any) -> None:
        """记录一条告警文本."""
        self.warnings.append(str(message))


class _FakeDirectoryHelper:
    """伪宿主 DirectoryHelper: 记录目录匹配与保存路径确认的调用与返回."""

    calls: list[Any] = []
    result: Any = None
    save_path_calls: list[tuple[Any, str]] = []
    save_path_result: Any = None

    @classmethod
    def reset(cls, result: Any = None, save_path_result: Any = None) -> None:
        """清空调用记录并设置下一次目录匹配与保存路径确认的返回值."""
        cls.calls = []
        cls.result = result
        cls.save_path_calls = []
        cls.save_path_result = save_path_result

    def get_download_dir(self, media: Any) -> Any:
        """记录被查询的媒体并返回预设目录."""
        type(self).calls.append(media)
        return type(self).result

    def get_download_dir_by_save_path(self, media: Any, save_path: str) -> Any:
        """记录二次确认的入参顺序并返回预设目录."""
        type(self).save_path_calls.append((media, save_path))
        return type(self).save_path_result


class _Media:
    """模拟 context.media_info 的对象属性形态（不是宿主类型）."""

    def __init__(self, media_type: Any, category: Any = "", title: Any = "", year: Any = "") -> None:
        self.type = media_type
        self.category = category
        self.title = title
        self.year = year


def _directory(name: str, storage: str | None, path: str | None, priority: Any = 0) -> dict[str, Any]:
    """构造一条宿主目录原始配置."""
    return {"name": name, "storage": storage, "download_path": path, "priority": priority}


def _prepare(
    monkeypatch: Any,
    *,
    tag: Any = "MOVIEPILOT",
    directories: Any = _UNSET,
) -> tuple[Any, Any, _FakeLogger, _FakeSystemConfig]:
    """构造带伪宿主依赖的插件实例, 只替换外部读取端（Oper/logger/settings/DirectoryHelper）."""
    module = _load_plugin()
    fake_logger = _FakeLogger()
    monkeypatch.setattr(module, "logger", fake_logger)
    monkeypatch.setattr(module, "settings", SimpleNamespace(TORRENT_TAG=tag))
    monkeypatch.setattr(module, "DirectoryHelper", _FakeDirectoryHelper)
    _FakeDirectoryHelper.reset()

    plugin = object.__new__(module.SubscribeDownloadAttr)
    value = [] if directories is _UNSET else directories
    oper = _FakeSystemConfig(value)
    plugin.systemconfig = oper
    return module, plugin, fake_logger, oper


# ---------------- 宿主只读适配：目录配置 ----------------


def test_directory_config_change_keeps_keys(monkeypatch):
    """每次 init_plugin 实时读取目录, 目录变化后路径类键仍与目录一一对应."""
    module, plugin, _, oper = _prepare(
        monkeypatch,
        directories=[
            _directory("电视剧", "/mnt/media", "/tv", 2),
            _directory("电影", "/mnt/media", "/movie", 1),
        ],
    )

    plugin.init_plugin({})
    first = plugin.directory_keys
    assert first is not None
    assert first["effective_keys"] == {"/mnt/media:/movie": 1, "/mnt/media:/tv": 2}
    assert len(oper.calls) == 1
    assert oper.calls[0] is SystemConfigKey.Directories

    # 新增一条更高优先级的目录后重新初始化：实时读取第二次, 已有键不变
    oper.value = [
        _directory("电视剧", "/mnt/media", "/tv", 2),
        _directory("电影", "/mnt/media", "/movie", 1),
        _directory("动漫", "/mnt/media", "/anime", 0),
    ]
    plugin.init_plugin({})
    second = plugin.directory_keys
    assert second is not None
    assert len(oper.calls) == 2
    assert second["effective_keys"] == {
        "/mnt/media:/anime": 1,
        "/mnt/media:/movie": 2,
        "/mnt/media:/tv": 3,
    }
    # 路径类键不随序号漂移, 与目录本身一一对应
    assert set(second["effective_keys"]) == set(first["effective_keys"]) | {"/mnt/media:/anime"}


def test_directory_entries_sorted_by_priority_and_stable(monkeypatch):
    """目录按 priority or 0 升序排列, 同优先级保持宿主配置顺序."""
    _, plugin, _, _ = _prepare(
        monkeypatch,
        directories=[
            _directory("后", "/mnt/media", "/c", 5),
            _directory("先", "/mnt/media", "/a", 1),
            _directory("次", "/mnt/media", "/b", 1),
        ],
    )

    plugin.init_plugin({})

    assert [entry["name"] for entry in plugin.directory_entries] == ["先", "次", "后"]
    assert [entry["priority"] for entry in plugin.directory_entries] == [1, 1, 5]
    keys = plugin.directory_keys
    assert keys is not None
    assert [entry["sequence"] for entry in keys["entries"]] == [1, 2, 3]


def test_directory_entries_skip_empty_download_path(monkeypatch):
    """无 download_path 的目录不参与规则键与 #序号 编号."""
    _, plugin, _, _ = _prepare(
        monkeypatch,
        directories=[
            _directory("纯媒体库", "/mnt/media", None),
            {"download_path": "/bare", "priority": 1},
            _directory("电影", "/mnt/media", "/movie", 0),
        ],
    )

    plugin.init_plugin({})

    assert [entry["download_path"] for entry in plugin.directory_entries] == ["/movie", "/bare"]
    keys = plugin.directory_keys
    assert keys is not None
    # 纯媒体库目录被过滤, 因此无 storage/name 的条目是 #2 而不是 #1
    assert keys["effective_keys"] == {"/mnt/media:/movie": 1, "#2": 2}


def test_directory_duplicate_key_keeps_first_and_reports_conflict(monkeypatch):
    """完整键重复时首条生效, 冲突结构由 rules.resolve_directory_keys 生成."""
    _, plugin, _, _ = _prepare(
        monkeypatch,
        directories=[
            _directory("电视剧副本", "/mnt/media", "/tv", 5),
            _directory("电视剧", "/mnt/media", "/tv", 0),
        ],
    )

    plugin.init_plugin({})

    keys = plugin.directory_keys
    assert keys is not None
    assert keys["effective_keys"] == {"/mnt/media:/tv": 1}
    assert [entry["effective"] for entry in keys["entries"]] == [True, False]
    assert keys["conflicts"] == [
        {
            "key": "/mnt/media:/tv",
            "count": 2,
            "first_sequence": 1,
            "names": ["电视剧", "电视剧副本"],
            "priorities": [0, 5],
            "sequences": [1, 2],
        },
    ]


def test_directory_config_degrades_without_raising(monkeypatch):
    """空/None/非列表/坏条目/Oper 异常都降级为空结构并记 warning, 不抛给 init."""
    cases = [
        ("空列表", [], None),
        ("None", None, None),
        ("非列表", {"name": "不是列表"}, None),
        ("坏条目", ["不是对象", {"priority": [1], "download_path": "/tv"}], None),
        ("Oper 异常", None, RuntimeError("系统配置快照尚未加载")),
    ]
    for label, value, error in cases:
        _, plugin, fake_logger, oper = _prepare(monkeypatch, directories=value)
        oper.error = error

        plugin.init_plugin({})

        assert plugin.directory_entries == (), label
        assert plugin.directory_keys is None, label
        assert plugin.host_issues, label
        assert fake_logger.warnings, label
        assert len(fake_logger.warnings) == len(plugin.host_issues), label


def test_directory_config_keeps_valid_entries_on_bad_items(monkeypatch):
    """坏条目只跳过自身, 有效项保留并生成目录键, 同时记录 warning."""
    _, plugin, fake_logger, _ = _prepare(
        monkeypatch,
        directories=[
            "坏条目",
            {"name": "坏优先级", "priority": {"a": 1}, "download_path": "/x"},
            _directory("电影", "/mnt/media", "/movie", 0),
        ],
    )

    plugin.init_plugin({})

    assert [entry["name"] for entry in plugin.directory_entries] == ["电影"]
    keys = plugin.directory_keys
    assert keys is not None
    assert keys["effective_keys"] == {"/mnt/media:/movie": 1}
    joined = " | ".join(fake_logger.warnings)
    assert "第 1 条目录配置不是对象" in joined
    assert "第 2 条目录配置校验失败" in joined


def test_source_uses_system_config_key_not_raw_literal():
    """源码只通过 SystemConfigKey 引用目录配置键, 不出现原始字符串字面量."""
    source = SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    literals = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    attributes = {
        (node.value.id, node.attr)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
    }

    assert "Directories" not in literals
    assert ("SystemConfigKey", "Directories") in attributes


# ---------------- 宿主只读适配：系统标签 ----------------


def test_system_tag_reads_plain_value(monkeypatch):
    """settings.TORRENT_TAG 以普通 str 保存, 不作为系统配置键读取."""
    _, plugin, fake_logger, _ = _prepare(
        monkeypatch,
        tag="  MYTAG  ",
        directories=[_directory("电影", "/mnt/media", "/movie", 0)],
    )

    plugin.init_plugin({})

    assert plugin.system_tag == "MYTAG"
    assert isinstance(plugin.system_tag, str)
    assert fake_logger.warnings == []
    assert "settings.TORRENT_TAG" in SOURCE.read_text(encoding="utf-8")


def test_system_tag_falls_back_with_warning(monkeypatch):
    """标签为空或属性缺失时回退 rules.DEFAULT_SYSTEM_TAG 并记 warning."""
    module, plugin, fake_logger, _ = _prepare(
        monkeypatch,
        tag="",
        directories=[_directory("电影", "/mnt/media", "/movie", 0)],
    )
    plugin.init_plugin({})
    assert plugin.system_tag == module.DEFAULT_SYSTEM_TAG
    assert len(fake_logger.warnings) == 1
    assert "TORRENT_TAG" in fake_logger.warnings[0]

    # 宿主设置对象没有该属性时同样降级
    module, plugin, fake_logger, _ = _prepare(
        monkeypatch,
        directories=[_directory("电影", "/mnt/media", "/movie", 0)],
    )
    monkeypatch.setattr(module, "settings", SimpleNamespace())
    plugin.init_plugin({})
    assert plugin.system_tag == module.DEFAULT_SYSTEM_TAG
    assert len(fake_logger.warnings) == 1
    assert "TORRENT_TAG" in fake_logger.warnings[0]


# ---------------- 宿主只读适配：媒体与目录定位 ----------------


def test_missing_media_skips_directory_resolution(monkeypatch):
    """媒体缺失或 type 为空时不调用 get_download_dir, 目录解析返回 None."""
    _, plugin, _, _ = _prepare(
        monkeypatch,
        directories=[_directory("电影", "/mnt/media", "/movie", 0)],
    )
    plugin.init_plugin({})
    _FakeDirectoryHelper.reset(
        result=TransferDirectoryConf(name="电影", storage="/mnt/media", download_path="/movie"),
    )

    for media in (None, _Media(None), _Media("")):
        assert plugin._media_snapshot(media) is None
        assert plugin._resolve_directory_entry(media) is None

    assert _FakeDirectoryHelper.calls == []


def test_media_snapshot_plain_fields(monkeypatch):
    """媒体快照按对象属性读取, 枚举转普通 str, 四字段精确匹配."""
    _, plugin, _, _ = _prepare(monkeypatch)

    snapshot = plugin._media_snapshot(_Media(MediaType.TV, "动画", "示例剧", "2024"))

    assert snapshot is not None
    assert snapshot == {
        "media_type": "电视剧",
        "media_category": "动画",
        "title": "示例剧",
        "year": "2024",
    }
    assert all(isinstance(value, str) for value in snapshot.values())


def test_directory_resolution_returns_matching_key(monkeypatch):
    """宿主命中目录后返回同一序列中的目录键条目, 未命中返回 None."""
    _, plugin, _, _ = _prepare(
        monkeypatch,
        directories=[
            _directory("电视剧", "/mnt/media", "/tv", 1),
            _directory("电影", "/mnt/media", "/movie", 0),
        ],
    )
    plugin.init_plugin({})
    media = _Media(MediaType.TV, "国产剧", "示例剧", "2023")

    # 宿主返回全新实例, 只能按已校验字段定位
    _FakeDirectoryHelper.reset(
        result=TransferDirectoryConf(name="电视剧", storage="/mnt/media", download_path="/tv", priority=1),
    )
    assert plugin._resolve_directory_entry(media) == {
        "sequence": 2,
        "key": "/mnt/media:/tv",
        "name": "电视剧",
        "storage": "/mnt/media",
        "download_path": "/tv",
        "priority": 1,
        "effective": True,
    }
    assert _FakeDirectoryHelper.calls == [media]

    _FakeDirectoryHelper.reset()
    assert plugin._resolve_directory_entry(media) is None

    _FakeDirectoryHelper.reset(
        result=TransferDirectoryConf(name="未知", storage="/mnt/other", download_path="/tv"),
    )
    assert plugin._resolve_directory_entry(media) is None


def test_stop_service_clears_directory_runtime(monkeypatch):
    """stop_service 清空目录快照、目录键与系统标签, 且可重复调用."""
    module, plugin, _, _ = _prepare(
        monkeypatch,
        tag="MYTAG",
        directories=[_directory("电影", "/mnt/media", "/movie", 0)],
    )
    plugin.init_plugin({})
    assert plugin.directory_entries
    assert plugin.directory_keys is not None
    assert plugin.system_tag == "MYTAG"

    plugin.stop_service()
    plugin.stop_service()

    assert plugin.directory_entries == ()
    assert plugin.directory_keys is None
    assert plugin.host_issues == ()
    assert plugin.system_tag == module.DEFAULT_SYSTEM_TAG


# ---------------- DownloadAdded 事件处理：伪持久化、伪 writer 与事件构造 ----------------


class _FakeStore:
    """伪插件数据存储: 记录 get/save 调用并深拷贝数据, 不访问宿主数据库."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.get_calls: list[Any] = []
        self.save_calls: list[tuple[str, Any]] = []

    def get_data(self, key: Any = None) -> Any:
        """记录读取并返回数据副本."""
        self.get_calls.append(key)
        return copy.deepcopy(self.data.get(key))

    def save_data(self, key: str, value: Any) -> None:
        """记录写入并保存数据副本."""
        self.save_calls.append((key, copy.deepcopy(value)))
        self.data[key] = copy.deepcopy(value)


class _FakeWriter:
    """伪下载器写入适配: 记录 read/add/set 调用并按预设返回统一结果."""

    def __init__(
        self,
        *,
        base_tags: dict[str, list[str]] | None = None,
        read_status: Any = None,
        read_error: Exception | None = None,
        add_status: Any = None,
        add_error: Exception | None = None,
        category_status: Any = None,
        category_error: Exception | None = None,
    ) -> None:
        module = _load_plugin()
        self._writer_result = module.WriterResult
        self._writer_status = module.WriterStatus
        self._read_result = module.TagReadResult
        self.base_tags = base_tags or {}
        self.read_status = self._status(read_status, module.WriterStatus.OK)
        self.read_error = read_error
        self.add_status = self._status(add_status, module.WriterStatus.OK)
        self.add_error = add_error
        self.category_status = self._status(category_status, module.WriterStatus.OK)
        self.category_error = category_error
        self.read_calls: list[list[str]] = []
        self.tag_calls: list[tuple[list[str], list[str]]] = []
        self.category_calls: list[tuple[str, str]] = []
        self.supports_category: bool | None = True

    @staticmethod
    def _status(value: Any, default: Any) -> Any:
        """把字符串或枚举状态统一为 writer 状态枚举."""
        if value is None:
            return default
        return value if isinstance(value, type(default)) else type(default)(value)

    def read_tags_ex(self, hashs: Any) -> Any:
        """返回每个 hash 的预设原标签."""
        self.read_calls.append(list(hashs))
        if self.read_error is not None:
            raise self.read_error
        tags = {item: list(self.base_tags.get(item, [])) for item in hashs}
        return self._read_result(tags, self._writer_result(self.read_status, "fake read"))

    def add_tags(self, hashs: Any, tags: Any) -> Any:
        """记录标签写入并按预设返回."""
        self.tag_calls.append((list(hashs), list(tags)))
        if self.add_error is not None:
            raise self.add_error
        return self._writer_result(self.add_status, "fake tag write")

    def set_category(self, hash_value: str, category: str) -> Any:
        """记录分类写入并按预设返回."""
        self.category_calls.append((hash_value, category))
        if self.category_error is not None:
            raise self.category_error
        return self._writer_result(self.category_status, "fake category write")


class _MutableClock:
    """可推进的伪时间源, 只返回固定 datetime, 不读系统时间."""

    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        """返回当前设定时间."""
        return self.value


def _bind_store(plugin: Any) -> _FakeStore:
    """用伪存储替换基类数据读写, 只伪造持久化边界."""
    store = _FakeStore()
    plugin.get_data = store.get_data
    plugin.save_data = store.save_data
    return store


def _install_writer(module: Any, monkeypatch: Any, writer: _FakeWriter) -> list[Any]:
    """替换外部 writer 工厂并返回下载器名称调用记录."""
    calls: list[Any] = []

    def factory(downloader: Any = None) -> _FakeWriter:
        calls.append(downloader)
        return writer

    monkeypatch.setattr(module, "resolve_writer", factory)
    return calls


def _tv_directory(priority: Any = 0) -> TransferDirectoryConf:
    """构造宿主选中的电视剧目录."""
    return TransferDirectoryConf(name="电视剧", storage="/mnt/media", download_path="/tv", priority=priority)


def _hit_config(**overrides: Any) -> dict[str, Any]:
    """构造能命中电视剧目录规则的启用配置."""
    config: dict[str, Any] = {
        "enabled": True,
        "trigger": "all",
        "apply_tags": True,
        "apply_category": True,
        "keep_system_tag": True,
        "dry_run": False,
        "default_rule": {},
        "directory_rules": {"/mnt/media:/tv": {"category": "电视剧", "tags": ["tv"]}},
        "category_rules": [],
    }
    config.update(overrides)
    return config


def _prepare_event(monkeypatch: Any, *, config: dict[str, Any] | None = None) -> tuple[Any, Any, Any, _FakeStore]:
    """构造完成初始化的插件, 并把持久化替换为伪存储."""
    module, plugin, fake_logger, _ = _prepare(
        monkeypatch,
        directories=[_directory("电视剧", "/mnt/media", "/tv", 0)],
    )
    plugin.init_plugin(config or _hit_config())
    store = _bind_store(plugin)
    return module, plugin, fake_logger, store


def _event(
    hash_value: str = "h1",
    *,
    media: Any = _UNSET,
    source: Any = 'Subscribe|{"id": 1, "name": "示例订阅"}',
    downloader: Any = "qbittorrent",
    save_path: Any = _UNSET,
    context: Any = _UNSET,
) -> Event:
    """构造 DownloadAdded 事件 payload."""
    if context is _UNSET:
        media_value = _Media(MediaType.TV, "国产剧", "示例剧", "2024") if media is _UNSET else media
        context = SimpleNamespace(media_info=media_value)
        if save_path is not _UNSET:
            context.save_path = save_path
    return Event(
        EventType.DownloadAdded,
        {"hash": hash_value, "context": context, "downloader": downloader, "source": source},
    )


def _records(store: _FakeStore) -> list[dict[str, Any]]:
    """返回伪存储中的处理记录."""
    value = store.data.get("records")
    return value if isinstance(value, list) else []


def _seen(store: _FakeStore) -> dict[str, str]:
    """返回伪存储中的去重表."""
    value = store.data.get("seen")
    return value if isinstance(value, dict) else {}


# ---------------- DownloadAdded 事件处理：开关与来源 ----------------


def test_disabled_short_circuit(monkeypatch):
    """enabled=False 立即返回: 不写记录、不解析目录、不取 writer."""
    module, plugin, _, store = _prepare_event(monkeypatch, config=_hit_config(enabled=False))
    writer = _FakeWriter()
    writer_calls = _install_writer(module, monkeypatch, writer)
    _FakeDirectoryHelper.reset(result=_tv_directory())

    plugin.on_download_added(_event())

    assert store.save_calls == []
    assert store.get_calls == []
    assert _FakeDirectoryHelper.calls == []
    assert writer_calls == []
    assert writer.read_calls == []
    assert writer.tag_calls == []
    assert writer.category_calls == []


def test_trigger_subscribe_filters_source(monkeypatch):
    """trigger=subscribe 时非订阅来源 skipped 且不写, 订阅来源正常处理."""
    module, plugin, _, store = _prepare_event(monkeypatch, config=_hit_config(trigger="subscribe"))
    writer = _FakeWriter()
    writer_calls = _install_writer(module, monkeypatch, writer)
    _FakeDirectoryHelper.reset(result=_tv_directory())

    plugin.on_download_added(_event("manual", source="Manual|用户手动下载"))

    assert writer_calls == []
    assert writer.tag_calls == []
    records = _records(store)
    assert len(records) == 1
    assert records[0]["status"] == "skipped"
    assert "非订阅来源" in records[0]["reason"]

    plugin.on_download_added(_event("subscribed", source='Subscribe|{"id": 7, "name": "示例订阅"}'))

    assert writer_calls == ["qbittorrent"]
    assert writer.tag_calls == [(["subscribed"], ["tv"])]
    assert _records(store)[-1]["status"] == "ok"


def test_dry_run_no_writer_calls(monkeypatch):
    """dry_run=true 不取 writer、不读标签、不写, 但记录 dry_run 与拟写入值."""
    module, plugin, _, store = _prepare_event(monkeypatch, config=_hit_config(dry_run=True))
    writer = _FakeWriter()
    writer_calls = _install_writer(module, monkeypatch, writer)
    _FakeDirectoryHelper.reset(result=_tv_directory())

    plugin.on_download_added(_event())

    assert writer_calls == []
    assert writer.read_calls == []
    assert writer.tag_calls == []
    assert writer.category_calls == []
    record = _records(store)[-1]
    assert record["status"] == "dry_run"
    assert record["category"] == "电视剧"
    assert record["tags"] == ["tv"]
    assert "dry_run" in record["reason"]
    assert _seen(store) == {"h1": record["time"]}


def test_event_handler_swallows_write_error(monkeypatch):
    """Writer 抛异常时处理器正常返回, 记录 failed 并写 warning."""
    module, plugin, fake_logger, store = _prepare_event(monkeypatch)
    writer = _FakeWriter(add_error=RuntimeError("rpc down"))
    _install_writer(module, monkeypatch, writer)
    _FakeDirectoryHelper.reset(result=_tv_directory())

    plugin.on_download_added(_event())

    record = _records(store)[-1]
    assert record["status"] == "failed"
    assert "rpc down" in record["reason"]
    assert any("写入失败" in message and "rpc down" in message for message in fake_logger.warnings)
    # 写入失败不登记 seen, 便于宿主重投或修正配置后重试
    assert _seen(store) == {}


def test_writer_factory_error_is_contained(monkeypatch):
    """Writer 工厂抛错时处理器正常返回, 记录 failed 并写 warning."""
    module, plugin, fake_logger, store = _prepare_event(monkeypatch)
    _FakeDirectoryHelper.reset(result=_tv_directory())

    def broken_factory(downloader: Any = None) -> Any:
        raise RuntimeError("service registry unavailable")

    monkeypatch.setattr(module, "resolve_writer", broken_factory)

    plugin.on_download_added(_event())

    record = _records(store)[-1]
    assert record["status"] == "failed"
    assert "service registry unavailable" in record["reason"]
    assert any("处理 DownloadAdded 事件失败" in message for message in fake_logger.warnings)


# ---------------- DownloadAdded 事件处理：去重与记录上限 ----------------


def test_records_pruned(monkeypatch):
    """注入上限 3 时, 连续 4 条记录只保留最新 3 条."""
    module, plugin, _, store = _prepare_event(monkeypatch)
    plugin._max_records = 3
    writer = _FakeWriter()
    _install_writer(module, monkeypatch, writer)
    _FakeDirectoryHelper.reset(result=_tv_directory())

    for hash_value in ("h1", "h2", "h3", "h4"):
        plugin.on_download_added(_event(hash_value))

    records = _records(store)
    assert [record["hash"] for record in records] == ["h2", "h3", "h4"]
    assert len(_seen(store)) == 4


def test_records_limit_constant(monkeypatch):
    """默认记录上限常量固定为 200, 不放进配置."""
    module = _load_plugin()

    assert module.MAX_RECORDS == 200
    assert module.SubscribeDownloadAttr._max_records == 200


def test_seen_expires_after_seven_days(monkeypatch):
    """7 天前的记录与 seen 被清理, 过期 hash 可再次处理, 新 hash 保留."""
    module, plugin, _, store = _prepare_event(monkeypatch)
    clock = _MutableClock(datetime(2026, 11, 1, 12, 0, tzinfo=UTC))
    plugin._clock = clock
    writer = _FakeWriter()
    _install_writer(module, monkeypatch, writer)
    _FakeDirectoryHelper.reset(result=_tv_directory())

    plugin.on_download_added(_event("old"))
    assert list(_seen(store)) == ["old"]

    clock.value = clock.value + timedelta(days=8)
    plugin.on_download_added(_event("new"))

    assert [record["hash"] for record in _records(store)] == ["new"]
    assert list(_seen(store)) == ["new"]

    plugin.on_download_added(_event("old"))

    assert [record["hash"] for record in _records(store)] == ["new", "old"]
    assert set(_seen(store)) == {"new", "old"}
    assert [call[0] for call in writer.tag_calls] == [["old"], ["new"], ["old"]]


def test_duplicate_hash_not_rewritten(monkeypatch):
    """同一 hash 7 天内第二次事件不重复写入, 只追加 skipped 记录."""
    module, plugin, _, store = _prepare_event(monkeypatch)
    writer = _FakeWriter()
    _install_writer(module, monkeypatch, writer)
    _FakeDirectoryHelper.reset(result=_tv_directory())

    plugin.on_download_added(_event("same"))
    plugin.on_download_added(_event("same"))

    assert writer.read_calls == [["same"]]
    assert writer.tag_calls == [(["same"], ["tv"])]
    assert writer.category_calls == [("same", "电视剧")]
    records = _records(store)
    assert [record["status"] for record in records] == ["ok", "skipped"]
    assert "7 天" in records[-1]["reason"]


# ---------------- DownloadAdded 事件处理：守卫与规则空值 ----------------


def test_missing_media_event_skips(monkeypatch):
    """事件媒体缺失或 type 为空时 skipped, 目录解析与写入调用均为 0."""
    module, plugin, _, store = _prepare_event(monkeypatch)
    writer = _FakeWriter()
    writer_calls = _install_writer(module, monkeypatch, writer)
    _FakeDirectoryHelper.reset(result=_tv_directory())

    plugin.on_download_added(_event("no-media", media=None))
    plugin.on_download_added(_event("empty-type", media=_Media("")))

    assert _FakeDirectoryHelper.calls == []
    assert writer_calls == []
    assert writer.read_calls == []
    records = _records(store)
    assert [record["status"] for record in records] == ["skipped", "skipped"]
    assert all("媒体信息缺失" in record["reason"] for record in records)


def test_empty_rule_values_skip_writer_calls(monkeypatch):
    """命中规则但分类与标签皆空时 skipped, 不取 writer."""
    module, plugin, _, store = _prepare_event(
        monkeypatch,
        config=_hit_config(directory_rules={"/mnt/media:/tv": {"category": "", "tags": []}}),
    )
    writer = _FakeWriter()
    writer_calls = _install_writer(module, monkeypatch, writer)
    _FakeDirectoryHelper.reset(result=_tv_directory())

    plugin.on_download_added(_event())

    assert writer_calls == []
    record = _records(store)[-1]
    assert record["status"] == "skipped"
    assert record["level"] == "directory"
    assert record["directory_key"] == "/mnt/media:/tv"
    assert "无有效规则" in record["reason"]


def test_empty_category_or_tags_skip_corresponding_writer(monkeypatch):
    """空分类/空标签或 apply_* 关闭时不调用对应 writer 方法, 记录按实际生效值."""
    # 只有标签
    module, plugin, _, store = _prepare_event(
        monkeypatch,
        config=_hit_config(directory_rules={"/mnt/media:/tv": {"category": "", "tags": ["tv"]}}),
    )
    writer = _FakeWriter()
    _install_writer(module, monkeypatch, writer)
    _FakeDirectoryHelper.reset(result=_tv_directory())
    plugin.on_download_added(_event())
    assert writer.tag_calls == [(["h1"], ["tv"])]
    assert writer.category_calls == []
    assert _records(store)[-1]["status"] == "ok"

    # 只有分类
    module, plugin, _, store = _prepare_event(
        monkeypatch,
        config=_hit_config(directory_rules={"/mnt/media:/tv": {"category": "电视剧", "tags": []}}),
    )
    writer = _FakeWriter()
    _install_writer(module, monkeypatch, writer)
    _FakeDirectoryHelper.reset(result=_tv_directory())
    plugin.on_download_added(_event())
    assert writer.tag_calls == []
    assert writer.category_calls == [("h1", "电视剧")]
    assert _records(store)[-1]["status"] == "ok"

    # apply_tags=False 时不读标签也不写标签
    module, plugin, _, store = _prepare_event(monkeypatch, config=_hit_config(apply_tags=False))
    writer = _FakeWriter()
    _install_writer(module, monkeypatch, writer)
    _FakeDirectoryHelper.reset(result=_tv_directory())
    plugin.on_download_added(_event())
    assert writer.read_calls == []
    assert writer.tag_calls == []
    assert writer.category_calls == [("h1", "电视剧")]
    record = _records(store)[-1]
    assert record["tags"] == []
    assert record["category"] == "电视剧"

    # apply_category=False 时不写分类
    module, plugin, _, store = _prepare_event(monkeypatch, config=_hit_config(apply_category=False))
    writer = _FakeWriter()
    _install_writer(module, monkeypatch, writer)
    _FakeDirectoryHelper.reset(result=_tv_directory())
    plugin.on_download_added(_event())
    assert writer.category_calls == []
    assert writer.tag_calls == [(["h1"], ["tv"])]
    record = _records(store)[-1]
    assert record["category"] == ""
    assert record["tags"] == ["tv"]


def test_write_status_aggregation(monkeypatch):
    """ok/unsupported/failed/skipped 按 writer 返回状态准确记录."""
    cases = [
        ("ok", _FakeWriter(), "ok"),
        ("unsupported", _FakeWriter(category_status="unsupported"), "unsupported"),
        ("failed", _FakeWriter(category_status="failed"), "failed"),
        ("skipped", _FakeWriter(add_status="skipped", category_status="skipped"), "skipped"),
    ]
    for label, writer, expected in cases:
        module, plugin, _, store = _prepare_event(monkeypatch)
        _install_writer(module, monkeypatch, writer)
        _FakeDirectoryHelper.reset(result=_tv_directory())

        plugin.on_download_added(_event())

        record = _records(store)[-1]
        assert record["status"] == expected, label
        if expected == "unsupported":
            assert "fake category write" in record["reason"], label
        if expected == "failed":
            assert record["reason"], label


# ---------------- DownloadAdded 事件处理：读取失败、能力降级与重试 ----------------


def test_read_failure_skips_tag_write(monkeypatch):
    """读取原标签失败时放弃标签写入, 记 failed 且不登记 seen."""
    # 状态失败: 不写标签, 分类仍按能力写入
    module, plugin, _, store = _prepare_event(monkeypatch)
    writer = _FakeWriter(read_status="failed")
    _install_writer(module, monkeypatch, writer)
    _FakeDirectoryHelper.reset(result=_tv_directory())

    plugin.on_download_added(_event())

    assert writer.read_calls == [["h1"]]
    assert writer.tag_calls == []
    assert writer.category_calls == [("h1", "电视剧")]
    record = _records(store)[-1]
    assert record["status"] == "failed"
    assert "fake read" in record["reason"]
    assert _seen(store) == {}

    # 读取抛异常: 与读取失败同义, 仍不写标签
    module, plugin, _, store = _prepare_event(monkeypatch)
    writer = _FakeWriter(read_error=RuntimeError("tag read timeout"))
    _install_writer(module, monkeypatch, writer)
    _FakeDirectoryHelper.reset(result=_tv_directory())

    plugin.on_download_added(_event())

    assert writer.read_calls == [["h1"]]
    assert writer.tag_calls == []
    record = _records(store)[-1]
    assert record["status"] == "failed"
    assert "tag read timeout" in record["reason"]
    assert _seen(store) == {}


def test_read_unsupported_degrades_without_claiming_ok(monkeypatch):
    """读取能力 unsupported 时按追加语义写入, 状态仍记 unsupported 并说明原因."""
    module, plugin, _, store = _prepare_event(monkeypatch)
    writer = _FakeWriter(read_status="unsupported")
    _install_writer(module, monkeypatch, writer)
    _FakeDirectoryHelper.reset(result=_tv_directory())

    plugin.on_download_added(_event())

    assert writer.read_calls == [["h1"]]
    assert writer.tag_calls == [(["h1"], ["tv"])]
    record = _records(store)[-1]
    assert record["status"] == "unsupported"
    assert "读取原标签不可用" in record["reason"]
    assert "fake read" in record["reason"]
    assert record["tags"] == ["tv"]


def test_unsupported_category_capability_skips_writer_call(monkeypatch):
    """能力探测明确不支持分类时不调用分类写入, 记录 unsupported 而非 ok."""
    module, plugin, _, store = _prepare_event(monkeypatch)
    writer = _FakeWriter()
    writer.supports_category = False
    _install_writer(module, monkeypatch, writer)
    _FakeDirectoryHelper.reset(result=_tv_directory())

    plugin.on_download_added(_event())

    assert writer.tag_calls == [(["h1"], ["tv"])]
    assert writer.category_calls == []
    record = _records(store)[-1]
    assert record["status"] == "unsupported"
    assert "不支持分类写入" in record["reason"]
    assert _seen(store) == {"h1": record["time"]}


def test_failed_write_can_retry_same_hash(monkeypatch):
    """写入失败不登记 seen, 同一 hash 再次事件仍会重试写入并成功."""
    module, plugin, _, store = _prepare_event(monkeypatch)
    writer = _FakeWriter(add_error=RuntimeError("rpc down"))
    _install_writer(module, monkeypatch, writer)
    _FakeDirectoryHelper.reset(result=_tv_directory())

    plugin.on_download_added(_event("retry"))

    assert _records(store)[-1]["status"] == "failed"
    assert _seen(store) == {}

    writer.add_error = None
    plugin.on_download_added(_event("retry"))

    assert writer.tag_calls == [(["retry"], ["tv"]), (["retry"], ["tv"])]
    assert _records(store)[-1]["status"] == "ok"
    assert set(_seen(store)) == {"retry"}


def test_exception_fallback_records_failed(monkeypatch):
    """处理流程抛异常时兜底记一条 failed 记录且不向宿主抛出."""
    module, plugin, fake_logger, store = _prepare_event(monkeypatch)

    def broken_handle(event: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(plugin, "_handle_download_added", broken_handle)

    assert plugin.on_download_added(_event("err")) is None

    record = _records(store)[-1]
    assert record["status"] == "failed"
    assert record["hash"] == "err"
    assert "boom" in record["reason"]
    assert any("处理 DownloadAdded 事件失败" in message for message in fake_logger.warnings)
    assert _seen(store) == {}


def test_exception_fallback_does_not_recurse(monkeypatch):
    """兜底记录自身失败时只记 warning, 不递归记录也不外抛."""
    module, plugin, fake_logger, store = _prepare_event(monkeypatch)

    def broken_handle(event: Any) -> None:
        raise RuntimeError("boom")

    def broken_record(**kwargs: Any) -> None:
        raise RuntimeError("record down")

    monkeypatch.setattr(plugin, "_handle_download_added", broken_handle)
    monkeypatch.setattr(plugin, "_record", broken_record)

    plugin.on_download_added(_event("err"))

    assert plugin._reporting_failure is False
    assert any("处理 DownloadAdded 事件失败" in message and "boom" in message for message in fake_logger.warnings)
    assert any("记录事件处理异常失败" in message and "record down" in message for message in fake_logger.warnings)
    assert store.save_calls == []


# ---------------- DownloadAdded 事件处理：保存路径与重复键 ----------------


def test_save_path_second_confirmation(monkeypatch):
    """可读 save_path 时按 (media, save_path) 二次确认; 不一致不写, 无路径不额外读."""
    directories = [
        _directory("电视剧", "/mnt/media", "/tv", 0),
        _directory("电影", "/mnt/media", "/movie", 1),
    ]
    rules = {
        "/mnt/media:/tv": {"category": "电视剧", "tags": ["tv"]},
        "/mnt/media:/movie": {"category": "电影", "tags": ["movie"]},
    }
    module, plugin, _, _ = _prepare_event(
        monkeypatch,
        config=_hit_config(directory_rules=rules),
    )
    # 重新初始化以使用两条目录
    plugin.systemconfig = _FakeSystemConfig(directories)
    plugin.init_plugin(_hit_config(directory_rules=rules))
    store = _bind_store(plugin)
    writer = _FakeWriter()
    _install_writer(module, monkeypatch, writer)

    media = _Media(MediaType.TV, "国产剧", "示例剧", "2024")
    context = SimpleNamespace(media_info=media, save_path="/downloads/tv")

    # 保存路径确认失败: 不写, 记录 skipped
    _FakeDirectoryHelper.reset(result=_tv_directory(), save_path_result=None)
    plugin.on_download_added(_event("mismatch", context=context))
    assert _FakeDirectoryHelper.save_path_calls == [(media, "/downloads/tv")]
    assert writer.tag_calls == []
    assert _records(store)[-1]["status"] == "skipped"
    assert "不一致" in _records(store)[-1]["reason"]

    # 保存路径确认到电影目录: 使用确认后的目录键
    confirmed = TransferDirectoryConf(name="电影", storage="/mnt/media", download_path="/movie", priority=1)
    _FakeDirectoryHelper.reset(result=_tv_directory(), save_path_result=confirmed)
    plugin.on_download_added(_event("confirmed", context=context))
    record = _records(store)[-1]
    assert record["directory_key"] == "/mnt/media:/movie"
    assert record["level"] == "directory"
    assert record["category"] == "电影"
    assert record["tags"] == ["movie"]
    assert writer.category_calls == [("confirmed", "电影")]

    # 无 save_path 时不额外读取, 保留 get_download_dir 结果
    _FakeDirectoryHelper.reset(result=_tv_directory())
    plugin.on_download_added(_event("no-path", context=SimpleNamespace(media_info=media)))
    assert _FakeDirectoryHelper.save_path_calls == []
    record = _records(store)[-1]
    assert record["directory_key"] == "/mnt/media:/tv"
    assert record["category"] == "电视剧"


def test_duplicate_key_effective_false_and_conflict_warning(monkeypatch):
    """命中重复键非生效项时 skipped 且运行期只记一条冲突 warning, 不误写."""
    module, plugin, fake_logger, store = _prepare_event(monkeypatch)
    # 用重复键目录重新初始化
    plugin.systemconfig = _FakeSystemConfig(
        [
            _directory("电视剧", "/mnt/media", "/tv", 0),
            _directory("电视剧副本", "/mnt/media", "/tv", 5),
        ]
    )
    plugin.init_plugin(_hit_config())
    store = _bind_store(plugin)
    writer = _FakeWriter()
    writer_calls = _install_writer(module, monkeypatch, writer)

    duplicate = TransferDirectoryConf(name="电视剧副本", storage="/mnt/media", download_path="/tv", priority=5)
    _FakeDirectoryHelper.reset(result=duplicate)
    plugin.on_download_added(_event("dup"))

    record = _records(store)[-1]
    assert record["status"] == "skipped"
    assert "非生效项" in record["reason"]
    assert writer_calls == []
    assert writer.tag_calls == []
    conflict_warnings = [message for message in fake_logger.warnings if "完整键重复" in message]
    assert len(conflict_warnings) == 1

    # 命中生效项时正常写入, 冲突 warning 不重复
    _FakeDirectoryHelper.reset(result=_tv_directory())
    plugin.on_download_added(_event("effective"))
    assert writer_calls == ["qbittorrent"]
    assert _records(store)[-1]["status"] == "ok"
    assert len([message for message in fake_logger.warnings if "完整键重复" in message]) == 1


# ---------------- DownloadAdded 事件处理：记录结构与持久化容错 ----------------


def test_record_fields_json_serializable(monkeypatch):
    """记录字段完整且可 JSON 序列化, 时间/标题/层级/标签合并准确."""
    module, plugin, _, store = _prepare_event(monkeypatch)
    clock = _MutableClock(datetime(2026, 11, 15, 8, 30, tzinfo=UTC))
    plugin._clock = clock
    writer = _FakeWriter(base_tags={"h1": ["MOVIEPILOT", "old"]})
    _install_writer(module, monkeypatch, writer)
    _FakeDirectoryHelper.reset(result=_tv_directory())

    plugin.on_download_added(_event())

    record = _records(store)[-1]
    assert set(record) == {
        "time",
        "hash",
        "title",
        "directory_key",
        "level",
        "category",
        "tags",
        "status",
        "reason",
    }
    assert record["time"] == "2026-11-15T08:30:00+00:00"
    assert record["hash"] == "h1"
    assert record["title"] == "示例剧"
    assert record["directory_key"] == "/mnt/media:/tv"
    assert record["level"] == "directory"
    assert record["category"] == "电视剧"
    assert record["tags"] == ["tv"]
    assert record["status"] == "ok"
    assert isinstance(record["reason"], str)
    assert json.loads(json.dumps(record)) == record
    assert writer.tag_calls == [(["h1"], ["MOVIEPILOT", "old", "tv"])]
    assert _seen(store) == {"h1": record["time"]}


def test_persistence_failure_is_contained(monkeypatch):
    """save_data 异常不外抛, 记录保留在内存且只记 warning."""
    module, plugin, fake_logger, _ = _prepare_event(monkeypatch)
    writer = _FakeWriter()
    _install_writer(module, monkeypatch, writer)
    _FakeDirectoryHelper.reset(result=_tv_directory())

    def broken_save(key: str, value: Any) -> None:
        raise RuntimeError("db down")

    plugin.save_data = broken_save

    plugin.on_download_added(_event())

    assert any("保存处理记录失败" in message and "db down" in message for message in fake_logger.warnings)
    assert plugin._records_cache is not None
    assert [record["hash"] for record in plugin._records_cache] == ["h1"]
    assert plugin._seen_cache == {"h1": plugin._records_cache[0]["time"]}


def test_records_persist_across_instances(monkeypatch):
    """记录与 seen 经 save_data 持久化, 新实例经 get_data 读到后仍按 7 天去重."""
    module, plugin, _, store = _prepare_event(monkeypatch)
    writer = _FakeWriter()
    _install_writer(module, monkeypatch, writer)
    _FakeDirectoryHelper.reset(result=_tv_directory())

    plugin.on_download_added(_event("persisted"))

    assert [record["hash"] for record in _records(store)] == ["persisted"]
    assert set(_seen(store)) == {"persisted"}

    fresh = object.__new__(module.SubscribeDownloadAttr)
    fresh.systemconfig = _FakeSystemConfig([_directory("电视剧", "/mnt/media", "/tv", 0)])
    fresh.get_data = store.get_data
    fresh.save_data = store.save_data
    fresh.init_plugin(_hit_config())
    fresh_writer = _FakeWriter()
    _install_writer(module, monkeypatch, fresh_writer)
    _FakeDirectoryHelper.reset(result=_tv_directory())

    fresh.on_download_added(_event("persisted"))

    assert fresh_writer.tag_calls == []
    assert [record["status"] for record in _records(store)] == ["ok", "skipped"]
    assert "7 天" in _records(store)[-1]["reason"]


# ---------------- 详情页：只读记录表格与状态展示 ----------------


def _page_record(hash_value: str, status: str, *, time: str = "2026-11-15T08:00:00+00:00") -> dict[str, Any]:
    """构造一条与持久化结构一致的九字段处理记录."""
    return {
        "time": time,
        "hash": hash_value,
        "title": f"标题-{hash_value}",
        "directory_key": "/mnt/media:/tv",
        "level": "directory",
        "category": "电视剧",
        "tags": ["tv", hash_value],
        "status": status,
        "reason": f"原因-{hash_value}",
    }


def _page_events(page: Any) -> list[tuple[str, Any]]:
    """返回页面树中出现的全部事件绑定（onxxx 键）, 用于校验页面只读."""
    events: list[tuple[str, Any]] = []
    for node in _walk_form(page):
        for key, value in node.items():
            if key.startswith("on") and len(key) > 2:
                events.append((key, value))
    return events


def test_get_page_table_matches_record_fields(monkeypatch):
    """有记录时返回只读 VDataTable：九列表头与记录字段精确对应, items 最近优先且可序列化."""
    module, plugin, _, _ = _prepare(monkeypatch)
    store = _bind_store(plugin)
    records = [
        _page_record("h1", "ok", time="2026-11-15T08:00:00+00:00"),
        _page_record("h2", "failed", time="2026-11-15T09:00:00+00:00"),
        _page_record("h3", "dry_run", time="2026-11-15T10:00:00+00:00"),
    ]
    store.data[module.RECORDS_KEY] = copy.deepcopy(records)

    page = plugin.get_page()

    tables = _form_components(page, "VDataTable")
    assert len(tables) == 1
    props = tables[0]["props"]
    headers = props["headers"]
    # 表头与记录九个字段一一对应, 无缺列也无多余列
    assert [header["key"] for header in headers] == [
        "time",
        "hash",
        "title",
        "directory_key",
        "level",
        "category",
        "tags",
        "status",
        "reason",
    ]
    assert len(headers) == len(records[0]) == 9
    assert {header["key"] for header in headers} == set(records[0])
    assert all(isinstance(header["title"], str) and header["title"] for header in headers)
    # items 是记录的 JSON 安全拷贝, 按时间倒序（最近在前）且不改动业务取值
    assert props["items"] == list(reversed(records))
    assert [item["hash"] for item in props["items"]] == ["h3", "h2", "h1"]
    assert props["items"][0] is not store.data[module.RECORDS_KEY][-1]
    # 页面副本与持久化数据互相隔离
    props["items"][0]["status"] = "tampered"
    assert store.data[module.RECORDS_KEY][-1]["status"] == "dry_run"
    assert "最近在前" in _form_text(page)
    assert json.loads(json.dumps(page)) == page
    assert store.save_calls == []


def test_get_page_status_display_covers_all_five(monkeypatch):
    """五种状态映射齐全且可区分, dry_run 与 unsupported 显示为显式非成功."""
    module, plugin, _, _ = _prepare(monkeypatch)
    store = _bind_store(plugin)
    display = module.PAGE_STATUS_DISPLAY
    expected = ("ok", "unsupported", "failed", "skipped", "dry_run")
    assert tuple(module.PAGE_STATUS_ORDER) == expected
    assert set(display) == set(expected)

    labels = [display[status]["label"] for status in expected]
    colors = [display[status]["color"] for status in expected]
    assert len(set(labels)) == len(labels)
    assert len(set(colors)) == len(colors)
    assert display["ok"]["success"] is True
    assert all(display[status]["success"] is False for status in expected[1:])
    for status in ("dry_run", "unsupported"):
        assert "非成功" in display[status]["label"]
        assert display[status]["color"] != display["ok"]["color"]
    assert all(display[status]["hint"] for status in expected)

    store.data[module.RECORDS_KEY] = [_page_record(status, status) for status in expected]
    page = plugin.get_page()

    # 图例逐项标明原始状态值、显示标签与含义, dry_run/unsupported 不伪装成成功
    page_text = _form_text(page)
    for status in expected:
        assert f"{status}：{display[status]['label']}" in page_text
        assert display[status]["hint"] in page_text

    chips = _form_components(page, "VChip")
    assert [chip["props"]["text"] for chip in chips] == [f"{display[status]['label']} 1" for status in expected]
    assert [chip["props"]["color"] for chip in chips] == [display[status]["color"] for status in expected]
    assert "非成功" in chips[-1]["props"]["text"]


def test_get_page_read_only(monkeypatch):
    """详情页只读：不取 writer、不写插件数据、无事件绑定、模型绑定与编辑控件."""
    module, plugin, _, store = _prepare_event(monkeypatch)
    writer = _FakeWriter()
    writer_calls = _install_writer(module, monkeypatch, writer)
    store.data[module.RECORDS_KEY] = [_page_record("h1", "ok"), _page_record("h2", "dry_run")]
    store.save_calls.clear()

    page = plugin.get_page()

    assert writer_calls == []
    assert writer.read_calls == []
    assert writer.tag_calls == []
    assert writer.category_calls == []
    assert store.save_calls == []
    # 页面不提供任何点击事件、编辑绑定或输入控件
    assert _page_events(page) == []
    assert _form_models(page) == []
    assert {node["component"] for node in _walk_form(page)} == {"VAlert", "VRow", "VCol", "VChip", "VDataTable"}
    assert json.loads(json.dumps(page)) == page


def test_get_page_degrades_on_read_failure(monkeypatch):
    """记录读取抛异常时详情页降级为空态并记 warning, 不向上抛."""
    _, plugin, fake_logger, _ = _prepare(monkeypatch)

    def broken_get(key: Any = None) -> Any:
        raise RuntimeError("db down")

    plugin.get_data = broken_get

    page = plugin.get_page()

    assert [node["component"] for node in page] == ["VAlert"]
    assert page[0]["props"]["title"] == "暂无处理记录"
    assert any("读取处理记录失败" in message and "db down" in message for message in fake_logger.warnings)
    assert json.loads(json.dumps(page)) == page


def test_metadata_matches_index_entry():
    """索引条目必须与插件元数据、图标库一致, 且不声明旧代专属字段."""
    plugin_class = _load_plugin().SubscribeDownloadAttr
    index = json.loads((ROOT / "package.v3.json").read_text(encoding="utf-8"))
    entry = index[CLASS_NAME]

    assert entry["name"] == plugin_class.plugin_name
    assert entry["description"] == plugin_class.plugin_desc
    assert entry["version"] == plugin_class.plugin_version
    assert entry["author"] == plugin_class.plugin_author
    assert entry["icon"] == plugin_class.plugin_icon
    assert entry["level"] == plugin_class.auth_level
    assert entry["system_version"] == ">=3.2.0"
    # 标签为逗号分隔的小写英文词, 与仓库既有索引写法保持一致
    assert entry["labels"] == "download,subscribe,category,tag"
    # 新插件没有旧代条目, 索引既不应出现 v3 字段, 也不应指向旧代目录
    assert "v3" not in entry
    history = entry["history"]
    assert isinstance(history, dict) and history
    assert next(iter(history)) == plugin_class.plugin_version
    assert all(isinstance(note, str) and note.strip() for note in history.values())
    # 图标必须在共享图标库中, 且文件名大小写与索引完全一致
    icon_names = {item.name for item in (ROOT / "icons").iterdir()}
    assert plugin_class.plugin_icon in icon_names
    # 版本门禁按索引键定位插件目录, 目录必须存在
    assert (ROOT / "plugins.v3" / CLASS_NAME.lower()).is_dir()
