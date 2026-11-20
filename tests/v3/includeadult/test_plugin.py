"""Assembly, restore and config contract tests for the include_adult plugin.

The wrappers are installed on the real host symbols (``RequestUtils.request`` and
``AsyncRequestUtils._make_request``) but the underlying implementation is replaced by
a recording fake, so no request ever leaves the process. Every attribute a test may
replace is restored by an autouse fixture, which keeps the session clean for the
other plugin suites.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import json
import sys
import types
from pathlib import Path
from typing import Any

import httpx2
import pytest
import requests
from app.sdk.network import AsyncRequestUtils, RequestUtils

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "plugins.v3/includeadult/__init__.py"
PLUGIN_ID = "includeadult"
CLASS_NAME = "IncludeAdult"
SEARCH_URL = "https://api.themoviedb.org/3/search/movie?query=avatar&page=1"
DETAIL_URL = "https://api.themoviedb.org/3/movie/550"


def _load_plugin() -> Any:
    """Import the plugin module through the production namespace."""
    return importlib.import_module(f"app.plugins.{PLUGIN_ID}")


def _load_patcher() -> Any:
    """Import the plugin patcher module through the production namespace."""
    return importlib.import_module(f"app.plugins.{PLUGIN_ID}.patcher")


def _make_plugin() -> Any:
    """Instantiate the plugin without the host constructor and without runtime state."""
    return object.__new__(_load_plugin().IncludeAdult)


class _RecordingLogger:
    """Collect the debug and warning lines emitted by the patcher."""

    def __init__(self) -> None:
        self.debugs: list[str] = []
        self.warnings: list[str] = []

    def debug(self, message: str) -> None:
        """Record one debug line."""
        self.debugs.append(message)

    def warning(self, message: str) -> None:
        """Record one warning line."""
        self.warnings.append(message)


def _is_placeholder_stmt(stmt: ast.stmt) -> bool:
    """Mirror the host helper that decides whether a statement is a placeholder."""
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
    """Return True when the method body only consists of placeholder statements."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    for cls in [node for node in tree.body if isinstance(node, ast.ClassDef)]:
        if cls.name != CLASS_NAME:
            continue
        for fn in cls.body:
            if isinstance(fn, ast.FunctionDef) and fn.name == method_name:
                return all(_is_placeholder_stmt(stmt) for stmt in fn.body)
    raise AssertionError(f"method {method_name} not found")


def _collect_components(node: Any) -> list[dict[str, Any]]:
    """Flatten the form tree into a list of component nodes."""
    nodes: list[dict[str, Any]] = []
    if isinstance(node, dict):
        nodes.append(node)
        for child in node.get("content") or ():
            nodes.extend(_collect_components(child))
    elif isinstance(node, list):
        for child in node:
            nodes.extend(_collect_components(child))
    return nodes


@pytest.fixture(autouse=True)
def restore_host_state():
    """Restore patched host symbols and the module level active patcher after each test."""
    targets = (
        (RequestUtils, "request"),
        (AsyncRequestUtils, "_make_request"),
        (requests.sessions.Session, "send"),
        (httpx2.AsyncClient, "request"),
    )
    originals = [(owner, name, inspect.getattr_static(owner, name)) for owner, name in targets]
    yield
    _load_patcher()._ACTIVE_PATCHER = None
    for owner, name, original in originals:
        setattr(owner, name, original)


# ---------------- config normalization ----------------


def test_normalize_config_defaults_to_disabled():
    """Missing or None config normalizes to a disabled switch with no issues."""
    module = _load_plugin()

    assert module.normalize_config(None) == ({"enabled": False}, [])
    assert module.normalize_config({}) == ({"enabled": False}, [])
    assert module.normalize_config({"enabled": False}) == ({"enabled": False}, [])
    assert module.normalize_config({"enabled": True}) == ({"enabled": True}, [])


def test_normalize_config_accepts_string_switches():
    """String switches coming from the frontend are normalized case insensitively."""
    module = _load_plugin()

    assert module.normalize_config({"enabled": "true"}) == ({"enabled": True}, [])
    assert module.normalize_config({"enabled": " TRUE "}) == ({"enabled": True}, [])
    assert module.normalize_config({"enabled": "false"}) == ({"enabled": False}, [])
    assert module.normalize_config({"enabled": ""}) == ({"enabled": False}, [])


def test_normalize_config_reports_unusable_values():
    """Bad values fall back to disabled and leave a human readable issue."""
    module = _load_plugin()

    for bad in (1, 0.5, "maybe", ["enabled"], object()):
        normalized, issues = module.normalize_config({"enabled": bad})
        assert normalized == {"enabled": False}
        assert len(issues) == 1
        assert isinstance(issues[0], str) and issues[0]

    not_a_mapping, issues = module.normalize_config(["enabled"])
    assert not_a_mapping == {"enabled": False}
    assert len(issues) == 1


def test_normalize_config_is_json_serializable():
    """The normalized config can be persisted as JSON without extra conversion."""
    module = _load_plugin()

    normalized, issues = module.normalize_config({"enabled": "true", "unknown": {"nested": 1}})

    assert issues == []
    assert json.loads(json.dumps(normalized)) == {"enabled": True}


# ---------------- form and page hooks ----------------


def test_get_form_exposes_a_single_english_switch():
    """The config page holds exactly one VSwitch bound to enabled with an English label."""
    plugin = _make_plugin()

    form, model = plugin.get_form()
    components = _collect_components(form)
    switches = [node for node in components if node.get("component") == "VSwitch"]

    assert len(switches) == 1
    props = switches[0]["props"]
    assert props["model"] == "enabled"
    assert props["label"] == "Enable include_adult=true for TMDB search"
    assert props["label"].isascii()
    assert model == {"enabled": False}


def test_get_page_stays_a_placeholder():
    """get_page keeps the docstring + pass form so no data page entry is offered."""
    plugin = _make_plugin()

    assert plugin.get_page() is None
    assert _method_body_is_placeholder("get_page")


# ---------------- install and restore ----------------


def test_install_wraps_both_layers_and_uninstall_restores_them():
    """Both transport entry points are wrapped, recorded and restored exactly."""
    sync_before = inspect.getattr_static(RequestUtils, "request")
    async_before = inspect.getattr_static(AsyncRequestUtils, "_make_request")
    patcher = _load_patcher().SearchParamPatcher(logger=_RecordingLogger())

    result = patcher.install()

    assert result.status == "installed"
    assert len(result.targets) == 2
    assert any(label.endswith(".request") for label in result.targets)
    assert any(label.endswith("._make_request") for label in result.targets)

    sync_wrapper = inspect.getattr_static(RequestUtils, "request")
    async_wrapper = inspect.getattr_static(AsyncRequestUtils, "_make_request")
    assert sync_wrapper is not sync_before
    assert sync_wrapper.__wrapped__ is sync_before
    assert inspect.iscoroutinefunction(async_wrapper)
    assert async_wrapper.__wrapped__ is async_before
    assert patcher.originals == (
        (RequestUtils, "request", sync_before),
        (AsyncRequestUtils, "_make_request", async_before),
    )

    patcher.uninstall()

    assert inspect.getattr_static(RequestUtils, "request") is sync_before
    assert inspect.getattr_static(AsyncRequestUtils, "_make_request") is async_before
    assert patcher.installed is False
    assert patcher.originals == ()


def test_sync_wrapper_rewrites_search_requests_only(monkeypatch):
    """The sync wrapper appends the key for search URLs and passes everything else through."""
    calls: list[dict[str, Any]] = []

    def fake_request(self, method, url, raise_exception=False, **kwargs):
        """Record the rewritten call and return a sentinel."""
        calls.append({"method": method, "url": url, "kwargs": kwargs})
        return "sync-sentinel"

    monkeypatch.setattr(RequestUtils, "request", fake_request)
    patcher = _load_patcher().SearchParamPatcher(logger=_RecordingLogger())
    patcher.install()
    wrapper = inspect.getattr_static(RequestUtils, "request")

    result = wrapper(object(), "GET", "https://api.themoviedb.org/3/search/movie?api_key=k", params="query=avatar")
    assert result == "sync-sentinel"
    assert calls[0]["url"] == "https://api.themoviedb.org/3/search/movie"
    assert calls[0]["kwargs"]["params"] == "api_key=k&query=avatar&include_adult=true"
    assert patcher.hits == 1

    assert wrapper(object(), "GET", DETAIL_URL) == "sync-sentinel"
    assert calls[1]["url"] == DETAIL_URL
    assert "params" not in calls[1]["kwargs"]
    assert patcher.hits == 1

    patcher.uninstall()
    assert inspect.getattr_static(RequestUtils, "request") is fake_request


def test_sync_wrapper_propagates_original_exception(monkeypatch):
    """An exception raised by the original implementation is never swallowed."""

    class BoomTransportError(RuntimeError):
        """Sentinel exception raised by the fake transport."""

    def fake_request(self, method, url, raise_exception=False, **kwargs):
        """Raise the sentinel exception like a failing transport would."""
        raise BoomTransportError("boom")

    monkeypatch.setattr(RequestUtils, "request", fake_request)
    patcher = _load_patcher().SearchParamPatcher(logger=_RecordingLogger())
    patcher.install()
    wrapper = inspect.getattr_static(RequestUtils, "request")

    with pytest.raises(BoomTransportError, match="boom"):
        wrapper(object(), "GET", SEARCH_URL)

    assert patcher.hits == 1
    patcher.uninstall()


def test_async_wrapper_is_a_coroutine_that_awaits_the_original(monkeypatch):
    """The async wrapper rewrites params and awaits the original coroutine."""
    calls: list[dict[str, Any]] = []

    async def fake_make_request(self, client, method, url, raise_exception=False, **kwargs):
        """Record the rewritten call and return a sentinel."""
        calls.append({"method": method, "url": url, "kwargs": kwargs})
        return "async-sentinel"

    monkeypatch.setattr(AsyncRequestUtils, "_make_request", fake_make_request)
    patcher = _load_patcher().SearchParamPatcher(logger=_RecordingLogger())
    patcher.install()
    wrapper = inspect.getattr_static(AsyncRequestUtils, "_make_request")

    assert inspect.iscoroutinefunction(wrapper)
    result = asyncio.run(wrapper(object(), object(), "GET", SEARCH_URL))

    assert result == "async-sentinel"
    assert calls[0]["url"] == "https://api.themoviedb.org/3/search/movie"
    assert calls[0]["kwargs"]["params"] == "query=avatar&page=1&include_adult=true"
    assert patcher.hits == 1

    patcher.uninstall()
    assert inspect.getattr_static(AsyncRequestUtils, "_make_request") is fake_make_request


def test_non_coroutine_async_target_is_reported_and_falls_back(monkeypatch):
    """A non-coroutine _make_request is skipped with an issue and httpx is wrapped instead."""
    fake_module = types.ModuleType("app.sdk.network")

    class FakeRequestUtils:
        def request(self, method, url, raise_exception=False, **kwargs):
            """Return without sending anything."""
            return None

    class FakeAsyncRequestUtils:
        def _make_request(self, client, method, url, raise_exception=False, **kwargs):
            """Return without sending anything; intentionally not a coroutine."""
            return None

    # Inject module attributes through __dict__ so dynamic names stay out of static typing.
    fake_module.__dict__["RequestUtils"] = FakeRequestUtils
    fake_module.__dict__["AsyncRequestUtils"] = FakeAsyncRequestUtils
    monkeypatch.setitem(sys.modules, "app.sdk.network", fake_module)

    sync_original = FakeRequestUtils.request
    async_original = FakeAsyncRequestUtils._make_request
    httpx_before = inspect.getattr_static(httpx2.AsyncClient, "request")
    patcher = _load_patcher().SearchParamPatcher(logger=_RecordingLogger())

    result = patcher.install()

    assert result.installed is True
    assert any("is not a coroutine function" in issue for issue in result.issues)
    assert FakeAsyncRequestUtils._make_request is async_original
    assert not any("_make_request" in label for label in result.targets)
    assert any(label.endswith("AsyncClient.request") for label in result.targets)
    assert inspect.getattr_static(httpx2.AsyncClient, "request") is not httpx_before
    assert FakeRequestUtils.request is not sync_original

    patcher.uninstall()

    assert FakeRequestUtils.request is sync_original
    assert inspect.getattr_static(httpx2.AsyncClient, "request") is httpx_before


def test_missing_async_transport_keeps_the_sync_layer_only(monkeypatch):
    """With no usable async entry point the install is partial and only sync is wrapped."""
    fake_module = types.ModuleType("app.sdk.network")

    class FakeRequestUtils:
        def request(self, method, url, raise_exception=False, **kwargs):
            """Return without sending anything."""
            return None

    # Inject module attributes through __dict__ so dynamic names stay out of static typing.
    fake_module.__dict__["RequestUtils"] = FakeRequestUtils
    fake_module.__dict__["AsyncRequestUtils"] = None
    monkeypatch.setitem(sys.modules, "app.sdk.network", fake_module)
    monkeypatch.setitem(sys.modules, "httpx2", types.ModuleType("httpx2"))
    monkeypatch.setitem(sys.modules, "httpx", types.ModuleType("httpx"))

    sync_original = FakeRequestUtils.request
    patcher = _load_patcher().SearchParamPatcher(logger=_RecordingLogger())

    result = patcher.install()

    assert result.status == "partial"
    assert result.installed is True
    assert len(result.targets) == 1
    assert result.targets[0].endswith(".request")
    assert any("AsyncRequestUtils" in issue for issue in result.issues)
    assert FakeRequestUtils.request is not sync_original

    patcher.uninstall()
    assert FakeRequestUtils.request is sync_original


def test_third_party_replacement_is_not_overwritten(monkeypatch):
    """A symbol replaced after install is left alone and reported as a warning."""
    logger = _RecordingLogger()
    patcher = _load_patcher().SearchParamPatcher(logger=logger)
    patcher.install()

    def third_party(self, method, url, raise_exception=False, **kwargs):
        """Stand in for another plugin's wrapper."""
        return "third-party"

    monkeypatch.setattr(RequestUtils, "request", third_party)

    patcher.uninstall()

    assert inspect.getattr_static(RequestUtils, "request") is third_party
    assert any("skip restoring" in line for line in logger.warnings)
    assert patcher.installed is False


def test_install_is_idempotent_on_the_same_instance():
    """Installing twice keeps a single wrapper layer and the original is restored once."""
    original = inspect.getattr_static(RequestUtils, "request")
    patcher = _load_patcher().SearchParamPatcher(logger=_RecordingLogger())

    patcher.install()
    patcher.install()

    wrapper = inspect.getattr_static(RequestUtils, "request")
    assert wrapper.__wrapped__ is original
    assert len(patcher.originals) == 2

    patcher.uninstall()
    assert inspect.getattr_static(RequestUtils, "request") is original


def test_new_instance_takes_over_the_previous_installation():
    """A rebuilt patcher (host reload) replaces the old one instead of stacking."""
    patcher_module = _load_patcher()
    original = inspect.getattr_static(RequestUtils, "request")
    first = patcher_module.SearchParamPatcher(logger=_RecordingLogger())
    second = patcher_module.SearchParamPatcher(logger=_RecordingLogger())

    first.install()
    second.install()

    assert first.installed is False
    wrapper = inspect.getattr_static(RequestUtils, "request")
    assert wrapper.__wrapped__ is original

    second.uninstall()
    assert inspect.getattr_static(RequestUtils, "request") is original


# ---------------- plugin lifecycle and hit counting ----------------


def test_repeated_enable_disable_does_not_stack_wrappers(monkeypatch):
    """Ten enable/disable cycles always end with the untouched host symbols."""
    plugin = _make_plugin()
    monkeypatch.setattr(plugin, "save_data", lambda *args, **kwargs: None, raising=False)
    sync_original = inspect.getattr_static(RequestUtils, "request")
    async_original = inspect.getattr_static(AsyncRequestUtils, "_make_request")

    for _ in range(10):
        plugin.init_plugin({"enabled": True})
        assert plugin.get_state() is True
        wrapper = inspect.getattr_static(RequestUtils, "request")
        assert wrapper.__wrapped__ is sync_original
        assert inspect.getattr_static(AsyncRequestUtils, "_make_request").__wrapped__ is async_original

        plugin.stop_service()
        assert plugin.get_state() is False
        assert inspect.getattr_static(RequestUtils, "request") is sync_original
        assert inspect.getattr_static(AsyncRequestUtils, "_make_request") is async_original


def test_hit_counter_grows_until_the_plugin_is_disabled(monkeypatch):
    """Hits increase per rewritten request and freeze once the plugin is stopped."""
    module = _load_plugin()
    plugin = _make_plugin()
    saved: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(plugin, "save_data", lambda key, payload: saved.append((key, dict(payload))), raising=False)

    def fake_request(self, method, url, raise_exception=False, **kwargs):
        """Return without sending anything."""
        return "ok"

    monkeypatch.setattr(RequestUtils, "request", fake_request)
    plugin.init_plugin({"enabled": True})
    patcher = plugin._patcher
    assert patcher is not None
    wrapper = inspect.getattr_static(RequestUtils, "request")

    wrapper(object(), "GET", SEARCH_URL)
    assert patcher.hits == 1
    wrapper(object(), "GET", DETAIL_URL)
    assert patcher.hits == 1
    wrapper(object(), "GET", "https://api.themoviedb.org/3/search/multi?query=avatar")
    assert patcher.hits == 2

    hits_before_stop = patcher.hits
    plugin.stop_service()

    assert plugin._patcher is None
    assert inspect.getattr_static(RequestUtils, "request") is fake_request
    inspect.getattr_static(RequestUtils, "request")(object(), "GET", SEARCH_URL)
    assert patcher.hits == hits_before_stop

    assert saved[0][0] == module.DATA_KEY
    assert {"hits", "patched_at", "targets", "last_error"} <= set(saved[0][1])
    assert saved[0][1]["targets"]
    assert saved[0][1]["hits"] == 0
    assert saved[-1][0] == module.DATA_KEY
    assert saved[-1][1]["hits"] == hits_before_stop


def test_disabled_plugin_installs_nothing():
    """Enabling the plugin with enabled=False reports skipped and leaves host symbols alone."""
    plugin = _make_plugin()
    plugin.save_data = lambda *args, **kwargs: None
    sync_before = inspect.getattr_static(RequestUtils, "request")
    async_before = inspect.getattr_static(AsyncRequestUtils, "_make_request")

    plugin.init_plugin({"enabled": False})

    assert plugin.get_state() is False
    assert inspect.getattr_static(RequestUtils, "request") is sync_before
    assert inspect.getattr_static(AsyncRequestUtils, "_make_request") is async_before
    plugin.stop_service()


def test_disabling_keeps_the_hits_of_the_session_that_just_ended(monkeypatch):
    """The disable path persists the real hit count instead of the idle patcher's zero."""
    module = _load_plugin()
    plugin = _make_plugin()
    saved: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(plugin, "save_data", lambda key, payload: saved.append((key, dict(payload))), raising=False)

    def fake_request(self, method, url, raise_exception=False, **kwargs):
        """Return without sending anything."""
        return "ok"

    monkeypatch.setattr(RequestUtils, "request", fake_request)
    plugin.init_plugin({"enabled": True})
    wrapper = inspect.getattr_static(RequestUtils, "request")
    wrapper(object(), "GET", SEARCH_URL)
    wrapper(object(), "GET", "https://api.themoviedb.org/3/search/multi?query=avatar")
    wrapper(object(), "GET", DETAIL_URL)

    # init_plugin is what the host calls on disable: stop_service, then a rebuild.
    plugin.init_plugin({"enabled": False})

    assert plugin.get_state() is False
    assert plugin._patcher is not None
    assert plugin._patcher.hits == 0
    assert saved[-1][0] == module.DATA_KEY
    assert saved[-1][1]["enabled"] is False
    assert saved[-1][1]["status"] == "skipped"
    assert saved[-1][1]["hits"] == 2
    assert inspect.getattr_static(RequestUtils, "request") is fake_request


def test_idle_stop_keeps_the_stored_hit_count(monkeypatch):
    """Stopping the already disabled plugin again must not zero the stored count."""
    module = _load_plugin()
    plugin = _make_plugin()
    store: dict[str, Any] = {}
    monkeypatch.setattr(
        plugin,
        "save_data",
        lambda key, payload: store.__setitem__(key, dict(payload)),
        raising=False,
    )
    monkeypatch.setattr(
        plugin,
        "get_data",
        lambda key=None, plugin_id=None: dict(store.get(key or "", {})),
        raising=False,
    )

    def fake_request(self, method, url, raise_exception=False, **kwargs):
        """Return without sending anything."""
        return "ok"

    monkeypatch.setattr(RequestUtils, "request", fake_request)
    plugin.init_plugin({"enabled": True})
    wrapper = inspect.getattr_static(RequestUtils, "request")
    wrapper(object(), "GET", SEARCH_URL)
    wrapper(object(), "GET", SEARCH_URL)
    plugin.init_plugin({"enabled": False})
    assert store[module.DATA_KEY]["hits"] == 2

    # The host stops the disabled plugin again while reloading its module.
    plugin.stop_service()

    assert store[module.DATA_KEY]["hits"] == 2


def test_reenabling_restarts_the_hit_counter_from_zero(monkeypatch):
    """A new enabled session counts from zero and never accumulates the previous count."""
    module = _load_plugin()
    plugin = _make_plugin()
    saved: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(plugin, "save_data", lambda key, payload: saved.append((key, dict(payload))), raising=False)

    def fake_request(self, method, url, raise_exception=False, **kwargs):
        """Return without sending anything."""
        return "ok"

    monkeypatch.setattr(RequestUtils, "request", fake_request)
    plugin.init_plugin({"enabled": True})
    wrapper = inspect.getattr_static(RequestUtils, "request")
    wrapper(object(), "GET", SEARCH_URL)
    wrapper(object(), "GET", SEARCH_URL)
    plugin.init_plugin({"enabled": False})
    assert saved[-1][0] == module.DATA_KEY
    assert saved[-1][1]["hits"] == 2

    plugin.init_plugin({"enabled": True})

    assert plugin.get_state() is True
    assert saved[-1][1]["status"] == "installed"
    assert saved[-1][1]["hits"] == 0

    wrapper = inspect.getattr_static(RequestUtils, "request")
    wrapper(object(), "GET", SEARCH_URL)
    plugin.stop_service()

    assert saved[-1][1]["status"] == "skipped"
    assert saved[-1][1]["hits"] == 1


def test_reload_with_a_fresh_instance_keeps_the_persisted_hits(monkeypatch):
    """A host reload drops instance memory, so the disabled init falls back to stored hits."""
    module = _load_plugin()
    plugin = _make_plugin()
    store: dict[str, Any] = {}
    monkeypatch.setattr(
        plugin,
        "save_data",
        lambda key, payload: store.__setitem__(key, dict(payload)),
        raising=False,
    )
    monkeypatch.setattr(
        plugin,
        "get_data",
        lambda key=None, plugin_id=None: dict(store.get(key or "", {})),
        raising=False,
    )

    def fake_request(self, method, url, raise_exception=False, **kwargs):
        """Return without sending anything."""
        return "ok"

    monkeypatch.setattr(RequestUtils, "request", fake_request)
    plugin.init_plugin({"enabled": True})
    wrapper = inspect.getattr_static(RequestUtils, "request")
    wrapper(object(), "GET", SEARCH_URL)
    wrapper(object(), "GET", SEARCH_URL)
    plugin.init_plugin({"enabled": False})
    assert store[module.DATA_KEY]["hits"] == 2

    # The host reloads the module: the replacement instance carries no memory.
    fresh = _make_plugin()
    monkeypatch.setattr(
        fresh,
        "save_data",
        lambda key, payload: store.__setitem__(key, dict(payload)),
        raising=False,
    )
    monkeypatch.setattr(
        fresh,
        "get_data",
        lambda key=None, plugin_id=None: dict(store.get(key or "", {})),
        raising=False,
    )
    fresh.init_plugin({"enabled": False})

    assert fresh.get_state() is False
    assert store[module.DATA_KEY]["hits"] == 2
