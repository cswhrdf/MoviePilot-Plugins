#!/usr/bin/env python3
"""对上游 remoteidentifiers 源码幂等打免代理补丁.

补丁覆盖四组落点：配置读取与运行态、``__update_config`` 快照、``get_form`` 表单与默认模型、
唯一请求调用点，以及 fork 命名覆盖（``plugin_name`` 标记个人版）。锚点全部采用上游稳定结构；
任一必需锚点缺失或重复即失败退出，避免生成看似成功但实际失效的版本。匹配语义同时以可导入
纯函数导出，供单测直接调用。
"""

from __future__ import annotations

import argparse
import ipaddress
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class NoProxyRule:
    """一条有效免代理规则及其解析后的值."""

    kind: str
    value: Any


@dataclass(frozen=True)
class RuleWarning:
    """一条被跳过的非法规则及其行号与原因."""

    line_no: int
    rule: str
    reason: str


@dataclass(frozen=True)
class PatchResult:
    """一次补丁执行的结果与状态."""

    source: str
    status: str
    applied: tuple[str, ...]
    skipped: tuple[str, ...]
    warnings: tuple[str, ...] = ()


class PatchAnchorError(RuntimeError):
    """锚点缺失、重复或无法唯一定位时抛出的错误."""

    def __init__(self, anchor_name: str, reason: str, context: str) -> None:
        """保存锚点名、失败原因与上下文，并拼接异常消息."""
        self.anchor_name = anchor_name
        self.reason = reason
        self.context = context
        super().__init__(f"锚点[{anchor_name}] {reason}\n{context}")


def _classify_wildcard(rule: str) -> tuple[NoProxyRule | None, str]:
    """分类通配规则，拒绝包含端口或路径的写法."""
    if "/" in rule:
        return None, "通配规则不能包含路径"
    if ":" in rule:
        return None, "通配规则不能包含端口"
    return NoProxyRule("glob", rule.lower()), ""


def _classify_ip_rule(rule: str) -> tuple[NoProxyRule | None, str]:
    """分类精确 IP、CIDR 与普通域名，拒绝端口和非法网段."""
    if "/" in rule:
        _, _, prefix = rule.partition("/")
        if not prefix.isdigit():
            return None, "规则不能包含路径"
        try:
            return NoProxyRule("network", ipaddress.ip_network(rule, strict=False)), ""
        except ValueError:
            return None, "非法 CIDR"
    if ":" in rule:
        try:
            return NoProxyRule("ip", ipaddress.ip_address(rule)), ""
        except ValueError:
            return None, "规则不能包含端口"
    try:
        return NoProxyRule("ip", ipaddress.ip_address(rule)), ""
    except ValueError:
        return NoProxyRule("domain", rule.lower()), ""


def classify_rule(rule: str) -> tuple[NoProxyRule | None, str]:
    """把单条规则分类为域名、IP、CIDR 或通配；非法时返回原因."""
    if "[" in rule or "]" in rule:
        return None, "IPv6 规则不能使用方括号"
    if any(char.isspace() for char in rule):
        return None, "规则不能包含空白"
    if "://" in rule:
        return None, "规则不能包含协议"
    if "*" in rule or "?" in rule:
        return _classify_wildcard(rule)
    return _classify_ip_rule(rule)


def parse_no_proxy_rules(raw: str) -> tuple[list[NoProxyRule], list[RuleWarning]]:
    """解析多行配置，返回有效规则与非法行告警；空行不产生告警."""
    rules: list[NoProxyRule] = []
    warnings: list[RuleWarning] = []
    for line_no, line in enumerate((raw or "").splitlines(), start=1):
        rule = line.strip()
        if not rule:
            continue
        parsed, reason = classify_rule(rule)
        if parsed is None:
            warnings.append(RuleWarning(line_no=line_no, rule=rule, reason=reason))
            continue
        rules.append(parsed)
    return rules, warnings


def extract_host(url: str) -> str | None:
    """返回 URL 的小写主机名，不含端口、路径与 IPv6 方括号."""
    host = urlparse(url).hostname
    return host.lower() if host else None


def host_matches_rules(host: str, rules: Iterable[NoProxyRule]) -> bool:
    """判断主机名是否命中任一有效规则."""
    for rule in rules:
        if rule.kind == "domain" and host == rule.value:
            return True
        if rule.kind == "glob" and fnmatchcase(host, rule.value):
            return True
        if rule.kind not in {"ip", "network"}:
            continue
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            continue
        if rule.kind == "ip" and address == rule.value:
            return True
        if (
            isinstance(rule.value, (ipaddress.IPv4Network, ipaddress.IPv6Network))
            and address.version == rule.value.version
            and address in rule.value
        ):
            return True
    return False


def resolve_proxies(url: str, rules: Iterable[NoProxyRule], proxy: Any) -> tuple[Any, str | None]:
    """按规则选择 proxies 参数；无主机名时回退全局代理并返回告警原因."""
    host = extract_host(url)
    if host is None:
        return proxy, f"无法从地址中解析主机名：{url}"
    if host_matches_rules(host, rules):
        return None, None
    return proxy, None


@dataclass(frozen=True)
class PatchOperation:
    """一次以唯一锚点定位的文本补丁."""

    name: str
    anchor: str
    replacement: str
    target_markers: tuple[str, ...]

    def is_applied(self, source: str) -> bool:
        """目标标记全部存在时视为该组已处于目标状态."""
        return all(marker in source for marker in self.target_markers)


IMPORT_ANCHOR = "from app.log import logger\n"
IMPORT_REPLACEMENT = (
    "import ipaddress\n"
    "from fnmatch import fnmatchcase\n"
    "from urllib.parse import urlparse\n"
    "\n"
    "from app.log import logger\n"
)

CLASS_DEFAULT_ANCHOR = "    _flitter = True\n"
CLASS_DEFAULT_REPLACEMENT = (
    '    _flitter = True\n    _no_proxy_hosts = ""\n    _no_proxy_rules: list[tuple[str, Any]] = []\n'
)

# 非法行回显的运行态默认值：类级列表，init_plugin 复位，解析时填充。
CLASS_DEFAULT_ERRORS_ANCHOR = "    _no_proxy_rules: list[tuple[str, Any]] = []\n"
CLASS_DEFAULT_ERRORS_REPLACEMENT = (
    "    _no_proxy_rules: list[tuple[str, Any]] = []\n    _no_proxy_errors: list[str] = []\n"
)

INIT_RESET_ANCHOR = "        self.stop_service()\n"
INIT_RESET_REPLACEMENT = (
    '        self.stop_service()\n        self._no_proxy_hosts = ""\n        self._no_proxy_rules = []\n'
)

INIT_RESET_ERRORS_ANCHOR = "        self._no_proxy_rules = []\n"
INIT_RESET_ERRORS_REPLACEMENT = "        self._no_proxy_rules = []\n        self._no_proxy_errors = []\n"

INIT_READ_ANCHOR = "            self._file_urls = config.get(\"file_urls\") or ''\n"
INIT_READ_REPLACEMENT = (
    "            self._file_urls = config.get(\"file_urls\") or ''\n"
    '            self._no_proxy_hosts = config.get("no_proxy_hosts") or ""\n'
    "            self._no_proxy_rules = self._parse_no_proxy_hosts(self._no_proxy_hosts)\n"
)

UPDATE_CONFIG_ANCHOR = '            "file_urls": self._file_urls,\n        })\n'
UPDATE_CONFIG_REPLACEMENT = (
    '            "file_urls": self._file_urls,\n            "no_proxy_hosts": self._no_proxy_hosts,\n        })\n'
)

NO_PROXY_FORM_ROW = (
    "                    {\n"
    "                        'component': 'VRow',\n"
    "                        'content': [\n"
    "                            {\n"
    "                                'component': 'VCol',\n"
    "                                'props': {\n"
    "                                    'cols': 12,\n"
    "                                    'md': 12\n"
    "                                },\n"
    "                                'content': [\n"
    "                                    {\n"
    "                                        'component': 'VTextarea',\n"
    "                                        'props': {\n"
    "                                            'model': 'no_proxy_hosts',\n"
    "                                            'label': '免代理地址（一行一条）',\n"
    "                                            'placeholder': '127.0.0.1\\n::1\\n192.168.0.0/16\\n*.internal.lan',\n"
    "                                            'hint': '命中后直连，未命中仍使用全局代理；'\n"
    "                                                    '支持域名、IP、CIDR 和通配规则，不填写协议、端口或路径；'\n"
    "                                                    '存在非法行时会在上方提示并被忽略',\n"
    "                                            'rows': 4,\n"
    "                                            'auto-grow': True,\n"
    "                                            'persistent-hint': True,\n"
    "                                            'clearable': True\n"
    "                                        }\n"
    "                                    }\n"
    "                                ]\n"
    "                            }\n"
    "                        ]\n"
    "                    },\n"
)

FORM_ANCHOR = (
    "                    {\n"
    "                        'component': 'VRow',\n"
    "                        'content': [\n"
    "                            {\n"
    "                                'component': 'VCol',\n"
    "                                'props': {\n"
    "                                    'cols': 12,\n"
    "                                },\n"
    "                                'content': [\n"
    "                                    {\n"
    "                                        'component': 'VAlert',\n"
    "                                        'props': {\n"
    "                                            'type': 'success',\n"
)
FORM_REPLACEMENT = NO_PROXY_FORM_ROW + FORM_ANCHOR

DEFAULT_MODEL_ANCHOR = (
    '            "file_urls": "https://raw.githubusercontent.com/Putarku/MoviePilot-Help/main/Words/TV.txt\\n"\n'
    '                         "https://raw.githubusercontent.com/Putarku/MoviePilot-Help/main/Words/anime.txt",\n'
    "        }\n"
)
DEFAULT_MODEL_REPLACEMENT = (
    '            "file_urls": "https://raw.githubusercontent.com/Putarku/MoviePilot-Help/main/Words/TV.txt\\n"\n'
    '                         "https://raw.githubusercontent.com/Putarku/MoviePilot-Help/main/Words/anime.txt",\n'
    '            "no_proxy_hosts": "",\n'
    "        }\n"
)

HELPER_ANCHOR = "    def __get_response_text(self, url: str) -> str:\n"

RUNTIME_HELPERS = '''    @staticmethod
    def _classify_no_proxy_wildcard(rule: str) -> tuple[str, Any, str]:
        """分类通配规则，拒绝包含端口或路径的写法."""
        if "/" in rule:
            return "", None, "通配规则不能包含路径"
        if ":" in rule:
            return "", None, "通配规则不能包含端口"
        return "glob", rule.lower(), ""

    @staticmethod
    def _classify_no_proxy_ip(rule: str) -> tuple[str, Any, str]:
        """分类精确 IP、CIDR 与普通域名，拒绝端口和非法网段."""
        if "/" in rule:
            _, _, prefix = rule.partition("/")
            if not prefix.isdigit():
                return "", None, "规则不能包含路径"
            try:
                return "network", ipaddress.ip_network(rule, strict=False), ""
            except ValueError:
                return "", None, "非法 CIDR"
        if ":" in rule:
            try:
                return "ip", ipaddress.ip_address(rule), ""
            except ValueError:
                return "", None, "规则不能包含端口"
        try:
            return "ip", ipaddress.ip_address(rule), ""
        except ValueError:
            return "domain", rule.lower(), ""

    @classmethod
    def _classify_no_proxy_rule(cls, rule: str) -> tuple[str, Any, str]:
        """把单条免代理规则分类为域名、IP、CIDR 或通配；非法时返回原因."""
        if "[" in rule or "]" in rule:
            return "", None, "IPv6 规则不能使用方括号"
        if any(char.isspace() for char in rule):
            return "", None, "规则不能包含空白"
        if "://" in rule:
            return "", None, "规则不能包含协议"
        if "*" in rule or "?" in rule:
            return cls._classify_no_proxy_wildcard(rule)
        return cls._classify_no_proxy_ip(rule)

    def _parse_no_proxy_hosts(self, raw: str) -> list[tuple[str, Any]]:
        """解析多行免代理规则；空行忽略，非法行跳过并记录行号与原因."""
        rules: list[tuple[str, Any]] = []
        errors: list[str] = []
        for line_no, line in enumerate((raw or "").splitlines(), start=1):
            rule = line.strip()
            if not rule:
                continue
            kind, value, reason = self._classify_no_proxy_rule(rule)
            if reason:
                logger.warning(f"免代理地址第 {line_no} 行规则已跳过：{reason}（{rule}）")
                errors.append(f"第 {line_no} 行{reason}（{rule}）")
                continue
            rules.append((kind, value))
        self._no_proxy_errors = errors
        return rules

    def _host_matches_no_proxy(self, host: str) -> bool:
        """判断主机名是否命中任一有效免代理规则."""
        for kind, value in self._no_proxy_rules:
            if kind == "domain":
                if host == value:
                    return True
                continue
            if kind == "glob":
                if fnmatchcase(host, value):
                    return True
                continue
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                continue
            if kind == "ip" and address == value:
                return True
            if kind == "network" and address.version == value.version and address in value:
                return True
        return False

    def _resolve_proxies(self, url: str) -> Any:
        """命中免代理规则时返回 None 直连，否则回退全局代理."""
        host = urlparse(url).hostname
        if not host:
            logger.warning(f"无法从地址中解析主机名，已回退全局代理：{url}")
            return settings.PROXY
        if self._host_matches_no_proxy(host.lower()):
            return None
        return settings.PROXY

'''

HELPER_REPLACEMENT = RUNTIME_HELPERS + HELPER_ANCHOR

# 解析器升级：旧版补丁只写日志，升级为同时记录行号、原因与原文，有效行继续生效。
PARSE_METHOD_ANCHOR = (
    "    def _parse_no_proxy_hosts(self, raw: str) -> list[tuple[str, Any]]:\n"
    '        """解析多行免代理规则；空行忽略，非法行跳过并记录行号与原因."""\n'
    "        rules: list[tuple[str, Any]] = []\n"
    '        for line_no, line in enumerate((raw or "").splitlines(), start=1):\n'
    "            rule = line.strip()\n"
    "            if not rule:\n"
    "                continue\n"
    "            kind, value, reason = self._classify_no_proxy_rule(rule)\n"
    "            if reason:\n"
    '                logger.warning(f"免代理地址第 {line_no} 行规则已跳过：{reason}（{rule}）")\n'
    "                continue\n"
    "            rules.append((kind, value))\n"
    "        return rules\n"
)
PARSE_METHOD_HEADER = "    def _parse_no_proxy_hosts(self, raw: str) -> list[tuple[str, Any]]:\n"
PARSE_METHOD_TAIL = "        return rules\n"
_parse_start = RUNTIME_HELPERS.find(PARSE_METHOD_HEADER)
_parse_stop = RUNTIME_HELPERS.find(PARSE_METHOD_TAIL, _parse_start) + len(PARSE_METHOD_TAIL)
PARSE_METHOD_REPLACEMENT = RUNTIME_HELPERS[_parse_start:_parse_stop]

# 表单告警：仅在存在非法行时于免代理文本框上方插入一条 VAlert。
FORM_WARNING_HELPERS_ANCHOR = (
    "        if self._host_matches_no_proxy(host.lower()):\n            return None\n        return settings.PROXY\n\n"
)
FORM_WARNING_HELPERS = '''    def _row_uses_model(self, node: Any, model: str) -> bool:
        \"\"\"判断表单节点及其子节点是否绑定到指定模型.\"\"\"
        if isinstance(node, dict):
            props = node.get(\"props\")
            if isinstance(props, dict) and props.get(\"model\") == model:
                return True
            return any(self._row_uses_model(child, model) for child in node.get(\"content\") or ())
        if isinstance(node, list):
            return any(self._row_uses_model(child, model) for child in node)
        return False

    def _no_proxy_warning_row(self) -> dict:
        """构造免代理非法行的告警行，逐行拼接错误文本."""
        return {
            'component': 'VRow',
            'content': [
                {
                    'component': 'VCol',
                    'props': {
                        'cols': 12,
                        'md': 12
                    },
                    'content': [
                        {
                            'component': 'VAlert',
                            'props': {
                                'type': 'warning',
                                'variant': 'tonal',
                                'title': '免代理地址存在被忽略的行',
                                'text': "\\n".join(self._no_proxy_errors)
                            }
                        }
                    ]
                }
            ]
        }

    def _insert_no_proxy_warning(self, form: List[dict]) -> None:
        """存在被忽略的免代理行时，在免代理文本框上方插入告警行."""
        errors = list(self._no_proxy_errors or [])
        if not errors or not form or not isinstance(form[0], dict):
            return
        content = form[0].get("content")
        if not isinstance(content, list):
            return
        index = len(content)
        for position, row in enumerate(content):
            if self._row_uses_model(row, "no_proxy_hosts"):
                index = position
                break
        content.insert(index, self._no_proxy_warning_row())
'''
FORM_WARNING_HELPERS_REPLACEMENT = FORM_WARNING_HELPERS_ANCHOR + FORM_WARNING_HELPERS + "\n"

FORM_START_ANCHOR = "    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:\n        return [\n"
FORM_START_REPLACEMENT = "    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:\n        form: List[dict] = [\n"

FORM_DEFAULTS_ANCHOR = '        ], {\n            "enable": False,\n'
FORM_DEFAULTS_REPLACEMENT = '        ]\n        defaults: Dict[str, Any] = {\n            "enable": False,\n'

FORM_RETURN_ANCHOR = "    def __update_config(self):\n"
FORM_RETURN_REPLACEMENT = (
    "        self._insert_no_proxy_warning(form)\n        return form, defaults\n\n    def __update_config(self):\n"
)

# 旧版补丁的 hint 文案升级：新文案已存在时本组自动跳过。
FORM_HINT_ANCHOR = (
    "                                            'hint': '命中后直连，未命中仍使用全局代理；'\n"
    "                                                    '支持域名、IP、CIDR 和通配规则，不填写协议、端口或路径',\n"
)
FORM_HINT_REPLACEMENT = (
    "                                            'hint': '命中后直连，未命中仍使用全局代理；'\n"
    "                                                    '支持域名、IP、CIDR 和通配规则，不填写协议、端口或路径；'\n"
    "                                                    '存在非法行时会在上方提示并被忽略',\n"
)

REQUEST_ANCHOR = "            proxies=settings.PROXY,\n"
REQUEST_REPLACEMENT = "            proxies=self._resolve_proxies(url),\n"

PLUGIN_NAME_ANCHOR = '    plugin_name = "共享识别词"\n'
PLUGIN_NAME_REPLACEMENT = '    plugin_name = "共享识别词(个人版)"\n'

PATCH_OPERATIONS: tuple[PatchOperation, ...] = (
    PatchOperation(
        name="imports",
        anchor=IMPORT_ANCHOR,
        replacement=IMPORT_REPLACEMENT,
        target_markers=("import ipaddress", "from fnmatch import fnmatchcase", "from urllib.parse import urlparse"),
    ),
    PatchOperation(
        name="runtime-defaults",
        anchor=CLASS_DEFAULT_ANCHOR,
        replacement=CLASS_DEFAULT_REPLACEMENT,
        target_markers=('_no_proxy_hosts = ""', "_no_proxy_rules"),
    ),
    PatchOperation(
        name="runtime-errors",
        anchor=CLASS_DEFAULT_ERRORS_ANCHOR,
        replacement=CLASS_DEFAULT_ERRORS_REPLACEMENT,
        target_markers=("_no_proxy_errors: list[str] = []",),
    ),
    PatchOperation(
        name="plugin-name",
        anchor=PLUGIN_NAME_ANCHOR,
        replacement=PLUGIN_NAME_REPLACEMENT,
        target_markers=('plugin_name = "共享识别词(个人版)"',),
    ),
    PatchOperation(
        name="init-reset",
        anchor=INIT_RESET_ANCHOR,
        replacement=INIT_RESET_REPLACEMENT,
        target_markers=('self._no_proxy_hosts = ""',),
    ),
    PatchOperation(
        name="init-reset-errors",
        anchor=INIT_RESET_ERRORS_ANCHOR,
        replacement=INIT_RESET_ERRORS_REPLACEMENT,
        target_markers=("self._no_proxy_errors = []",),
    ),
    PatchOperation(
        name="init-read",
        anchor=INIT_READ_ANCHOR,
        replacement=INIT_READ_REPLACEMENT,
        target_markers=('config.get("no_proxy_hosts")',),
    ),
    PatchOperation(
        name="update-config",
        anchor=UPDATE_CONFIG_ANCHOR,
        replacement=UPDATE_CONFIG_REPLACEMENT,
        target_markers=('"no_proxy_hosts": self._no_proxy_hosts,',),
    ),
    PatchOperation(
        name="helpers",
        anchor=HELPER_ANCHOR,
        replacement=HELPER_REPLACEMENT,
        target_markers=("def _resolve_proxies(self, url: str)",),
    ),
    PatchOperation(
        name="parser-errors",
        anchor=PARSE_METHOD_ANCHOR,
        replacement=PARSE_METHOD_REPLACEMENT,
        target_markers=("self._no_proxy_errors = errors",),
    ),
    PatchOperation(
        name="form-warning-helpers",
        anchor=FORM_WARNING_HELPERS_ANCHOR,
        replacement=FORM_WARNING_HELPERS_REPLACEMENT,
        target_markers=("def _insert_no_proxy_warning(self, form: List[dict]) -> None:",),
    ),
    PatchOperation(
        name="form-control",
        anchor=FORM_ANCHOR,
        replacement=FORM_REPLACEMENT,
        target_markers=("'model': 'no_proxy_hosts'",),
    ),
    PatchOperation(
        name="form-hint",
        anchor=FORM_HINT_ANCHOR,
        replacement=FORM_HINT_REPLACEMENT,
        target_markers=("'存在非法行时会在上方提示并被忽略'",),
    ),
    PatchOperation(
        name="form-bind",
        anchor=FORM_START_ANCHOR,
        replacement=FORM_START_REPLACEMENT,
        target_markers=("        form: List[dict] = [",),
    ),
    PatchOperation(
        name="form-defaults",
        anchor=FORM_DEFAULTS_ANCHOR,
        replacement=FORM_DEFAULTS_REPLACEMENT,
        target_markers=("        defaults: Dict[str, Any] = {",),
    ),
    PatchOperation(
        name="default-model",
        anchor=DEFAULT_MODEL_ANCHOR,
        replacement=DEFAULT_MODEL_REPLACEMENT,
        target_markers=('"no_proxy_hosts": "",',),
    ),
    PatchOperation(
        name="form-return",
        anchor=FORM_RETURN_ANCHOR,
        replacement=FORM_RETURN_REPLACEMENT,
        target_markers=("self._insert_no_proxy_warning(form)",),
    ),
    PatchOperation(
        name="request-call",
        anchor=REQUEST_ANCHOR,
        replacement=REQUEST_REPLACEMENT,
        target_markers=("proxies=self._resolve_proxies(url),",),
    ),
)

_BYPASS_MARKERS = ("no_proxy", "noproxy", "proxies=none", "skip_proxy", "bypass_proxy")


def detect_upstream_bypass(source: str) -> bool:
    """上游已出现等效代理绕过关键字时返回 True，用于转入人工评估."""
    lowered = source.lower()
    return any(marker in lowered for marker in _BYPASS_MARKERS)


def anchor_context(source: str, anchor: str, radius: int = 3) -> str:
    """返回锚点附近的带行号上下文；锚点不存在时给出明确提示."""
    index = source.find(anchor)
    if index == -1:
        return "锚点上下文：未找到原文"
    line_no = source.count("\n", 0, index)
    lines = source.splitlines()
    start = max(line_no - radius, 0)
    end = min(line_no + radius + 1, len(lines))
    return "锚点上下文：\n" + "\n".join(f"{number + 1:4d}: {lines[number]}" for number in range(start, end))


def apply_no_proxy_patch(source: str) -> PatchResult:
    """应用全部补丁；锚点缺失或重复时抛出 PatchAnchorError."""
    if not any(operation.is_applied(source) for operation in PATCH_OPERATIONS) and detect_upstream_bypass(source):
        return PatchResult(
            source=source,
            status="upstream-bypass",
            applied=(),
            skipped=(),
            warnings=(),
        )

    applied: list[str] = []
    skipped: list[str] = []
    result = source
    for operation in PATCH_OPERATIONS:
        if operation.is_applied(result):
            skipped.append(operation.name)
            continue
        occurrences = result.count(operation.anchor)
        if occurrences != 1:
            reason = "锚点缺失" if occurrences == 0 else f"锚点出现 {occurrences} 次，无法唯一定位"
            raise PatchAnchorError(operation.name, reason, anchor_context(result, operation.anchor))
        result = result.replace(operation.anchor, operation.replacement, 1)
        applied.append(operation.name)
    status = "patched" if applied else "unchanged"
    return PatchResult(source=result, status=status, applied=tuple(applied), skipped=tuple(skipped))


def main(argv: list[str] | None = None) -> int:
    """命令行入口：写出补丁、检查目标状态，或在锚点漂移时失败退出."""
    parser = argparse.ArgumentParser(description="对 remoteidentifiers 源码幂等打免代理补丁")
    parser.add_argument("path", nargs="?", default="plugins/remoteidentifiers/__init__.py")
    parser.add_argument("--dry-run", action="store_true", help="只计算补丁，不写回文件")
    parser.add_argument("--check", action="store_true", help="仅检查是否已处于目标状态")
    args = parser.parse_args(argv)

    path = Path(args.path)
    if not path.exists():
        print(f"插件源码不存在：{path}", file=sys.stderr)
        return 1
    source = path.read_bytes().decode("utf-8")
    try:
        result = apply_no_proxy_patch(source)
    except PatchAnchorError as error:
        print(f"补丁失败：{error}", file=sys.stderr)
        return 1

    if result.status == "upstream-bypass":
        print("[评估关闭本补丁] 上游已提供等效代理绕过实现，未叠加本地补丁。")
        return 0
    if args.check:
        if result.status == "unchanged":
            print(f"补丁已处于目标状态：{path}")
            return 0
        print(f"补丁缺失，需要应用：{', '.join(result.applied)}", file=sys.stderr)
        return 1
    if result.status == "patched" and not args.dry_run:
        path.write_bytes(result.source.encode("utf-8"))
    print(f"补丁状态：{result.status}；已应用：{', '.join(result.applied) or '无'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
