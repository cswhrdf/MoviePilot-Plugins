"""Contract tests for the remote identifiers no-proxy patch and sync helpers.

The module stays backend free: it loads the repository scripts by path, applies the
textual patch to a representative upstream source fixture, and runs the injected
runtime helpers inside a lightweight class with fake host symbols. Pure matching,
config preservation, request dispatch, patch idempotency and anchor drift are covered
without importing any ``app.*`` module.
"""

from __future__ import annotations

import ast
import importlib.util
import ipaddress
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"

UPSTREAM_SOURCE = """import json
from typing import List, Tuple, Dict, Any

from app.log import logger


class RemoteIdentifiers(_PluginBase):
    plugin_name = "共享识别词"
    plugin_version = "2.4.1"

    _enable = False
    _cron = '30 4 * * *'
    _file_urls = ''
    _onlyonce = False
    _flitter = True
    _scheduler = None

    def init_plugin(self, config: dict = None):
        self.stop_service()
        if config:
            self._file_urls = config.get("file_urls") or ''

    def __get_response_text(self, url: str) -> str:
        response = RequestUtils(
            proxies=settings.PROXY,
            headers=None,
            timeout=15
        ).get_res(url)
        if not response:
            raise Exception(f"文件 {url} 下载失败！")
        return response.content.decode('utf-8')

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                'component': 'VForm',
                'content': [
                    {
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
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'file_urls',
                                            'rows': 6,
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'success',
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enable": False,
            "onlyonce": False,
            "flitter": True,
            "cron": '30 4 * * *',
            "file_urls": "https://raw.githubusercontent.com/Putarku/MoviePilot-Help/main/Words/TV.txt\\n"
                         "https://raw.githubusercontent.com/Putarku/MoviePilot-Help/main/Words/anime.txt",
        }

    def __update_config(self):
        self.update_config({
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "enable": self._enable,
            "flitter": self._flitter,
            "file_urls": self._file_urls,
        })

    def stop_service(self):
        pass
"""

UPSTREAM_ENTRY: dict[str, Any] = {
    "name": "共享识别词",
    "description": "从Github、Etherpad等远程文件中获取共享识别词并应用。",
    "labels": "识别",
    "version": "2.4.1",
    "icon": "words.png",
    "author": "honue",
    "level": 1,
    "v2": True,
    "history": {
        "v2.4.1": "移除官方共享支持",
        "v2.4": "支持 JSON 格式远程识别词集合订阅",
        "v2.3": "更换默认共享识别词地址",
    },
}

EXPECTED_GROUPS = {
    "imports",
    "runtime-defaults",
    "runtime-errors",
    "plugin-name",
    "init-reset",
    "init-reset-errors",
    "init-read",
    "update-config",
    "helpers",
    "parser-errors",
    "form-warning-helpers",
    "form-control",
    "form-bind",
    "form-defaults",
    "form-hint",
    "default-model",
    "form-return",
    "request-call",
}

FORM_METHODS = ("_row_uses_model", "_no_proxy_warning_row", "_insert_no_proxy_warning", "get_form")


def _load_script(name: str) -> Any:
    """Load a repository script module from its file path."""
    module_name = f"_mpp_test_{name}"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class _FakeLogger:
    """Collect warning lines emitted by the injected helpers."""

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, message: str) -> None:
        """Record one warning line."""
        self.warnings.append(message)


class _FakeSettings:
    """Stand-in for the host settings module."""

    PROXY = {"http": "http://proxy.local:7890", "https": "http://proxy.local:7890"}


class _FakeResponse:
    """Minimal successful HTTP response."""

    status_code = 200
    reason = "OK"
    content = b"ok"


class _FakeRequestUtils:
    """Record the proxies argument passed to the plugin request helper."""

    calls: list[Any] = []

    def __init__(self, proxies: Any = None, headers: Any = None, timeout: int = 15) -> None:
        del headers, timeout
        self.proxies = proxies
        self.calls.append(proxies)

    def get_res(self, url: str) -> _FakeResponse:
        """Return a successful fake response for any URL."""
        del url
        return _FakeResponse()


@pytest.fixture(scope="module")
def patch_module() -> Any:
    """Return the patch script module."""
    return _load_script("apply_no_proxy_patch")


@pytest.fixture(scope="module")
def sync_module() -> Any:
    """Return the sync script module."""
    return _load_script("sync_upstream_plugin")


@pytest.fixture(scope="module")
def upstream_source() -> str:
    """Return a representative upstream source with the four required anchor regions."""
    return UPSTREAM_SOURCE


@pytest.fixture(scope="module")
def patched_source(patch_module, upstream_source) -> str:
    """Return the representative upstream source with the full patch applied."""
    result = patch_module.apply_no_proxy_patch(upstream_source)
    assert result.status == "patched"
    return result.source


def _runtime_helpers(patch_module: Any) -> str:
    """Return the injected helpers, which already record ignored rule lines."""
    return patch_module.RUNTIME_HELPERS


def _runtime_namespace(patch_module) -> tuple[Any, type, _FakeLogger]:
    """Build a lightweight runtime class from the injected helper snippet."""
    logger = _FakeLogger()
    namespace = {
        "__name__": "no_proxy_runtime",
        "Any": Any,
        "List": list,
        "Tuple": tuple,
        "ipaddress": ipaddress,
        "fnmatchcase": patch_module.fnmatchcase,
        "urlparse": patch_module.urlparse,
        "logger": logger,
        "settings": _FakeSettings,
    }
    class_source = (
        "class _Runtime:\n"
        '    _no_proxy_hosts = ""\n'
        "    _no_proxy_rules: List[Tuple[str, Any]] = []\n"
        "    _no_proxy_errors: List[str] = []\n" + _runtime_helpers(patch_module)
    )
    exec(compile(class_source, "<runtime>", "exec"), namespace)
    return namespace, namespace["_Runtime"], logger


def _extract_method(source: str, name: str) -> str:
    """Return one method's source segment from a class body."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            return segment
    raise AssertionError(f"method {name} not found")


def _indent_method(segment: str) -> str:
    """Indent an unparsed method segment to class body level."""
    return "\n".join(f"    {line}" if line.strip() else line for line in segment.splitlines())


def _probe_with_methods(method_sources: list[str], patch_module, extra_namespace: dict[str, Any] | None = None) -> Any:
    """Execute a probe class containing injected helpers and selected methods."""
    namespace = {
        "__name__": "no_proxy_probe",
        "Any": Any,
        "List": list,
        "Tuple": tuple,
        "ipaddress": ipaddress,
        "fnmatchcase": patch_module.fnmatchcase,
        "urlparse": patch_module.urlparse,
        "logger": _FakeLogger(),
        "settings": _FakeSettings,
        "RequestUtils": _FakeRequestUtils,
    }
    namespace.update(extra_namespace or {})
    class_source = (
        "class _Probe:\n"
        '    _no_proxy_hosts = ""\n'
        "    _no_proxy_rules: List[Tuple[str, Any]] = []\n"
        "    _no_proxy_errors: List[str] = []\n"
        "    def update_config(self, data: dict) -> None:\n"
        "        self.saved = data\n" + _runtime_helpers(patch_module) + "\n"
    )
    class_source += "\n".join(_indent_method(method_source) for method_source in method_sources)
    exec(compile(class_source, "<probe>", "exec"), namespace)
    return namespace["_Probe"]()


def _component_path(node: Any, model: str, path: tuple[dict[str, Any], ...] = ()) -> tuple[dict[str, Any], ...]:
    """Return the component path leading to the node whose props.model matches."""
    if isinstance(node, dict):
        current = path + (node,)
        props = node.get("props")
        if isinstance(props, dict) and props.get("model") == model:
            return current
        for child in node.get("content") or ():
            found = _component_path(child, model, current)
            if found:
                return found
    elif isinstance(node, list):
        for child in node:
            found = _component_path(child, model, path)
            if found:
                return found
    return ()


def _warning_alerts(node: Any) -> list[dict[str, Any]]:
    """Return every warning VAlert component nested below the given form node."""
    if isinstance(node, dict):
        props = node.get("props")
        found = (
            [node]
            if node.get("component") == "VAlert" and isinstance(props, dict) and props.get("type") == "warning"
            else []
        )
        for child in node.get("content") or ():
            found.extend(_warning_alerts(child))
        return found
    if isinstance(node, list):
        found = []
        for child in node:
            found.extend(_warning_alerts(child))
        return found
    return []


def _form_probe(patch_module, patched_source: str, errors: list[str]) -> Any:
    """Build a probe exposing get_form plus the injected warning helpers."""
    methods = [_extract_method(patched_source, name) for name in FORM_METHODS]
    probe = _probe_with_methods(methods, patch_module)
    probe._no_proxy_errors = list(errors)
    return probe


# ---------------- matching semantics ----------------


def test_parse_rules_classifies_supported_forms(patch_module):
    """Domain, IP, CIDR and glob rules are normalized and kept in input order."""
    raw = "\n".join(["  Example.COM  ", "127.0.0.1", "::1", "192.168.0.0/16", "*.internal.lan", ""])

    rules, warnings = patch_module.parse_no_proxy_rules(raw)

    assert warnings == []
    assert [(rule.kind, str(rule.value)) for rule in rules] == [
        ("domain", "example.com"),
        ("ip", "127.0.0.1"),
        ("ip", "::1"),
        ("network", "192.168.0.0/16"),
        ("glob", "*.internal.lan"),
    ]


def test_invalid_rules_are_skipped_with_line_numbers(patch_module):
    """Invalid lines are reported with their line number while valid lines survive."""
    raw = "\n".join(
        [
            "example.com",
            "[::1]",
            "http://example.com",
            "example.com:8080",
            "example.com/path",
            "192.168.1.1/33",
            "bad host",
            "",
        ]
    )

    rules, warnings = patch_module.parse_no_proxy_rules(raw)

    assert [rule.value for rule in rules] == ["example.com"]
    assert [warning.line_no for warning in warnings] == [2, 3, 4, 5, 6, 7]
    assert all(warning.reason for warning in warnings)


def test_exact_domain_does_not_match_subdomain(patch_module):
    """Exact domain rules stay case insensitive but never cover child domains."""
    rules, _ = patch_module.parse_no_proxy_rules("Example.COM")

    assert patch_module.host_matches_rules("example.com", rules) is True
    assert patch_module.host_matches_rules("a.example.com", rules) is False


def test_glob_rules_can_cross_dots(patch_module):
    """Wildcards match nested names but not the bare domain."""
    rules, _ = patch_module.parse_no_proxy_rules("*.internal.lan")

    assert patch_module.host_matches_rules("a.internal.lan", rules) is True
    assert patch_module.host_matches_rules("a.b.internal.lan", rules) is True
    assert patch_module.host_matches_rules("internal.lan", rules) is False


def test_ip_and_cidr_matching_boundaries(patch_module):
    """IPs compare by value and CIDR rules include network and broadcast addresses."""
    rules, _ = patch_module.parse_no_proxy_rules("127.0.0.1\n::1\n192.168.0.0/16\n192.168.1.1/16")

    assert patch_module.host_matches_rules("127.0.0.1", rules) is True
    assert patch_module.host_matches_rules("::1", rules) is True
    assert patch_module.host_matches_rules("0:0:0:0:0:0:0:1", rules) is True
    assert patch_module.host_matches_rules("192.168.0.0", rules) is True
    assert patch_module.host_matches_rules("192.168.255.255", rules) is True
    assert patch_module.host_matches_rules("192.168.3.4", rules) is True
    assert patch_module.host_matches_rules("192.169.0.1", rules) is False
    assert patch_module.host_matches_rules("2001:db8::1", rules) is False
    assert patch_module.host_matches_rules("example.com", rules) is False


def test_url_host_extraction_drops_port_path_and_brackets(patch_module):
    """URL hostnames are lowercased and IPv6 brackets are removed."""
    assert patch_module.extract_host("http://Example.COM:8080/a") == "example.com"
    assert patch_module.extract_host("http://example.com:9090/b") == "example.com"
    assert patch_module.extract_host("http://[::1]:8080/a") == "::1"
    assert patch_module.extract_host("not a url") is None


def test_proxy_selection_falls_back_without_host(patch_module):
    """Matched hosts bypass the proxy; missing hosts warn and use the global proxy."""
    proxy = {"https": "http://proxy.local:7890"}
    rules, _ = patch_module.parse_no_proxy_rules("example.com")

    assert patch_module.resolve_proxies("https://example.com/a", rules, proxy) == (None, None)
    assert patch_module.resolve_proxies("https://other.example/a", rules, proxy) == (proxy, None)
    selected, warning = patch_module.resolve_proxies("not a url", rules, proxy)
    assert selected is proxy
    assert warning is not None


def test_injected_runtime_helpers_match_pure_semantics(patch_module):
    """The code injected into the plugin mirrors parsing and resolution behavior."""
    _, runtime_class, logger = _runtime_namespace(patch_module)
    runtime = runtime_class()
    runtime._no_proxy_rules = runtime._parse_no_proxy_hosts("Example.COM\n[::1]\n192.168.0.0/16\n*.internal.lan")

    assert len(logger.warnings) == 1
    assert "第 2 行" in logger.warnings[0]
    assert runtime._resolve_proxies("http://example.com/a") is None
    assert runtime._resolve_proxies("https://public.example/a") == _FakeSettings.PROXY
    assert runtime._resolve_proxies("not a url") == _FakeSettings.PROXY
    assert "无法从地址中解析主机名" in logger.warnings[-1]


def test_injected_parser_records_every_ignored_line(patch_module):
    """All invalid lines land in _no_proxy_errors with blank lines shifting the numbering."""
    _, runtime_class, logger = _runtime_namespace(patch_module)
    runtime = runtime_class()

    runtime._no_proxy_rules = runtime._parse_no_proxy_hosts(
        "example.com\n\nexample.com:8080\n[::1]\nhttp://nas.lan\n192.168.0.0/16\n"
    )

    assert runtime._no_proxy_errors == [
        "第 3 行规则不能包含端口（example.com:8080）",
        "第 4 行IPv6 规则不能使用方括号（[::1]）",
        "第 5 行规则不能包含协议（http://nas.lan）",
    ]
    assert len(logger.warnings) == 3
    assert [kind for kind, _ in runtime._no_proxy_rules] == ["domain", "network"]
    assert runtime._resolve_proxies("https://example.com/a") is None
    assert runtime._resolve_proxies("https://192.168.9.9/a") is None
    assert runtime._resolve_proxies("https://nas.lan/a") == _FakeSettings.PROXY


def test_ignored_lines_do_not_block_valid_rules(patch_module):
    """Invalid lines are still skipped while valid lines keep taking effect."""
    _, runtime_class, logger = _runtime_namespace(patch_module)
    runtime = runtime_class()

    rules = runtime._parse_no_proxy_hosts("nas.lan:5000\nnas.lan\n")
    runtime._no_proxy_rules = rules

    assert [(kind, str(value)) for kind, value in rules] == [("domain", "nas.lan")]
    assert runtime._no_proxy_errors == ["第 1 行规则不能包含端口（nas.lan:5000）"]
    assert "第 1 行" in logger.warnings[0]
    assert runtime._resolve_proxies("http://nas.lan/words.txt") is None


def test_get_form_shows_warning_above_no_proxy_textarea(patch_module, patched_source):
    """A non-empty error list inserts one warning alert above the no-proxy textarea."""
    errors = ["第 3 行规则不能包含端口（example.com:8080）", "第 4 行规则不能包含协议（http://nas.lan）"]

    form, defaults = _form_probe(patch_module, patched_source, errors).get_form()

    rows = form[0]["content"]
    textarea_index = next(index for index, row in enumerate(rows) if _component_path(row, "no_proxy_hosts"))
    previous_row_alerts = _warning_alerts(rows[textarea_index - 1])

    assert len(previous_row_alerts) == 1
    props = previous_row_alerts[0]["props"]
    assert props["variant"] == "tonal"
    assert props["title"] == "免代理地址存在被忽略的行"
    assert props["text"] == "\n".join(errors)
    assert _warning_alerts(rows) == previous_row_alerts
    assert "no_proxy_errors" not in defaults
    assert all("第 3 行" not in str(value) for value in defaults.values())


def test_get_form_hides_warning_without_ignored_lines(patch_module, patched_source):
    """Without ignored lines the form keeps its original components only."""
    form, _ = _form_probe(patch_module, patched_source, []).get_form()

    assert _warning_alerts(form[0]["content"]) == []


def test_reapplying_on_legacy_patch_adds_error_echo(patch_module, patched_source):
    """A source carrying only the older patch gains the echo groups and nothing else."""
    legacy = patched_source
    for anchor, replacement in (
        (patch_module.CLASS_DEFAULT_ERRORS_REPLACEMENT, patch_module.CLASS_DEFAULT_ERRORS_ANCHOR),
        (patch_module.INIT_RESET_ERRORS_REPLACEMENT, patch_module.INIT_RESET_ERRORS_ANCHOR),
        (patch_module.PARSE_METHOD_REPLACEMENT, patch_module.PARSE_METHOD_ANCHOR),
        (patch_module.FORM_WARNING_HELPERS_REPLACEMENT, patch_module.FORM_WARNING_HELPERS_ANCHOR),
        (patch_module.FORM_START_REPLACEMENT, patch_module.FORM_START_ANCHOR),
        (patch_module.FORM_DEFAULTS_REPLACEMENT, patch_module.FORM_DEFAULTS_ANCHOR),
        (patch_module.FORM_RETURN_REPLACEMENT, patch_module.FORM_RETURN_ANCHOR),
        (patch_module.FORM_HINT_REPLACEMENT, patch_module.FORM_HINT_ANCHOR),
    ):
        assert anchor in legacy
        legacy = legacy.replace(anchor, replacement, 1)

    assert "_no_proxy_errors" not in legacy
    result = patch_module.apply_no_proxy_patch(legacy)

    assert result.status == "patched"
    assert set(result.applied) == {
        "runtime-errors",
        "init-reset-errors",
        "parser-errors",
        "form-warning-helpers",
        "form-hint",
        "form-bind",
        "form-defaults",
        "form-return",
    }
    assert result.source == patched_source


# ---------------- patch content and request dispatch ----------------


def test_patch_applies_all_groups(patch_module, upstream_source, patched_source):
    """All groups are applied in one pass; legacy parser/hint upgrade groups are skipped."""
    result = patch_module.apply_no_proxy_patch(upstream_source)

    assert set(result.applied) == EXPECTED_GROUPS - {"parser-errors", "form-hint"}
    assert result.skipped == ("parser-errors", "form-hint")
    assert 'plugin_name = "共享识别词(个人版)"' in patched_source
    assert "proxies=self._resolve_proxies(url)," in patched_source
    assert "proxies=settings.PROXY," not in patched_source
    assert "def _resolve_proxies(self, url: str)" in patched_source
    assert "'model': 'no_proxy_hosts'" in patched_source


def test_patch_renames_plugin_name(patch_module, upstream_source, patched_source):
    """The fork displays the personal-edition name while upstream keeps the original."""
    assert 'plugin_name = "共享识别词"' in upstream_source
    assert 'plugin_name = "共享识别词(个人版)"' not in upstream_source
    assert 'plugin_name = "共享识别词"' not in patched_source
    assert 'plugin_name = "共享识别词(个人版)"' in patched_source


def test_patch_is_idempotent(patch_module, patched_source):
    """A second application changes nothing."""
    result = patch_module.apply_no_proxy_patch(patched_source)

    assert result.status == "unchanged"
    assert result.applied == ()
    assert result.source == patched_source


def test_partial_patch_only_fills_missing_group(patch_module, patched_source):
    """A partially applied source only receives the missing group."""
    broken = patched_source.replace('            "no_proxy_hosts": self._no_proxy_hosts,\n', "")

    result = patch_module.apply_no_proxy_patch(broken)

    assert result.status == "patched"
    assert result.applied == ("update-config",)
    assert result.source == patched_source


def test_missing_anchor_reports_name_and_context(patch_module, patched_source):
    """A drifted request call fails with the anchor name and nearby context."""
    drifted = patched_source.replace(
        "            proxies=self._resolve_proxies(url),\n",
        "            proxies=custom_proxy_for(url),\n",
    )

    with pytest.raises(patch_module.PatchAnchorError) as error_info:
        patch_module.apply_no_proxy_patch(drifted)

    assert error_info.value.anchor_name == "request-call"
    assert "锚点缺失" in str(error_info.value)
    assert "锚点上下文" in str(error_info.value)


def test_duplicate_anchor_reports_conflict(patch_module, patched_source):
    """Duplicated anchors cannot be located uniquely and must fail."""
    duplicated = patched_source.replace(
        "            proxies=self._resolve_proxies(url),\n",
        "            proxies=settings.PROXY,\n            proxies=settings.PROXY,\n",
    )

    with pytest.raises(patch_module.PatchAnchorError) as error_info:
        patch_module.apply_no_proxy_patch(duplicated)

    assert error_info.value.anchor_name == "request-call"
    assert "2 次" in str(error_info.value)


def test_upstream_bypass_is_not_overlaid(patch_module, upstream_source):
    """An upstream equivalent bypass is reported instead of stacking local code."""
    bypass_source = upstream_source.replace(
        "            proxies=settings.PROXY,\n",
        "            proxies=NO_PROXY.proxies_for(url) or settings.PROXY,\n",
    )

    result = patch_module.apply_no_proxy_patch(bypass_source)

    assert result.status == "upstream-bypass"
    assert result.applied == ()
    assert result.source == bypass_source


def test_request_call_dispatches_direct_and_proxy(patch_module, patched_source):
    """The patched request helper passes None on a hit and settings.PROXY otherwise."""
    method_source = _extract_method(patched_source, "__get_response_text")
    probe = _probe_with_methods([method_source], patch_module)
    probe._no_proxy_rules = probe._parse_no_proxy_hosts("example.com")
    _FakeRequestUtils.calls.clear()

    probe._Probe__get_response_text("https://example.com/a")
    probe._Probe__get_response_text("https://other.example/b")

    assert _FakeRequestUtils.calls == [None, _FakeSettings.PROXY]


def test_update_config_keeps_existing_fields(patch_module, patched_source):
    """The config snapshot keeps every upstream field and adds no_proxy_hosts."""
    method_source = _extract_method(patched_source, "__update_config")
    probe = _probe_with_methods([method_source], patch_module)
    probe._onlyonce = False
    probe._cron = "30 4 * * *"
    probe._enable = True
    probe._flitter = True
    probe._file_urls = "https://example.com/a.txt"
    probe._no_proxy_hosts = "example.com"

    probe._Probe__update_config()

    assert probe.saved == {
        "onlyonce": False,
        "cron": "30 4 * * *",
        "enable": True,
        "flitter": True,
        "file_urls": "https://example.com/a.txt",
        "no_proxy_hosts": "example.com",
    }


def test_form_adds_textarea_after_file_urls(patch_module, patched_source):
    """The new textarea follows file_urls and keeps the expected props and defaults."""
    probe = _form_probe(patch_module, patched_source, [])

    form, defaults = probe.get_form()
    file_urls_path = _component_path(form, "file_urls")
    no_proxy_path = _component_path(form, "no_proxy_hosts")

    assert file_urls_path and no_proxy_path
    assert file_urls_path[0] is no_proxy_path[0]
    textarea = no_proxy_path[-1]
    parent_col = no_proxy_path[-2]
    props = textarea["props"]
    assert parent_col["component"] == "VCol"
    assert parent_col["props"] == {"cols": 12, "md": 12}
    assert textarea["component"] == "VTextarea"
    assert props["label"] == "免代理地址（一行一条）"
    assert props["hint"].startswith("命中后直连，未命中仍使用全局代理")
    assert "存在非法行时会在上方提示并被忽略" in props["hint"]
    assert props["rows"] == 4
    assert props["auto-grow"] is True
    assert props["persistent-hint"] is True
    assert props["clearable"] is True
    assert "127.0.0.1" in props["placeholder"]
    assert defaults["no_proxy_hosts"] == ""
    assert defaults["enable"] is False
    assert defaults["onlyonce"] is False
    assert defaults["flitter"] is True
    assert defaults["cron"] == "30 4 * * *"
    assert defaults["file_urls"].startswith("https://raw.githubusercontent.com/Putarku/MoviePilot-Help/")


def test_init_resets_and_reads_config(patched_source):
    """Runtime state is reset before the saved value is parsed."""
    reset_marker = '        self._no_proxy_hosts = ""\n        self._no_proxy_rules = []\n'
    read_marker = 'self._no_proxy_hosts = config.get("no_proxy_hosts") or ""'

    assert reset_marker in patched_source
    assert read_marker in patched_source
    assert "        self._no_proxy_errors = []\n" in patched_source
    assert "self._parse_no_proxy_hosts(self._no_proxy_hosts)" in patched_source
    assert patched_source.index(reset_marker) < patched_source.index(read_marker)


# ---------------- sync helpers ----------------


def test_semantic_version_accepts_optional_v_prefix(sync_module):
    """Upstream history keys may carry a v prefix."""
    assert sync_module.semantic_version("v2.4.1") == (2, 4, 1)
    assert sync_module.semantic_version("2.4") == (2, 4)
    assert sync_module.semantic_version("bad") is None


def test_upstream_contract_accepts_real_entry_shape(sync_module):
    """The upstream 2.4.1 entry and source satisfy the version contract."""
    assert sync_module.validate_upstream_contract(UPSTREAM_SOURCE, UPSTREAM_ENTRY) == []


def test_upstream_contract_rejects_bad_history_order(sync_module):
    """History must be strictly descending with the first key matching the version."""
    entry = dict(UPSTREAM_ENTRY)
    entry["history"] = {"v2.3": "old", "v2.4.1": "new"}

    errors = sync_module.validate_upstream_contract(UPSTREAM_SOURCE, entry)

    assert any("降序" in error for error in errors)


def test_upstream_contract_rejects_version_mismatch(sync_module):
    """Source and index versions must agree."""
    entry = dict(UPSTREAM_ENTRY)
    entry["version"] = "2.4.2"

    errors = sync_module.validate_upstream_contract(UPSTREAM_SOURCE, entry)

    assert any("plugin_version" in error for error in errors)


def test_merge_index_entry_applies_fork_overrides_and_keeps_local_fields(sync_module):
    """The fork name and v2/v3 markers win while local extra fields survive."""
    merged = sync_module.merge_index_entry(UPSTREAM_ENTRY, {"release": True, "v3": False})

    assert merged["name"] == "共享识别词(个人版)"
    assert merged["v2"] is True
    assert merged["v3"] is True
    assert merged["release"] is True
    assert list(merged)[: len(UPSTREAM_ENTRY)] == list(UPSTREAM_ENTRY)


def test_replace_index_entry_inserts_into_empty_package(sync_module):
    """An empty package gains the entry without losing JSON validity."""
    merged = sync_module.merge_index_entry(UPSTREAM_ENTRY, None)

    text = sync_module.replace_index_entry_text("{}\n", "RemoteIdentifiers", merged)

    assert text.startswith('{\n  "RemoteIdentifiers": {\n')
    assert json.loads(text)["RemoteIdentifiers"] == merged


def test_replace_index_entry_preserves_other_entries(sync_module):
    """Insertion keeps existing entries and their formatting untouched."""
    merged = sync_module.merge_index_entry(UPSTREAM_ENTRY, None)
    original = '{\n  "Other": {\n    "version": "1.0.0"\n  }\n}\n'

    text = sync_module.replace_index_entry_text(original, "RemoteIdentifiers", merged)

    assert '  "Other": {\n    "version": "1.0.0"\n  }' in text
    assert text.index('"Other"') < text.index('"RemoteIdentifiers"')
    assert json.loads(text)["Other"] == {"version": "1.0.0"}


def test_replace_index_entry_updates_existing_entry_only(sync_module):
    """Replacing the same key touches only its value block."""
    merged = sync_module.merge_index_entry(UPSTREAM_ENTRY, None)
    original = '{\n  "RemoteIdentifiers": {\n    "version": "2.3"\n  },\n  "Other": {\n    "version": "1"\n  }\n}\n'

    text = sync_module.replace_index_entry_text(original, "RemoteIdentifiers", merged)
    parsed = json.loads(text)

    assert parsed["RemoteIdentifiers"] == merged
    assert parsed["Other"] == {"version": "1"}
    assert '  "Other": {\n    "version": "1"\n  }' in text


def test_ensure_readme_entry_adds_once(sync_module):
    """The root README gains one plugin-list entry and stays idempotent."""
    merged = sync_module.merge_index_entry(UPSTREAM_ENTRY, None)
    original = "# Repo\n\n## 插件清单\n\n### [旧插件](plugins.v3/old/README.md)\n\n旧插件说明。\n"

    text = sync_module.ensure_readme_entry(original, merged)

    assert "plugins/remoteidentifiers/README.md" in text
    assert text.index("plugins/remoteidentifiers/README.md") < text.index("plugins.v3/old/README.md")
    assert sync_module.ensure_readme_entry(text, merged) == text
