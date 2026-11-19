"""writer.py 能力探测与写入适配的 focused 单测.

只覆盖写入适配本身：伪造宿主 ``DownloaderHelper`` / ``ServiceInfo`` / ``DownloadChain`` 与
三端下载器实例（记录调用入参），用可注入的 fake sleep 断言重试次数与间隔，不实例化真实
组合根、不访问网络、数据库或真实下载器，也不 mock 被测函数本身。
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.plugins.subscribedownloadattr import writer

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "plugins.v3/subscribedownloadattr/writer.py"
LEGACY_MODULE_PREFIXES = ("app.core.", "app.helper.", "app.utils.", "app.sdk._legacy")
ALLOWED_APP_IMPORTS = {"app.sdk.logging", "app.sdk.services", "app.chain.download"}
ALLOWED_STDLIB_IMPORTS = {"__future__", "time", "collections.abc", "dataclasses", "enum", "typing"}
# 宿主每个 endpoint 都返回的三字段 envelope 与本次无关，这里只用 sentinel 区分"未设置"
_UNSET = object()


class _Service:
    """伪造宿主 ``ServiceInfo``：只保留解析写入适配需要的字段."""

    def __init__(self, name: str, instance: Any, type_name: str | None) -> None:
        self.name = name
        self.instance = instance
        self.module = None
        self.type = type_name
        self.config = object()


class _FakeHelper:
    """伪造 ``DownloaderHelper``：记录查询名称，可按需抛错."""

    def __init__(self, services: dict[str, _Service] | None = None, error: Exception | None = None) -> None:
        self._services = dict(services or {})
        self._error = error
        self.names: list[str | None] = []

    def get_service(self, name: str | None) -> _Service | None:
        """返回登记的伪服务信息，模拟宿主同名查询语义."""
        self.names.append(name)
        if self._error is not None:
            raise self._error
        return self._services.get(name or "")


class _FakeChain:
    """伪造 ``DownloadChain``：记录写入入参，返回值可逐用例改."""

    def __init__(self) -> None:
        self.tag_calls: list[tuple[list[str], list[str], str | None]] = []
        self.update_calls: list[tuple[str, str | None, str | None]] = []
        self.tag_result: Any = True
        self.update_result: Any = {"category": True}

    def set_torrents_tag(self, hashs: list[str], tags: list[str], downloader: str | None = None) -> Any:
        """记录标签写入并返回预设结果."""
        self.tag_calls.append((list(hashs), list(tags), downloader))
        return self.tag_result

    def update_torrent(self, hash_string: str, downloader: str | None = None, category: str | None = None) -> Any:
        """记录属性更新并返回预设结构."""
        self.update_calls.append((hash_string, downloader, category))
        return self.update_result


class _SleepRecorder:
    """替换 ``time.sleep``：记录间隔，不真实等待."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def __call__(self, seconds: float) -> None:
        """记录一次等待间隔."""
        self.sleeps.append(seconds)


class _Resolver:
    """解析夹具：注入伪 helper / chain 工厂，并记录 chain 被构造的次数."""

    def __init__(self, services: dict[str, _Service] | None = None, helper_error: Exception | None = None) -> None:
        self.helper = _FakeHelper(services, error=helper_error)
        self.chain = _FakeChain()
        self.chain_built = 0

    def _helper_factory(self) -> _FakeHelper:
        """返回伪 helper，避免触碰真实服务目录."""
        return self.helper

    def _chain_factory(self) -> _FakeChain:
        """返回伪 chain 并计数，用于断言是否走到回退通道."""
        self.chain_built += 1
        return self.chain

    def resolve(
        self,
        name: str | None,
        *,
        sleep: Any = None,
        retry_delays: tuple[float, ...] = writer.DEFAULT_RETRY_DELAYS,
    ) -> writer.TorrentAttrWriter:
        """按伪工厂解析写入适配，重试节奏可注入."""
        return writer.resolve_writer(
            name,
            helper_factory=self._helper_factory,
            chain_factory=self._chain_factory,
            sleep=sleep,
            retry_delays=retry_delays,
        )


class _FakeQbittorrent:
    """qBittorrent 伪实例：默认只有追加标签与查询任务能力."""

    def __init__(
        self,
        accepted: Any = True,
        error: Exception | None = None,
        torrents: list[Any] | None = None,
        error_flag: bool = False,
    ) -> None:
        self.accepted = accepted
        self.error = error
        self.torrents = list(torrents or [])
        self.error_flag = error_flag
        self.tag_calls: list[tuple[list[str], list[str]]] = []
        self.torrents_calls: list[list[str]] = []

    def set_torrents_tag(self, ids: list[str], tags: list[str]) -> Any:
        """记录追加标签入参并返回预设结果."""
        self.tag_calls.append((list(ids), list(tags)))
        if self.error is not None:
            raise self.error
        return self.accepted

    def get_torrents(
        self,
        ids: list[str] | None = None,
        status: Any = None,
        tags: Any = None,
    ) -> tuple[list[Any], bool]:
        """按宿主形态返回 ``(任务列表, 错误标志)``."""
        self.torrents_calls.append(list(ids or []))
        return list(self.torrents), self.error_flag


class _FakeQbittorrentWithCategory(_FakeQbittorrent):
    """带分类写入能力的 qBittorrent 伪实例."""

    def __init__(self, category_accepted: Any = True, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.category_accepted = category_accepted
        self.category_calls: list[tuple[str, str]] = []

    def set_torrent_category(self, hash_string: str, category: str) -> Any:
        """记录分类写入入参并返回预设结果."""
        self.category_calls.append((hash_string, category))
        return self.category_accepted


class _ExplodingReads(_FakeQbittorrent):
    """查询任务直接抛异常的 qBittorrent 伪实例."""

    def get_torrents(
        self,
        ids: list[str] | None = None,
        status: Any = None,
        tags: Any = None,
    ) -> tuple[list[Any], bool]:
        """模拟 RPC 异常，验证读取异常被收敛."""
        raise RuntimeError("rpc down")


class _FakeTransmission:
    """Transmission 伪实例：覆盖写入，需先读原标签."""

    def __init__(
        self,
        original: list[str] | None = None,
        read_error: Exception | None = None,
        read_result: Any = _UNSET,
        accepted: Any = True,
    ) -> None:
        self.original = list(original if original is not None else ["MOVIEPILOT", "tv"])
        self.read_error = read_error
        self.read_result = read_result
        self.accepted = accepted
        self.read_calls: list[list[str]] = []
        self.write_calls: list[tuple[list[str], list[str], list[str]]] = []

    def get_torrent_tags(self, ids: list[str]) -> Any:
        """按预设返回原标签，可按需抛错或返回错误结构."""
        self.read_calls.append(list(ids))
        if self.read_error is not None:
            raise self.read_error
        if self.read_result is not _UNSET:
            return self.read_result
        return list(self.original)

    def set_torrent_tag(self, ids: list[str], tags: list[str], org_tags: list[str] | None = None) -> Any:
        """记录覆盖写入入参（含 ``org_tags``）并返回预设结果."""
        self.write_calls.append((list(ids), list(tags), list(org_tags or [])))
        return self.accepted


class _FakeTransmissionReadOnly:
    """只有读标签能力、没有写标签方法的 Transmission 伪实例."""

    def get_torrent_tags(self, ids: list[str]) -> list[str]:
        """返回空标签列表，模拟只读实例."""
        return []


class _FakeRtorrent:
    """rTorrent 伪实例：合并写入，读标签只接受单个 hash."""

    def __init__(self, labels: dict[str, list[str]] | None = None, accepted: Any = True) -> None:
        self.labels = dict(labels or {})
        self.accepted = accepted
        self.read_calls: list[str] = []
        self.write_calls: list[tuple[list[str], list[str], bool]] = []

    def get_torrent_tags(self, ids: str) -> list[str]:
        """记录单 hash 读取并返回预设标签."""
        self.read_calls.append(ids)
        return list(self.labels.get(ids, []))

    def set_torrents_tag(self, ids: list[str], tags: list[str], overwrite: bool = False) -> Any:
        """记录合并写入入参并返回预设结果."""
        self.write_calls.append((list(ids), list(tags), overwrite))
        return self.accepted


# ---------------- 能力探测 ----------------


def test_capability_probe():
    """只有 set_torrents_tag 的 qB 实例标签可写、分类 unsupported；带分类方法时分类可写."""
    plain = _FakeQbittorrent()
    resolver = _Resolver({"qb": _Service("qb", plain, "qbittorrent")})
    plain_writer = resolver.resolve("qb")

    assert resolver.chain_built == 0
    assert plain_writer.capabilities == writer.WriterCapabilities(
        kind="qbittorrent",
        can_read_tags=True,
        can_add_tags=True,
        can_set_category=False,
    )
    assert plain_writer.supports_category is False

    tagged = plain_writer.add_tags(["abc"], ["tv"])
    assert tagged.status is writer.WriterStatus.OK
    assert tagged.attempts == 1
    assert plain.tag_calls == [(["abc"], ["tv"])]

    unsupported = plain_writer.set_category("abc", "电视剧")
    assert unsupported.status is writer.WriterStatus.UNSUPPORTED
    assert unsupported.reason

    full = _FakeQbittorrentWithCategory()
    full_writer = _Resolver({"qb": _Service("qb", full, "qbittorrent")}).resolve("qb")
    assert full_writer.supports_category is True
    written = full_writer.set_category("abc", "电视剧")
    assert written.status is writer.WriterStatus.OK
    assert full.category_calls == [("abc", "电视剧")]


def test_type_whitelist_and_missing_service_fall_back():
    """类型不在白名单、服务缺失、查询抛错或无名称时都回退 Chain 且不抛异常."""
    aria = _Resolver({"aria": _Service("aria", _FakeQbittorrent(), "aria2")})
    aria_writer = aria.resolve("aria")
    assert aria.chain_built == 1
    assert aria_writer.capabilities.kind == "chain"

    missing = _Resolver()
    assert missing.resolve("nope").capabilities.kind == "chain"
    assert missing.helper.names == ["nope"]

    broken = _Resolver(helper_error=RuntimeError("服务目录尚未装配"))
    broken_writer = broken.resolve("qb")
    assert broken_writer.capabilities.kind == "chain"
    assert broken_writer.add_tags(["h1"], ["tv"]).status is writer.WriterStatus.OK

    no_name = _Resolver()
    assert no_name.resolve(None).capabilities.kind == "chain"
    assert no_name.helper.names == []


# ---------------- Transmission 语义 ----------------


def test_transmission_preserves_original_tags():
    """Transmission 覆盖写入必须带上原标签, 实际 ids/tags 与 org_tags 都要断言."""
    instance = _FakeTransmission(original=["MOVIEPILOT", "tv"])
    resolved = _Resolver({"tr": _Service("tr", instance, "transmission")}).resolve("tr")

    result = resolved.add_tags(["h1", "h2"], ["anime"])
    assert result.status is writer.WriterStatus.OK
    assert instance.read_calls == [["h1", "h2"]]
    assert instance.write_calls == [(["h1", "h2"], ["anime"], ["MOVIEPILOT", "tv"])]
    assert resolved.supports_category is False
    assert resolved.set_category("h1", "电视剧").status is writer.WriterStatus.UNSUPPORTED


def test_transmission_read_failure_skips_write():
    """读原标签抛异常或返回错误结构时一次都不写, 结果 failed 且原因可读."""
    cases: list[dict[str, Any]] = [
        {"read_error": RuntimeError("rpc down")},
        {"read_result": None},
    ]
    for case in cases:
        instance = _FakeTransmission(**case)
        sleep = _SleepRecorder()
        resolved = _Resolver({"tr": _Service("tr", instance, "transmission")}).resolve("tr", sleep=sleep)

        result = resolved.add_tags(["h1"], ["anime"])
        assert instance.write_calls == []
        assert instance.read_calls == [["h1"]] * 3
        assert result.status is writer.WriterStatus.FAILED
        assert result.attempts == 3
        assert result.reason
        assert sleep.sleeps == [2.0, 5.0]


# ---------------- 回退通道 ----------------


def test_chain_fallback():
    """无实例时走 Chain: 分类 False 记 unsupported、None 记 failed、标签 None 不算成功."""
    resolver = _Resolver()
    resolved = resolver.resolve("down")

    assert resolver.helper.names == ["down"]
    assert resolver.chain_built == 1
    assert resolved.capabilities.kind == "chain"
    assert resolved.supports_category is None
    assert resolved.read_tags(["h1"]) == {}
    assert resolved.read_tags_ex(["h1"]).result.status is writer.WriterStatus.UNSUPPORTED

    resolver.chain.update_result = {"category": False}
    assert resolved.set_category("h1", "电视剧").status is writer.WriterStatus.UNSUPPORTED

    resolver.chain.update_result = None
    sleep = _SleepRecorder()
    retrying = resolver.resolve("down", sleep=sleep)
    failed = retrying.set_category("h1", "电视剧")
    assert failed.status is writer.WriterStatus.FAILED
    assert failed.attempts == 3
    assert sleep.sleeps == [2.0, 5.0]

    resolver.chain.update_result = {"save_path": True}
    assert retrying.set_category("h1", "电视剧").status is writer.WriterStatus.FAILED

    resolver.chain.tag_result = None
    tag_failed = retrying.add_tags(["h1"], ["tv"])
    assert tag_failed.status is writer.WriterStatus.FAILED
    assert tag_failed.attempts == 3

    resolver.chain.tag_result = True
    tag_ok = retrying.add_tags(["h1", "h2"], ["tv"])
    assert tag_ok.status is writer.WriterStatus.OK
    assert resolver.chain.tag_calls[-1] == (["h1", "h2"], ["tv"], "down")


def test_chain_unavailable_reports_failed():
    """回退通道构造失败时写入记 failed, 不向外抛异常."""

    def _broken_chain() -> Any:
        """模拟 DownloadChain 构造失败."""
        raise RuntimeError("chain unavailable")

    sleep = _SleepRecorder()
    resolved = writer.resolve_writer(
        "down",
        helper_factory=lambda: _FakeHelper(),
        chain_factory=_broken_chain,
        sleep=sleep,
    )

    tagged = resolved.add_tags(["h1"], ["tv"])
    assert tagged.status is writer.WriterStatus.FAILED
    assert tagged.attempts == 1
    categorized = resolved.set_category("h1", "电视剧")
    assert categorized.status is writer.WriterStatus.FAILED
    assert sleep.sleeps == []


# ---------------- 空值跳过 ----------------


def test_empty_category_skips_write():
    """空分类一次都不调用 set_torrent_category（qBittorrent 语义）, 结果 skipped 且不重试."""
    instance = _FakeQbittorrentWithCategory()
    sleep = _SleepRecorder()
    resolved = _Resolver({"qb": _Service("qb", instance, "qbittorrent")}).resolve("qb", sleep=sleep)

    for empty in ("", "   "):
        result = resolved.set_category("h1", empty)
        assert result.status is writer.WriterStatus.SKIPPED
        assert result.attempts == 1
    assert instance.category_calls == []
    assert sleep.sleeps == []

    chain_resolver = _Resolver()
    chain_writer = chain_resolver.resolve("down")
    assert chain_writer.set_category("h1", " ").status is writer.WriterStatus.SKIPPED
    assert chain_resolver.chain.update_calls == []


def test_empty_tags_or_hashs_skipped():
    """空 hash 或空标签一律 skipped, 不调用下载器也不重试."""
    instance = _FakeQbittorrent()
    sleep = _SleepRecorder()
    resolved = _Resolver({"qb": _Service("qb", instance, "qbittorrent")}).resolve("qb", sleep=sleep)

    assert resolved.add_tags([], ["tv"]).status is writer.WriterStatus.SKIPPED
    assert resolved.add_tags(["h1"], []).status is writer.WriterStatus.SKIPPED
    assert resolved.add_tags(["  "], ["   "]).status is writer.WriterStatus.SKIPPED
    assert resolved.read_tags([]) == {}
    assert resolved.read_tags_ex(["  "]).result.status is writer.WriterStatus.SKIPPED
    assert instance.tag_calls == []
    assert instance.torrents_calls == []
    assert sleep.sleeps == []

    chain_resolver = _Resolver()
    chain_writer = chain_resolver.resolve("down")
    assert chain_writer.add_tags(["h1"], []).status is writer.WriterStatus.SKIPPED
    assert chain_resolver.chain.tag_calls == []


# ---------------- 重试 ----------------


def test_retry_times_injected():
    """默认重试间隔 [2,5]、总尝试 3 次; 注入间隔生效; unsupported 不重试."""
    instance = _FakeQbittorrent(accepted=False)
    sleep = _SleepRecorder()
    resolved = _Resolver({"qb": _Service("qb", instance, "qbittorrent")}).resolve("qb", sleep=sleep)

    result = resolved.add_tags(["h1"], ["tv"])
    assert result.status is writer.WriterStatus.FAILED
    assert result.attempts == 3
    assert len(instance.tag_calls) == 3
    assert sleep.sleeps == [2.0, 5.0]
    assert writer.DEFAULT_RETRY_DELAYS == (2.0, 5.0)

    custom_sleep = _SleepRecorder()
    custom = _Resolver({"qb2": _Service("qb2", _FakeQbittorrent(accepted=False), "qbittorrent")}).resolve(
        "qb2",
        sleep=custom_sleep,
        retry_delays=(0.5,),
    )
    assert custom.add_tags(["h1"], ["tv"]).attempts == 2
    assert custom_sleep.sleeps == [0.5]

    readonly_sleep = _SleepRecorder()
    readonly = _Resolver({"tr": _Service("tr", _FakeTransmissionReadOnly(), "transmission")}).resolve(
        "tr",
        sleep=readonly_sleep,
    )
    unsupported = readonly.add_tags(["h1"], ["tv"])
    assert unsupported.status is writer.WriterStatus.UNSUPPORTED
    assert unsupported.attempts == 1
    assert readonly_sleep.sleeps == []


def test_write_exception_returns_failed():
    """写入抛异常时收敛为 failed, 重试次数与普通失败一致且不向外抛."""
    instance = _FakeQbittorrent(error=RuntimeError("rpc down"))
    sleep = _SleepRecorder()
    resolved = _Resolver({"qb": _Service("qb", instance, "qbittorrent")}).resolve("qb", sleep=sleep)

    result = resolved.add_tags(["h1"], ["tv"])
    assert result.status is writer.WriterStatus.FAILED
    assert "rpc down" in result.reason
    assert result.attempts == 3
    assert len(instance.tag_calls) == 3
    assert sleep.sleeps == [2.0, 5.0]


# ---------------- 读取标签 ----------------


def test_qbittorrent_read_tags():
    """解析 tags 逗号串（qBittorrent 语义）, 支持 dict 与对象任务; error_flag 与异常都记 failed."""
    tasks = [
        {"hash": "H1", "tags": "a, b ,, "},
        SimpleNamespace(hash="h2", tags=["x", " x ", "y"]),
        {"hash": "h3", "tags": "other"},
    ]
    instance = _FakeQbittorrent(torrents=tasks)
    resolved = _Resolver({"qb": _Service("qb", instance, "qbittorrent")}).resolve("qb")

    read = resolved.read_tags_ex(["h1", "h2"])
    assert read.tags == {"H1": ["a", "b"], "h2": ["x", "y"]}
    assert read.result.status is writer.WriterStatus.OK
    assert instance.torrents_calls == [["h1", "h2"]]

    instance.error_flag = True
    flagged = resolved.read_tags_ex(["h1"])
    assert flagged.tags == {}
    assert flagged.result.status is writer.WriterStatus.FAILED
    assert "错误标志" in flagged.result.reason

    exploding = _Resolver({"qb": _Service("qb", _ExplodingReads(), "qbittorrent")}).resolve("qb")
    broken = exploding.read_tags_ex(["h1"])
    assert broken.tags == {}
    assert broken.result.status is writer.WriterStatus.FAILED
    assert "rpc down" in broken.result.reason
    assert exploding.read_tags(["h1"]) == {}


def test_rtorrent_and_transmission_read_tags():
    """逐 hash 读取单 hash 标签（rT 语义）, TR 也逐 hash 读取且只传单 hash 列表, 均不写入."""
    rtorrent = _FakeRtorrent(labels={"h1": ["a"], "h2": []})
    rtorrent_writer = _Resolver({"rt": _Service("rt", rtorrent, "rtorrent")}).resolve("rt")
    assert rtorrent_writer.read_tags(["h1", "h2"]) == {"h1": ["a"], "h2": []}
    assert rtorrent.read_calls == ["h1", "h2"]

    transmission = _FakeTransmission(original=["MOVIEPILOT"])
    transmission_writer = _Resolver({"tr": _Service("tr", transmission, "transmission")}).resolve("tr")
    assert transmission_writer.read_tags(["h1", "h2"]) == {"h1": ["MOVIEPILOT"], "h2": ["MOVIEPILOT"]}
    assert transmission.read_calls == [["h1"], ["h2"]]
    assert transmission.write_calls == []


def test_rtorrent_write_merges():
    """写标签必须传 overwrite=False 合并（rTorrent 语义）, 不覆盖原标签."""
    rtorrent = _FakeRtorrent()
    resolved = _Resolver({"rt": _Service("rt", rtorrent, "rtorrent")}).resolve("rt")

    result = resolved.add_tags(["h1", "h2"], ["tv"])
    assert result.status is writer.WriterStatus.OK
    assert rtorrent.write_calls == [(["h1", "h2"], ["tv"], False)]


# ---------------- 缓存与依赖边界 ----------------


def test_capability_cache_is_process_local_and_clearable():
    """能力探测缓存只在进程内, 不同实例各占一条, 可整体清理."""
    writer.clear_capability_cache()
    assert writer.capability_cache_size() == 0

    first = _FakeQbittorrent()
    second = _FakeQbittorrentWithCategory()
    _Resolver({"qb": _Service("qb", first, "qbittorrent")}).resolve("qb")
    assert writer.capability_cache_size() == 1
    _Resolver({"qb": _Service("qb", second, "qbittorrent")}).resolve("qb")
    assert writer.capability_cache_size() == 2

    writer.clear_capability_cache()
    assert writer.capability_cache_size() == 0


def test_writer_only_uses_allowed_imports():
    """writer.py 只用稳定宿主导入与标准库, 不导入产品模块、SDK 客户端或旧路径."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    modules: list[str] = []
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)

    assert "DownloaderType" not in imported_names
    assert not [name for name in modules if name.startswith(LEGACY_MODULE_PREFIXES)]
    assert not [name for name in modules if name.startswith("app.modules")]
    assert {name for name in modules if name.startswith("app.")} <= ALLOWED_APP_IMPORTS
    stdlib = {name for name in modules if not name.startswith("app.")}
    assert stdlib <= ALLOWED_STDLIB_IMPORTS
