"""rules.py 纯逻辑层的 focused 单测.

只覆盖目录键、配置规范化、两级匹配与标签合并：不 mock 被测函数本身，不访问网络、
数据库或下载器，断言全部基于实际返回值、顺序与冲突结果。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from app.plugins.subscribedownloadattr import rules

DEFAULT_CONFIG = {
    "enabled": False,
    "trigger": "subscribe",
    "apply_tags": True,
    "apply_category": True,
    "keep_system_tag": True,
    "dry_run": False,
    "default_rule": {"category": "", "tags": []},
    "directory_rules": {},
}


class _Directory:
    """模拟宿主适配后可能传入的同名属性对象（不是宿主类型）."""

    def __init__(self, name: str, storage: str, download_path: str, priority: int = 0) -> None:
        self.name = name
        self.storage = storage
        self.download_path = download_path
        self.priority = priority


def _imported_modules() -> list[str]:
    """返回 rules.py 中出现的所有导入模块名."""
    source = Path(str(rules.__file__))
    tree = ast.parse(source.read_text(encoding="utf-8"))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return modules


# ---------------- 目录键 ----------------


def test_directory_key_priority():
    """完整键优先, 缺失 storage/path 回退 name, name 也缺失才用 #序号."""
    full = {"name": "电视剧", "storage": "/mnt/media", "download_path": "/tv"}
    assert rules.directory_key(full, 1) == "/mnt/media:/tv"

    # 同名不同路径 -> 不同完整键, 规则各自独立
    other = {"name": "电视剧", "storage": "/mnt/other", "download_path": "/tv"}
    assert rules.directory_key(other, 2) != rules.directory_key(full, 1)

    # 缺 storage 或缺 download_path 都直接回退 name, 不用单 storage 拼中间键
    assert rules.directory_key({"name": "纪录片", "download_path": "/doc"}, 3) == "纪录片"
    assert rules.directory_key({"name": "纪录片", "storage": "/mnt"}, 4) == "纪录片"

    # 两级都不可用 -> #序号
    assert rules.directory_key({"name": "  "}, 5) == "#5"
    assert rules.directory_key({}, 6) == "#6"


def test_directory_key_fallback_sequence():
    """#序号取调用方给的 1-based 位置, 函数自身不排序也不重排."""
    entries = [{"name": ""}, {"name": None}, {}]
    keys = [rules.directory_key(entry, index) for index, entry in enumerate(entries, 1)]

    assert keys == ["#1", "#2", "#3"]
    # 传入什么序号就用什么序号, 不受列表位置影响
    assert rules.directory_key({}, 7) == "#7"
    # 空串与非字符串字段一律按缺失处理
    assert rules.directory_key({"storage": "", "download_path": "", "name": "电影"}, 1) == "电影"
    assert rules.directory_key({"storage": 1, "download_path": 2, "name": "电影"}, 1) == "电影"


def test_directory_key_normalization():
    """strip、反斜杠归一、去尾部斜杠、根路径保留, 且不折叠大小写."""
    entry = {"storage": "  /mnt/media/  ", "download_path": "\\tv\\", "name": "x"}
    assert rules.directory_key(entry, 1) == "/mnt/media:/tv"

    # 根路径不能被削成空串
    assert rules.directory_key({"storage": "/mnt/", "download_path": "/"}, 1) == "/mnt:/"

    tv = {"name": "tv", "storage": "/mnt", "download_path": "/TV"}
    lower = {"name": "tv", "storage": "/mnt", "download_path": "/tv"}
    assert rules.directory_key(tv, 1) == "/mnt:/TV"
    assert rules.directory_key(lower, 2) == "/mnt:/tv"
    assert rules.directory_key(tv, 1) != rules.directory_key(lower, 2)


def test_directory_key_conflict_keeps_first():
    """完整键重复时保留首条有效项, 其余进冲突列表且冲突可见."""
    entries = [
        {"name": "A", "storage": "/mnt", "download_path": "/tv", "priority": 1},
        {"name": "B", "storage": "/mnt", "download_path": "/tv/", "priority": 5},
        {"name": "C", "storage": "/mnt", "download_path": "/doc", "priority": 9},
    ]

    resolved = rules.resolve_directory_keys(entries)

    assert resolved["effective_keys"] == {"/mnt:/tv": 1, "/mnt:/doc": 3}
    assert [item["effective"] for item in resolved["entries"]] == [True, False, True]
    assert len(resolved["conflicts"]) == 1

    conflict = resolved["conflicts"][0]
    assert conflict["key"] == "/mnt:/tv"
    assert conflict["count"] == 2
    assert conflict["first_sequence"] == 1
    assert conflict["names"] == ["A", "B"]
    assert conflict["priorities"] == [1, 5]
    assert conflict["sequences"] == [1, 2]

    # 结果必须是普通结构, 可直接 json.dumps 供配置页使用
    assert json.loads(json.dumps(resolved)) == resolved


def test_resolve_directory_keys_reads_plain_objects():
    """目录键解析同时支持普通 dict 与具有同名属性的普通对象."""
    resolved = rules.resolve_directory_keys([_Directory("电影", "/mnt", "/movie", 3)])

    assert resolved["entries"][0]["key"] == "/mnt:/movie"
    assert resolved["entries"][0]["name"] == "电影"
    assert resolved["entries"][0]["priority"] == 3
    assert resolved["effective_keys"] == {"/mnt:/movie": 1}
    assert resolved["conflicts"] == []


def test_resolve_directory_keys_empty_input():
    """空输入与非目录输入不抛异常, 返回空结构."""
    for entries in (None, [], "not-a-list", 42):
        resolved = rules.resolve_directory_keys(entries)
        assert resolved == {"entries": [], "effective_keys": {}, "conflicts": []}


# ---------------- 配置规范化 ----------------


def test_normalize_config_defaults():
    """空配置补齐八个默认键, 键顺序与配置模型一致且可 json.dumps."""
    normalized, issues = rules.normalize_config(None)

    assert tuple(normalized) == rules.CONFIG_KEYS
    assert normalized == DEFAULT_CONFIG
    assert issues == []
    assert "_issues" not in normalized
    assert json.loads(json.dumps(normalized)) == normalized


def test_normalize_config_tolerant():
    """None、字符串、列表与其它错误类型都不抛异常, 且非对象输入记 issue."""
    normalized, issues = rules.normalize_config(None)
    assert normalized == DEFAULT_CONFIG
    assert issues == []

    for bad in ("enabled=false", ["enabled"], 42, object(), (("enabled", False),)):
        normalized, issues = rules.normalize_config(bad)
        assert tuple(normalized) == rules.CONFIG_KEYS
        assert "_issues" not in normalized
        assert normalized["enabled"] is False
        assert issues and isinstance(issues[0], str)
        assert json.loads(json.dumps(normalized)) == normalized


def test_normalize_config_bool_and_trigger():
    """布尔项只接受明确语义, 非法值回退默认; trigger 只接受 subscribe/all."""
    normalized, issues = rules.normalize_config(
        {
            "enabled": "false",
            "apply_tags": "0",
            "apply_category": "TRUE",
            "keep_system_tag": 1,
            "dry_run": "not-a-bool",
            "trigger": " ALL ",
        }
    )

    assert normalized["enabled"] is False
    assert normalized["apply_tags"] is False
    assert normalized["apply_category"] is True
    assert normalized["keep_system_tag"] is True
    # 任意非空字符串不得被当成 True
    assert normalized["dry_run"] is False
    assert normalized["trigger"] == "all"
    assert len(issues) == 1
    assert "dry_run" in issues[0]

    normalized, issues = rules.normalize_config({"enabled": 2, "trigger": "manual"})
    assert normalized["enabled"] is False
    assert normalized["trigger"] == "subscribe"
    assert len(issues) == 2


def test_normalize_config_rules():
    """规则值规范为 category/tags, 旧逗号串与坏条目都被稳定处理."""
    normalized, issues = rules.normalize_config(
        {
            "default_rule": {"category": " 电影 ", "tags": " a , b,,"},
            "directory_rules": {
                " /mnt:/tv ": {"category": 123, "tags": ["tv", "", "tv", 5, None, ["x"]]},
                "": {"category": "空键"},
                "ok": {"tags": ("a", "a", "b")},
                "broken": "not-a-rule",
                "/mnt:/tv": {"category": "重复键"},
            },
        }
    )

    assert normalized["default_rule"] == {"category": "电影", "tags": ["a", "b"]}

    directory_rules = normalized["directory_rules"]
    assert set(directory_rules) == {"/mnt:/tv", "ok"}
    assert directory_rules["/mnt:/tv"] == {"category": "", "tags": ["tv", "5"]}
    assert directory_rules["ok"] == {"category": "", "tags": ["a", "b"]}

    # 非法分类、非法标签项、空键、坏规则值、strip 后重复键各记一条 issue
    assert len(issues) == 6
    assert all("_issues" not in key for key in normalized)

    normalized, issues = rules.normalize_config({"directory_rules": ["x"]})
    assert normalized["directory_rules"] == {}
    assert len(issues) == 1

    normalized, issues = rules.normalize_config({"default_rule": {"tags": {"b", "a"}}})
    assert normalized["default_rule"] == {"category": "", "tags": ["a", "b"]}
    assert issues == []


def test_normalize_config_legacy_category_rules_ignored():
    """旧配置的 category_rules 不再进入模型, 只记提示且不影响其余配置."""
    normalized, issues = rules.normalize_config(
        {
            "enabled": False,
            "category_rules": [{"directory": "/mnt:/tv", "media_type": "电视剧", "category": "anime"}],
        }
    )

    assert "category_rules" not in normalized
    assert tuple(normalized) == rules.CONFIG_KEYS
    assert normalized["enabled"] is False
    assert len(issues) == 1
    assert "category_rules" in issues[0] and "不再使用" in issues[0]
    assert json.loads(json.dumps(normalized)) == normalized

    # 旧值即使是非法 JSON 字符串也只按忽略处理, 不额外报解析错误
    normalized, issues = rules.normalize_config({"category_rules": "{not json"})
    assert "category_rules" not in normalized
    assert len(issues) == 1
    assert "不再使用" in issues[0]


def test_normalize_config_flat_directory_rules():
    """点号扁平目录规则还原为嵌套, 目录键可含 . : / 且不被错误拆分."""
    normalized, issues = rules.normalize_config(
        {
            "directory_rules.local:/data/downloads/番剧.category": "番剧",
            "directory_rules.local:/data/downloads/番剧.tags": ["anime", ""],
            "directory_rules./mnt:/tv.category": "电视剧",
            "directory_rules.my.dir.category": "动画",
        }
    )

    assert normalized["directory_rules"] == {
        "local:/data/downloads/番剧": {"category": "番剧", "tags": ["anime"]},
        "/mnt:/tv": {"category": "电视剧", "tags": []},
        "my.dir": {"category": "动画", "tags": []},
    }
    assert issues == []
    assert tuple(normalized) == rules.CONFIG_KEYS
    assert json.loads(json.dumps(normalized)) == normalized


def test_normalize_config_flat_default_rule_fills_empty_nested():
    """嵌套默认规则字段为空时由扁平值补入, 不丢默认标签."""
    normalized, issues = rules.normalize_config(
        {
            "default_rule": {"category": "", "tags": []},
            "default_rule.tags": ["BT"],
        }
    )

    assert normalized["default_rule"] == {"category": "", "tags": ["BT"]}
    assert issues == []


def test_normalize_config_flat_conflicts_nested_wins():
    """嵌套与扁平两处都有不同值时以嵌套为准并记 issue."""
    normalized, issues = rules.normalize_config(
        {
            "default_rule": {"category": "电影", "tags": ["A"]},
            "default_rule.category": "番剧",
            "default_rule.tags": ["B"],
            "directory_rules": {"/mnt:/tv": {"category": "电视剧", "tags": ["tv"]}},
            "directory_rules./mnt:/tv.category": "番剧",
            "directory_rules./mnt:/tv.tags": ["anime"],
        }
    )

    assert normalized["default_rule"] == {"category": "电影", "tags": ["A"]}
    assert normalized["directory_rules"] == {"/mnt:/tv": {"category": "电视剧", "tags": ["tv"]}}
    assert len(issues) == 3
    assert all("已以表单中的值为准" in issue for issue in issues)
    # 面向用户的文案不使用嵌套/扁平等技术术语
    assert all("嵌套" not in issue and "扁平" not in issue for issue in issues)
    assert any("默认分类" in issue for issue in issues)
    assert any("默认标签" in issue for issue in issues)
    assert any("/mnt:/tv" in issue for issue in issues)


def test_normalize_config_flat_same_value_is_silent():
    """宿主同时写入嵌套与扁平同名值时视为同一配置, 不报冲突."""
    normalized, issues = rules.normalize_config(
        {
            "default_rule": {"category": "", "tags": ["BT"]},
            "default_rule.tags": ["BT"],
        }
    )

    assert normalized["default_rule"] == {"category": "", "tags": ["BT"]}
    assert issues == []


def test_normalize_config_flat_same_directory_rule_is_silent():
    """目录规则嵌套与扁平内容相同时不报冲突, 规则保持不变."""
    normalized, issues = rules.normalize_config(
        {
            "directory_rules": {"local:/data/downloads/番剧": {"category": "番剧", "tags": ["anime"]}},
            "directory_rules.local:/data/downloads/番剧.category": "番剧",
            "directory_rules.local:/data/downloads/番剧.tags": ["anime"],
        }
    )

    assert normalized["directory_rules"] == {"local:/data/downloads/番剧": {"category": "番剧", "tags": ["anime"]}}
    assert issues == []


def test_normalize_config_flat_same_category_is_silent():
    """默认分类嵌套与扁平值相同时不报冲突."""
    normalized, issues = rules.normalize_config(
        {
            "default_rule": {"category": "番剧", "tags": []},
            "default_rule.category": "番剧",
        }
    )

    assert normalized["default_rule"] == {"category": "番剧", "tags": []}
    assert issues == []


def test_normalize_config_flat_unknown_field_ignored():
    """扁平键字段名非 category/tags 时忽略并记 issue."""
    normalized, issues = rules.normalize_config(
        {
            "default_rule.unknown": "x",
            "directory_rules./mnt:/tv.unknown": "y",
        }
    )

    assert normalized["default_rule"] == {"category": "", "tags": []}
    assert normalized["directory_rules"] == {}
    assert len(issues) == 2
    assert all("不是受支持的" in issue for issue in issues)


def test_normalize_config_flat_real_machine_shape():
    """实机扁平配置还原后命中目录级, 不再退回 default 空规则."""
    normalized, issues = rules.normalize_config(
        {
            "enabled": True,
            "trigger": "subscribe",
            "directory_rules": {},
            "default_rule": {"category": "", "tags": []},
            "default_rule.tags": ["BT"],
            "directory_rules.local:/data/downloads/番剧.category": "番剧",
            "directory_rules.local:/data/downloads/番剧.tags": ["anime"],
        }
    )

    assert normalized["directory_rules"] == {"local:/data/downloads/番剧": {"category": "番剧", "tags": ["anime"]}}
    assert normalized["default_rule"] == {"category": "", "tags": ["BT"]}
    assert issues == []
    assert tuple(normalized) == rules.CONFIG_KEYS

    matched = rules.match_rule(normalized, "local:/data/downloads/番剧")
    assert matched["level"] == "directory"
    assert matched["matched"] is True
    assert matched["rule"] == {"category": "番剧", "tags": ["anime"]}


# ---------------- 两级匹配 ----------------


def _match_config() -> dict:
    """构造覆盖目录级与默认级的配置样本."""
    return {
        "default_rule": {"category": "default", "tags": ["default"]},
        "directory_rules": {"/mnt:/tv": {"category": "电视剧", "tags": ["tv"]}},
    }


def test_match_rule_priority():
    """目录 > 默认, 目录未配置时向默认级回落."""
    config = _match_config()

    hit = rules.match_rule(config, "/mnt:/tv")
    assert hit == {
        "level": "directory",
        "matched": True,
        "rule": {"category": "电视剧", "tags": ["tv"]},
        "rule_index": None,
    }

    # 目录未配置 -> 默认级
    hit = rules.match_rule(config, "/mnt:/doc")
    assert hit["level"] == "default"
    assert hit["rule"] == {"category": "default", "tags": ["default"]}

    # 无目录键时仍可使用默认规则
    hit = rules.match_rule(config, None)
    assert hit["level"] == "default"
    hit = rules.match_rule(config, "   ")
    assert hit["level"] == "default"


def test_match_rule_directory_empty_fields_fall_back_to_default():
    """目录键命中后逐字段兜底：分类/标签为空分别取默认规则对应字段."""
    config = {
        "default_rule": {"category": "default", "tags": ["default"]},
        "directory_rules": {"/k": {"category": "", "tags": []}},
    }

    # 目录规则两字段都为空 -> 全部回退默认规则, 但仍停在目录级
    hit = rules.match_rule(config, "/k")
    assert hit["level"] == "directory"
    assert hit["matched"] is True
    assert hit["rule"] == {"category": "default", "tags": ["default"]}

    # 只缺分类 -> 取默认分类, 标签仍用目录值
    partial_category = {
        "default_rule": {"category": "default", "tags": ["default"]},
        "directory_rules": {"/k": {"category": "", "tags": ["tv"]}},
    }
    hit = rules.match_rule(partial_category, "/k")
    assert hit["level"] == "directory"
    assert hit["rule"] == {"category": "default", "tags": ["tv"]}

    # 只缺标签 -> 取默认标签, 分类仍用目录值
    partial_tags = {
        "default_rule": {"category": "default", "tags": ["default"]},
        "directory_rules": {"/k": {"category": "电视剧", "tags": []}},
    }
    hit = rules.match_rule(partial_tags, "/k")
    assert hit["level"] == "directory"
    assert hit["rule"] == {"category": "电视剧", "tags": ["default"]}

    # 目录键未命中 -> 默认级, 规则即默认规则
    hit = rules.match_rule(config, "/other")
    assert hit["level"] == "default"
    assert hit["rule"] == {"category": "default", "tags": ["default"]}


def test_match_rule_without_any_rule():
    """没有任何规则时返回默认级空规则, 行为稳定可断言."""
    hit = rules.match_rule({}, "/k")

    assert hit == {
        "level": "default",
        "matched": False,
        "rule": {"category": "", "tags": []},
        "rule_index": None,
    }


# ---------------- 标签合并 ----------------


def test_merge_tags():
    """去空去重保持首次顺序, 不折叠大小写, keep_system_tag 控制系统标签."""
    assert rules.merge_tags(["tv", "TV", " tv "], ["tv", "anime"], True) == ["tv", "TV", "anime"]
    assert rules.merge_tags(["MOVIEPILOT", " tv "], "tv, anime", True) == ["MOVIEPILOT", "tv", "anime"]
    assert rules.merge_tags("MOVIEPILOT, tv", "anime", False) == ["tv", "anime"]
    assert rules.merge_tags(None, None, True) == []
    assert rules.merge_tags(["a", "", "   "], ",,b,", True) == ["a", "b"]

    # system_tag 可由调用方覆盖（P0-04 传 settings.TORRENT_TAG）
    assert rules.merge_tags(["CUSTOM"], ["x"], True, system_tag="CUSTOM") == ["CUSTOM", "x"]
    assert rules.merge_tags(["CUSTOM"], ["x"], False, system_tag="CUSTOM") == ["x"]
    # 不传 system_tag 时按默认 MOVIEPILOT 处理
    assert rules.merge_tags(["MOVIEPILOT"], [], False) == []


# ---------------- 依赖边界 ----------------


def test_rules_module_is_dependency_free():
    """rules.py 只允许标准库导入, 不得依赖宿主 app.* 或第三方包."""
    modules = _imported_modules()

    assert [name for name in modules if name.startswith("app")] == []
    assert set(modules) <= {"__future__", "collections.abc", "typing"}
