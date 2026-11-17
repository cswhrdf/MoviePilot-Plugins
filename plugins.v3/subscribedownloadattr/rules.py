"""规则纯逻辑层：目录键、配置规范化、两级匹配与标签合并.

本模块只做纯计算：不导入任何宿主模块（``app.*``）、不引入第三方依赖、不读写配置或
下载器，便于配置页、事件处理与单测共用同一套目录键与匹配实现。所有公开入口对错误
输入都降级处理，不向上抛异常；坏条目的原因写入 ``issues``，由配置页展示。

两级匹配顺序为「目录 → 默认」：目录键命中目录规则时按字段兜底（分类/标签为空
分别回退默认规则对应字段），目录未命中时使用默认规则。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "CONFIG_KEYS",
    "DEFAULT_SYSTEM_TAG",
    "directory_key",
    "match_rule",
    "merge_tags",
    "normalize_config",
    "resolve_directory_keys",
]

# 系统标签默认值；P0-04 会把 settings.TORRENT_TAG 显式传入，纯逻辑层不读取设置
DEFAULT_SYSTEM_TAG = "MOVIEPILOT"

# 规范化后配置的八个键，顺序与配置模型（第 5.1 节）一致
CONFIG_KEYS = (
    "enabled",
    "trigger",
    "apply_tags",
    "apply_category",
    "keep_system_tag",
    "dry_run",
    "default_rule",
    "directory_rules",
)

_TRIGGERS = ("subscribe", "all")
_TRUE_TEXT = frozenset({"true", "1", "yes", "on"})
_FALSE_TEXT = frozenset({"false", "0", "no", "off"})


# ---------------- 通用字段读取 ----------------


def _read_value(entry: Any, key: str) -> Any:
    """按映射键或同名属性读取字段, 两者都缺失时返回 None."""
    if isinstance(entry, Mapping):
        return entry.get(key)
    return getattr(entry, key, None)


def _text(value: Any) -> str | None:
    """把字符串字段 strip 后返回, 非字符串与空串一律按缺失返回 None."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _plain_text(value: Any) -> str:
    """把可选字符串字段规范为 strip 后的字符串或空串."""
    return _text(value) or ""


def _path_text(value: Any) -> str | None:
    """规范化目录字段：分隔符归一为 /、去掉尾部 /, 根路径 / 保留."""
    text = _text(value)
    if text is None:
        return None
    text = text.replace("\\", "/")
    while len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    return text


def _priority_value(entry: Any) -> int:
    """读取目录 priority, 无法解析时按宿主语义归零."""
    value = _read_value(entry, "priority")
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return 0
    return 0


def _entry_list(entries: Any) -> list[Any]:
    """把目录集合转成列表, 不可迭代或像字符串/映射的输入按空列表处理."""
    if entries is None or isinstance(entries, (str, bytes, Mapping)):
        return []
    try:
        return list(entries)
    except TypeError:
        return []


# ---------------- 目录键 ----------------


def directory_key(entry: Any, sequence_index: int) -> str:
    """按「完整键 → name → #序号」三级规则生成目录规则键.

    Args:
        entry: 宿主目录条目, 支持普通 dict 或具有同名属性的普通对象.
        sequence_index: 调用方在 priority 升序序列中给出的 1-based 位置, 本函数不排序.

    Returns:
        ``storage:download_path`` 完整键；任一字段缺失时回退 ``name``；``name`` 也缺失
        时返回 ``#序号``.

    """
    storage = _path_text(_read_value(entry, "storage"))
    download_path = _path_text(_read_value(entry, "download_path"))
    if storage is not None and download_path is not None:
        return f"{storage}:{download_path}"
    name = _text(_read_value(entry, "name"))
    if name is not None:
        return name
    return f"#{sequence_index}"


def resolve_directory_keys(entries: Any) -> dict[str, Any]:
    """解析目录键序列：同键只保留首条有效项, 其余记入冲突列表.

    Args:
        entries: 已按宿主 priority 升序排列的目录条目列表, 本函数不再排序.

    Returns:
        可 ``json.dumps`` 的普通 dict：``entries`` 为逐条结果（sequence/key/name/
        storage/download_path/priority/effective）, ``effective_keys`` 为键到首条
        序号的映射, ``conflicts`` 为重复键详情（键、涉及目录名、优先级与序号）.

    """
    ordered: list[dict[str, Any]] = []
    effective: dict[str, int] = {}
    for sequence, entry in enumerate(_entry_list(entries), 1):
        key = directory_key(entry, sequence)
        is_first = key not in effective
        if is_first:
            effective[key] = sequence
        ordered.append(
            {
                "sequence": sequence,
                "key": key,
                "name": _text(_read_value(entry, "name")) or "",
                "storage": _path_text(_read_value(entry, "storage")),
                "download_path": _path_text(_read_value(entry, "download_path")),
                "priority": _priority_value(entry),
                "effective": is_first,
            }
        )

    conflicts: list[dict[str, Any]] = []
    for key, first_sequence in effective.items():
        duplicated = [item for item in ordered if item["key"] == key]
        if len(duplicated) < 2:
            continue
        conflicts.append(
            {
                "key": key,
                "count": len(duplicated),
                "first_sequence": first_sequence,
                "names": [item["name"] for item in duplicated],
                "priorities": [item["priority"] for item in duplicated],
                "sequences": [item["sequence"] for item in duplicated],
            }
        )
    return {"entries": ordered, "effective_keys": effective, "conflicts": conflicts}


# ---------------- 配置规范化 ----------------


def _config_mapping(config: Any, issues: list[str]) -> dict[str, Any]:
    """把配置整体规范为普通 dict, None 视为空配置, 其它非对象类型记 issue."""
    if isinstance(config, Mapping):
        return dict(config)
    if config is not None:
        issues.append(f"配置整体应为对象，实际为 {type(config).__name__}，已按空配置处理")
    return {}


def _coerce_bool(value: Any) -> bool | None:
    """把明确布尔语义的输入转换为 bool, 无法判定时返回 None."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value)
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_TEXT:
            return True
        if text in _FALSE_TEXT:
            return False
    return None


def _bool_field(raw: Mapping[str, Any], name: str, default: bool, issues: list[str]) -> bool:
    """读取布尔配置项, 缺失取默认值, 类型非法回退默认值并记录原因."""
    if name not in raw:
        return default
    value = raw[name]
    coerced = _coerce_bool(value)
    if coerced is None:
        issues.append(f"配置项 {name} 的值 {value!r} 不是合法布尔值，已回退默认值 {default}")
        return default
    return coerced


def _trigger_field(raw: Mapping[str, Any], issues: list[str]) -> str:
    """读取触发来源配置, 缺失与非法值都回退 subscribe."""
    if "trigger" not in raw:
        return "subscribe"
    value = raw["trigger"]
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRIGGERS:
            return text
    issues.append(f"配置项 trigger 的值 {value!r} 不受支持，已回退 subscribe")
    return "subscribe"


def _collect_tag_items(value: Any) -> list[Any] | None:
    """把标签输入展开为待处理元素, 不支持的类型返回 None."""
    if value is None:
        return []
    if isinstance(value, str):
        return value.split(",")
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    if isinstance(value, (list, tuple)):
        return list(value)
    return None


def _scalar_text(value: Any) -> str | None:
    """把可安全转换的标量转成字符串, 其它类型返回 None."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _coerce_tags(value: Any, label: str, issues: list[str]) -> list[str]:
    """规范化标签：支持 list/tuple/set 与逗号串, 去空去重并保持首次出现顺序."""
    items = _collect_tag_items(value)
    if items is None:
        issues.append(f"{label} 的值类型 {type(value).__name__} 不受支持，已按空标签处理")
        return []
    tags: list[str] = []
    for index, item in enumerate(items, 1):
        text = _scalar_text(item)
        if text is None:
            issues.append(f"{label} 第 {index} 项类型 {type(item).__name__} 不受支持，已跳过")
            continue
        text = text.strip()
        if text and text not in tags:
            tags.append(text)
    return tags


def _empty_rule() -> dict[str, Any]:
    """返回空规则默认值."""
    return {"category": "", "tags": []}


def _rule(value: Any, label: str, issues: list[str]) -> dict[str, Any]:
    """把单条规则规范为 ``{category: str, tags: list[str]}``."""
    if not isinstance(value, Mapping):
        issues.append(f"{label} 应为对象，实际为 {type(value).__name__}，已按空规则处理")
        return _empty_rule()
    category = value.get("category")
    if category is None:
        category_text = ""
    elif isinstance(category, str):
        category_text = category.strip()
    else:
        issues.append(f"{label}.category 应为字符串，实际为 {type(category).__name__}，已按空分类处理")
        category_text = ""
    return {"category": category_text, "tags": _coerce_tags(value.get("tags"), f"{label}.tags", issues)}


def _rule_field(raw: Mapping[str, Any], name: str, issues: list[str]) -> dict[str, Any]:
    """读取单条规则配置项（如 ``default_rule``）."""
    if name not in raw or raw[name] is None:
        return _empty_rule()
    return _rule(raw[name], name, issues)


def _directory_rules_field(raw: Mapping[str, Any], issues: list[str]) -> dict[str, Any]:
    """读取目录规则映射, 空键、重复键与非法条目跳过并记录原因."""
    if "directory_rules" not in raw or raw["directory_rules"] is None:
        return {}
    value = raw["directory_rules"]
    if not isinstance(value, Mapping):
        issues.append(f"配置项 directory_rules 应为对象，实际为 {type(value).__name__}，已按空规则处理")
        return {}
    rules: dict[str, Any] = {}
    for key, rule_value in value.items():
        if not isinstance(key, str):
            issues.append(f"目录规则的键应为字符串，实际为 {type(key).__name__}，已跳过")
            continue
        stripped = key.strip()
        if not stripped:
            issues.append("目录规则存在空目录键，已跳过")
            continue
        if stripped in rules:
            issues.append(f"目录规则键 {stripped!r} 重复，保留首次出现的规则")
            continue
        if not isinstance(rule_value, Mapping):
            issues.append(f"目录规则 {stripped!r} 的值应为对象，实际为 {type(rule_value).__name__}，已跳过")
            continue
        rules[stripped] = _rule(rule_value, f"directory_rules[{stripped}]", issues)
    return rules


_DEFAULT_RULE_FLAT_PREFIX = "default_rule."
_DIRECTORY_RULES_FLAT_PREFIX = "directory_rules."
_RULE_FIELDS = ("category", "tags")


def _is_empty_rule_value(value: Any) -> bool:
    """判断规则字段是否为空值：None、空串与空序列都视为空."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, frozenset)):
        return not value
    return False


def _pop_flat_rules(raw: dict[str, Any], issues: list[str]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """摘除点号扁平键, 返回 (默认规则字段, 目录规则字段) 并就地删除扁平键."""
    flat_default: dict[str, Any] = {}
    flat_directory: dict[str, dict[str, Any]] = {}
    for key in list(raw):
        if not isinstance(key, str):
            continue
        if key.startswith(_DEFAULT_RULE_FLAT_PREFIX):
            field = key[len(_DEFAULT_RULE_FLAT_PREFIX) :]
            if field in _RULE_FIELDS:
                flat_default[field] = raw[key]
            else:
                issues.append(f"配置项 {key!r} 不是受支持的默认规则字段，已忽略")
            del raw[key]
        elif key.startswith(_DIRECTORY_RULES_FLAT_PREFIX):
            remainder = key[len(_DIRECTORY_RULES_FLAT_PREFIX) :]
            head, sep, field = remainder.rpartition(".")
            if sep and head and field in _RULE_FIELDS:
                flat_directory.setdefault(head, {})[field] = raw[key]
            else:
                issues.append(f"配置项 {key!r} 不是受支持的目录规则字段，已忽略")
            del raw[key]
    return flat_default, flat_directory


def _merge_default_rule(nested: Any, flat: Mapping[str, Any], issues: list[str]) -> Any:
    """把扁平默认规则字段合并进嵌套默认规则, 嵌套字段非空时以嵌套为准."""
    if not isinstance(nested, Mapping):
        return dict(flat)
    merged = dict(nested)
    for field, value in flat.items():
        current = merged.get(field)
        if _is_empty_rule_value(current):
            merged[field] = value
        elif not _is_empty_rule_value(value):
            issues.append(f"配置项 default_rule.{field} 在嵌套与扁平两处都有值，已以嵌套为准")
    return merged


def _merge_directory_rules(nested: Any, flat: Mapping[str, dict[str, Any]], issues: list[str]) -> Any:
    """把扁平目录规则合并进嵌套目录规则, 同键以嵌套为准并记 issue."""
    if not isinstance(nested, Mapping):
        return dict(flat)
    merged = dict(nested)
    for dir_key, rule in flat.items():
        if dir_key in merged:
            issues.append(f"目录规则 {dir_key!r} 在嵌套与扁平两处都有值，已以嵌套为准")
        else:
            merged[dir_key] = rule
    return merged


def _expand_flat_config(raw: Mapping[str, Any], issues: list[str]) -> dict[str, Any]:
    """把点号扁平配置键还原为嵌套的 default_rule 与 directory_rules.

    宿主保存表单时可能把 ``default_rule.category`` / ``default_rule.tags`` /
    ``directory_rules.<目录键>.category`` / ``directory_rules.<目录键>.tags`` 存成
    顶层扁平键。目录键本身可含 ``.`` / ``:`` / ``/``，因此目录规则按「最后一个 ``.``
    之前为目录键、其后为字段名」拆分；字段名只接受 ``category`` / ``tags``。

    展开后扁平键不再保留；同一目录键在嵌套与扁平两处都存在时以嵌套为准并记一条
    issue；默认规则按字段合并，嵌套字段为空时由扁平值补入，两处都有值且不一致时
    以嵌套为准并记 issue。

    """
    result = dict(raw)
    flat_default, flat_directory = _pop_flat_rules(result, issues)
    if flat_default:
        result["default_rule"] = _merge_default_rule(result.get("default_rule"), flat_default, issues)
    if flat_directory:
        result["directory_rules"] = _merge_directory_rules(result.get("directory_rules"), flat_directory, issues)
    return result


def normalize_config(config: Any) -> tuple[dict[str, Any], list[str]]:
    """把任意输入规范化为八个配置键与问题列表.

    Args:
        config: 插件原始配置, 支持 dict 与 None；字符串、列表等非对象类型按空配置处理.

    Returns:
        ``(normalized, issues)`` 二元组. ``normalized`` 的键集合与配置模型一致且可
        ``json.dumps``；坏条目、被跳过条目的原因只写入 ``issues``, 不混入 ``normalized``.
        宿主持久化的点号扁平键会先还原为嵌套结构再校验；旧配置里的 ``category_rules``
        不再使用, 只记一条提示, 不会成为配置键.

    """
    issues: list[str] = []
    raw = _expand_flat_config(_config_mapping(config, issues), issues)
    if "category_rules" in raw:
        issues.append("配置项 category_rules 已不再使用（目录规则直接读取系统目录），本次已忽略")
    normalized: dict[str, Any] = {
        "enabled": _bool_field(raw, "enabled", True, issues),
        "trigger": _trigger_field(raw, issues),
        "apply_tags": _bool_field(raw, "apply_tags", True, issues),
        "apply_category": _bool_field(raw, "apply_category", True, issues),
        "keep_system_tag": _bool_field(raw, "keep_system_tag", True, issues),
        "dry_run": _bool_field(raw, "dry_run", False, issues),
        "default_rule": _rule_field(raw, "default_rule", issues),
        "directory_rules": _directory_rules_field(raw, issues),
    }
    return normalized, issues


# ---------------- 规则匹配 ----------------


def _match_result(level: str, rule: Mapping[str, Any]) -> dict[str, Any]:
    """构造匹配结果：命中层级、规则副本与是否含有效内容."""
    copied = {"category": _plain_text(rule.get("category")), "tags": list(rule.get("tags") or [])}
    return {
        "level": level,
        "matched": bool(copied["category"] or copied["tags"]),
        "rule": copied,
        "rule_index": None,
    }


def _merge_rule_fields(default_rule: Mapping[str, Any], directory_rule: Mapping[str, Any]) -> dict[str, Any]:
    """目录规则字段为空时用默认规则兜底, 非空字段以目录规则为准."""
    category = directory_rule.get("category") or default_rule.get("category") or ""
    tags = directory_rule.get("tags") or default_rule.get("tags") or []
    return {"category": category, "tags": list(tags)}


def match_rule(config: Any, directory_key: str | None = None) -> dict[str, Any]:
    """按「目录 → 默认」两级顺序匹配规则.

    Args:
        config: 插件配置, 可以是 ``normalize_config`` 的结果或原始 dict.
        directory_key: 目录键, 为空时跳过目录级、直接使用默认规则.

    Returns:
        含 ``level``（``directory`` / ``default``）、``matched``、``rule``、
        ``rule_index`` 的普通 dict；``rule_index`` 保留字段以兼容记录结构, 两级匹配均
        固定为 None. 目录规则命中时按字段兜底：分类/标签为空分别回退默认规则对应字段.

    """
    normalized, _ = normalize_config(config)
    key = _text(directory_key)
    default_rule = normalized["default_rule"]

    if key is not None:
        directory_rule = normalized["directory_rules"].get(key)
        if directory_rule is not None:
            return _match_result("directory", _merge_rule_fields(default_rule, directory_rule))

    return _match_result("default", default_rule)


# ---------------- 标签合并 ----------------


def _unique_tags(value: Any) -> list[str]:
    """规范化标签输入, 坏项直接丢弃（不收集 issue）."""
    return _coerce_tags(value, "tags", [])


def merge_tags(
    base_tags: Any,
    rule_tags: Any,
    keep_system_tag: bool = True,
    system_tag: str = DEFAULT_SYSTEM_TAG,
) -> list[str]:
    """合并任务既有标签与规则标签, 去空去重并处理系统标签.

    Args:
        base_tags: 任务既有标签, 支持 list/tuple/set、逗号分隔字符串与 None.
        rule_tags: 规则给出的标签, 类型同 ``base_tags``.
        keep_system_tag: 为 False 时从结果中移除 ``system_tag``.
        system_tag: 系统标签值, P0-04 传入 ``settings.TORRENT_TAG``.

    Returns:
        合并后的标签列表, 保持首次出现顺序, 不折叠大小写.

    """
    merged = _unique_tags(base_tags)
    for tag in _unique_tags(rule_tags):
        if tag not in merged:
            merged.append(tag)
    if not keep_system_tag and system_tag:
        merged = [tag for tag in merged if tag != system_tag]
    return merged
