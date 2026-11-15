"""下载器写入适配：能力探测、标签与分类写入的统一协议.

只依赖宿主稳定入口 ``app.sdk.services.DownloaderHelper`` 与回退通道
``app.chain.download.DownloadChain``（已评审接受的例外依赖）；不导入任何下载器产品
模块、SDK 客户端类或 ``DownloaderType`` 枚举。写入能力一律按"实例方法是否存在" +
"实例小写类型白名单（``qbittorrent`` / ``transmission`` / ``rtorrent``）"探测，缺失
记 ``unsupported`` 而不是抛错。

结果模型：``WriterResult(status, reason, attempts)``，``status`` 取 ``ok`` /
``unsupported`` / ``failed`` / ``skipped`` 四值，调用方按状态判断，不再依赖 bool 的
真假，``None`` 与 ``False`` 都不会被当成成功。读取标签额外用 ``TagReadResult`` 携带
状态与原因，使"读取失败"与"确实没有标签"可以区分。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol

from app.sdk.logging import logger

# 能力探测的类型白名单：与宿主 ServiceInfo.type 的小写取值一致，不可用 DownloaderType 枚举值比较
SUPPORTED_TYPES: tuple[str, ...] = ("qbittorrent", "transmission", "rtorrent")
# 写入失败的重试间隔：两次重试、总计三次尝试；可注入，测试不真实等待
DEFAULT_RETRY_DELAYS: tuple[float, ...] = (2.0, 5.0)

SleepFunc = Callable[[float], None]


class WriterStatus(StrEnum):
    """写入/读取结果的统一状态."""

    OK = "ok"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class WriterResult:
    """一次写入动作的统一结果，业务层按 ``status`` 判断而不是按 bool 猜."""

    status: WriterStatus
    reason: str = ""
    attempts: int = 1

    @property
    def ok(self) -> bool:
        """是否确实写入成功."""
        return self.status is WriterStatus.OK

    def describe(self) -> str:
        """返回可直接写入日志或详情页记录的一行描述."""
        text = f"{self.status.value} attempts={self.attempts}"
        return f"{text} {self.reason}".strip()


@dataclass(frozen=True)
class TagReadResult:
    """读取任务标签的结果，``tags`` 为数据、``result`` 为状态与原因."""

    tags: dict[str, list[str]]
    result: WriterResult


@dataclass(frozen=True)
class WriterCapabilities:
    """一次能力探测的结果，``kind`` 为实例小写类型或 ``chain``."""

    kind: str
    can_read_tags: bool
    can_add_tags: bool
    can_set_category: bool


class TorrentAttrWriter(Protocol):
    """插件内部统一的下载任务属性写入协议."""

    @property
    def capabilities(self) -> WriterCapabilities:
        """返回探测到的能力矩阵."""
        ...

    @property
    def supports_category(self) -> bool | None:
        """返回分类写入支持情况，None 表示类型未知需按实际写入结果判断."""
        ...

    def read_tags(self, hashs: Sequence[str]) -> dict[str, list[str]]:
        """读取任务标签，读不到时返回空字典而不是抛异常."""
        ...

    def read_tags_ex(self, hashs: Sequence[str]) -> TagReadResult:
        """读取任务标签并返回带状态的结果."""
        ...

    def add_tags(self, hashs: Sequence[str], tags: Sequence[str]) -> WriterResult:
        """以追加/合并语义写入标签，返回统一结果."""
        ...

    def set_category(self, hash_value: str, category: str) -> WriterResult:
        """写入任务分类，返回统一结果."""
        ...


class _ReadError(RuntimeError):
    """读取标签失败的内部异常，只在本模块内收敛为失败结果."""


def _text(value: Any) -> str:
    """把任意值转成去首尾空白的 str，None 返回空串."""
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else str(value).strip()


def _clean_hashs(hashs: Sequence[str] | str | None) -> list[str]:
    """规范化任务 hash：去空白、去空、去重并保持首次顺序."""
    if isinstance(hashs, str):
        candidates: list[Any] = [hashs]
    elif isinstance(hashs, Sequence):
        candidates = list(hashs)
    else:
        return []
    result: list[str] = []
    for item in candidates:
        text = _text(item)
        if text and text not in result:
            result.append(text)
    return result


def _clean_tags(tags: Sequence[str] | str | None) -> list[str]:
    """规范化标签：去空白、去空、去重并保持首次顺序（大小写不折叠）."""
    return _clean_hashs(tags)


def _split_tags(value: Any) -> list[str]:
    """把标签字段（逗号串或序列）拆成去空标签列表，无法识别时返回空列表."""
    if isinstance(value, str):
        parts: list[Any] = value.split(",")
    elif isinstance(value, Sequence):
        parts = list(value)
    else:
        return []
    return _clean_hashs(parts)


def _require_tag_list(value: Any, label: str) -> list[str]:
    """把读取结果转成标签列表，None 与非字符串/序列结构视为读取失败.

    Raises:
        _ReadError: 返回结构无法识别时抛出，由调用方收敛为 ``failed``。

    """
    if isinstance(value, (str, Sequence)):
        return _split_tags(value)
    raise _ReadError(f"{label} 返回 {type(value).__name__}，无法确认任务原有标签")


def _field_value(task: Any, name: str) -> Any:
    """按 dict 键或对象属性读取任务字段，均不存在时返回 None."""
    if isinstance(task, Mapping):
        return task.get(name)
    return getattr(task, name, None)


class _RetryWriter:
    """带重试的写入基类：只对 ``failed`` 重试，``skipped``/``unsupported`` 立即返回."""

    def __init__(
        self,
        *,
        sleep: SleepFunc | None = None,
        retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS,
    ) -> None:
        """保存注入的等待函数与重试间隔，默认走真实 ``time.sleep``."""
        self._sleep: SleepFunc = sleep or time.sleep
        self._retry_delays: tuple[float, ...] = tuple(retry_delays)

    @property
    def retry_delays(self) -> tuple[float, ...]:
        """返回本次适配使用的中重试间隔."""
        return self._retry_delays

    def _with_retry(self, label: str, action: Callable[[], WriterResult]) -> WriterResult:
        """执行写入动作，仅对失败按注入间隔重试，任何异常都收敛为失败结果."""
        attempts = 0
        while True:
            attempts += 1
            try:
                result = action()
            except Exception as err:  # 写入侧异常一律收敛，不向事件层抛出
                result = WriterResult(WriterStatus.FAILED, f"{label} 抛异常：{err}")
            if result.status is not WriterStatus.FAILED or attempts > len(self._retry_delays):
                return replace(result, attempts=attempts)
            self._sleep(self._retry_delays[attempts - 1])


class _InstanceWriter(_RetryWriter):
    """基于下载器运行实例的能力探测式写入适配."""

    def __init__(
        self,
        instance: Any,
        capabilities: WriterCapabilities,
        *,
        sleep: SleepFunc | None = None,
        retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS,
    ) -> None:
        """保存实例与探测结果，不在此处调用任何下载器方法."""
        super().__init__(sleep=sleep, retry_delays=retry_delays)
        self._instance = instance
        self._capabilities = capabilities

    @property
    def capabilities(self) -> WriterCapabilities:
        """返回探测到的能力矩阵."""
        return self._capabilities

    @property
    def supports_category(self) -> bool | None:
        """返回实例是否支持分类写入（仅 qBittorrent 且存在写分类方法）."""
        return self._capabilities.can_set_category

    def read_tags(self, hashs: Sequence[str]) -> dict[str, list[str]]:
        """读取任务标签，失败时返回空字典."""
        return self.read_tags_ex(hashs).tags

    def read_tags_ex(self, hashs: Sequence[str]) -> TagReadResult:
        """读取任务标签并返回带状态的结果，任何异常都不向外抛."""
        wanted = _clean_hashs(hashs)
        if not wanted:
            return TagReadResult({}, WriterResult(WriterStatus.SKIPPED, "没有待读取的任务 hash"))
        kind = self._capabilities.kind
        if not self._capabilities.can_read_tags:
            return TagReadResult({}, WriterResult(WriterStatus.UNSUPPORTED, f"{kind} 实例缺少读取标签的方法"))
        readers: dict[str, Callable[[list[str]], dict[str, list[str]]]] = {
            "qbittorrent": self._read_qbittorrent_tags,
            "transmission": self._read_transmission_tags,
            "rtorrent": self._read_rtorrent_tags,
        }
        reader = readers.get(kind)
        if reader is None:
            return TagReadResult({}, WriterResult(WriterStatus.UNSUPPORTED, f"未知下载器类型 {kind} 不支持读取标签"))
        try:
            tags = reader(wanted)
        except Exception as err:  # 读取失败只降级为空数据 + 原因
            return TagReadResult({}, WriterResult(WriterStatus.FAILED, f"读取标签失败：{err}"))
        return TagReadResult(tags, WriterResult(WriterStatus.OK, f"读取 {len(tags)} 个任务的标签"))

    def _read_qbittorrent_tags(self, hashs: list[str]) -> dict[str, list[str]]:
        """按 ``get_torrents(ids)`` 的 ``(tasks, error_flag)`` 读出任务标签逗号串."""
        tasks, error_flag = self._instance.get_torrents(hashs)
        if error_flag:
            raise _ReadError("get_torrents 返回错误标志 True")
        wanted = {item.lower() for item in hashs}
        tags: dict[str, list[str]] = {}
        for task in tasks or ():
            task_hash = _text(_field_value(task, "hash"))
            if not task_hash or task_hash.lower() not in wanted:
                continue
            tags[task_hash] = _split_tags(_field_value(task, "tags"))
        return tags

    def _read_transmission_tags(self, hashs: list[str]) -> dict[str, list[str]]:
        """逐 hash 读取 Transmission 标签（宿主只返回首个匹配任务的标签）."""
        tags: dict[str, list[str]] = {}
        for item in hashs:
            tags[item] = _require_tag_list(self._instance.get_torrent_tags([item]), "get_torrent_tags")
        return tags

    def _read_rtorrent_tags(self, hashs: list[str]) -> dict[str, list[str]]:
        """逐 hash 读取 rTorrent 标签（``get_torrent_tags`` 只接受单个 hash）."""
        return {item: _split_tags(self._instance.get_torrent_tags(item)) for item in hashs}

    def add_tags(self, hashs: Sequence[str], tags: Sequence[str]) -> WriterResult:
        """以追加/合并语义写入标签，空 hash 或空标签直接跳过且不调用下载器."""
        wanted = _clean_hashs(hashs)
        wanted_tags = _clean_tags(tags)
        if not wanted or not wanted_tags:
            return WriterResult(WriterStatus.SKIPPED, "标签或任务 hash 为空，未调用下载器")
        kind = self._capabilities.kind
        if not self._capabilities.can_add_tags:
            return WriterResult(WriterStatus.UNSUPPORTED, f"{kind} 实例缺少写入标签的方法")
        return self._with_retry("写入标签", lambda: self._write_tags(wanted, wanted_tags))

    def _write_tags(self, hashs: list[str], tags: list[str]) -> WriterResult:
        """按实例类型分派一次标签写入."""
        kind = self._capabilities.kind
        if kind == "transmission":
            return self._write_transmission_tags(hashs, tags)
        if kind == "rtorrent":
            return self._write_rtorrent_tags(hashs, tags)
        return self._write_qbittorrent_tags(hashs, tags)

    def _write_qbittorrent_tags(self, hashs: list[str], tags: list[str]) -> WriterResult:
        """原生追加标签（qBittorrent 语义）, 返回统一写入结果."""
        accepted = self._instance.set_torrents_tag(hashs, tags)
        if accepted is True:
            return WriterResult(WriterStatus.OK, f"已追加 {len(tags)} 个标签")
        return WriterResult(WriterStatus.FAILED, f"set_torrents_tag 返回 {accepted!r}，未确认写入")

    def _write_rtorrent_tags(self, hashs: list[str], tags: list[str]) -> WriterResult:
        """以 overwrite=False 合并标签（rTorrent 语义）, 保留原有标签."""
        accepted = self._instance.set_torrents_tag(hashs, tags, overwrite=False)
        if accepted is True:
            return WriterResult(WriterStatus.OK, f"已合并 {len(tags)} 个标签")
        return WriterResult(WriterStatus.FAILED, f"set_torrents_tag 返回 {accepted!r}，未确认写入")

    def _write_transmission_tags(self, hashs: list[str], tags: list[str]) -> WriterResult:
        """Transmission 先读原标签再覆盖写入，读取失败时放弃本次写入.

        宿主 ``get_torrent_tags`` 的 ``[]`` 表示"任务确实没有标签"，按无原标签处理并
        以 ``org_tags=[]`` 写入；``None``、非字符串/序列结构或异常都视为读取失败，
        本次不调用 ``set_torrent_tag``，避免盲写覆盖原标签。
        """
        try:
            original = _require_tag_list(self._instance.get_torrent_tags(hashs), "get_torrent_tags")
        except Exception as err:  # 读到失败就不写，宁可记 failed 也不覆盖原标签
            return WriterResult(WriterStatus.FAILED, f"读取原标签失败，未写入以避免覆盖：{err}")
        accepted = self._instance.set_torrent_tag(hashs, tags, org_tags=list(original))
        if accepted is True:
            return WriterResult(WriterStatus.OK, f"已写入 {len(tags)} 个标签（保留原标签 {len(original)} 个）")
        return WriterResult(WriterStatus.FAILED, f"set_torrent_tag 返回 {accepted!r}，未确认写入")

    def set_category(self, hash_value: str, category: str) -> WriterResult:
        """写入任务分类，空分类或空 hash 直接跳过（避免清空 qBittorrent 分类）."""
        key = _text(hash_value)
        text = _text(category)
        if not text:
            return WriterResult(WriterStatus.SKIPPED, "分类为空，未调用分类写入以避免清空任务分类")
        if not key:
            return WriterResult(WriterStatus.SKIPPED, "任务 hash 为空，未调用分类写入")
        kind = self._capabilities.kind
        if not self._capabilities.can_set_category:
            return WriterResult(WriterStatus.UNSUPPORTED, f"{kind} 不支持分类写入，仅标签可用")
        return self._with_retry("写入分类", lambda: self._write_category(key, text))

    def _write_category(self, hash_value: str, category: str) -> WriterResult:
        """调用实例的分类写入方法并判定结果."""
        accepted = self._instance.set_torrent_category(hash_value, category)
        if accepted is True:
            return WriterResult(WriterStatus.OK, f"已写入分类 {category}")
        return WriterResult(WriterStatus.FAILED, f"set_torrent_category 返回 {accepted!r}，未确认写入")


class _ChainWriter(_RetryWriter):
    """服务实例不可用时的回退写入适配（``DownloadChain`` + 下载器模块）."""

    def __init__(
        self,
        chain: Any,
        downloader_name: str | None = None,
        *,
        sleep: SleepFunc | None = None,
        retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS,
    ) -> None:
        """保存回退通道与下载器名称，``chain`` 为 None 表示通道本身不可用."""
        super().__init__(sleep=sleep, retry_delays=retry_delays)
        self._chain = chain
        self._downloader = downloader_name or None

    @property
    def capabilities(self) -> WriterCapabilities:
        """回退通道只确知标签写入可用，读取与分类能力未探测."""
        return WriterCapabilities(kind="chain", can_read_tags=False, can_add_tags=True, can_set_category=False)

    @property
    def supports_category(self) -> bool | None:
        """类型未知时不宣称支持分类，返回 None 表示需按 ``set_category`` 实际结果判断."""
        return None

    def read_tags(self, hashs: Sequence[str]) -> dict[str, list[str]]:
        """回退通道没有官方读取入口，返回空字典."""
        return {}

    def read_tags_ex(self, hashs: Sequence[str]) -> TagReadResult:
        """回退通道没有官方读取入口，返回 unsupported + 原因而不猜 API."""
        return TagReadResult({}, WriterResult(WriterStatus.UNSUPPORTED, "回退通道没有官方标签读取接口"))

    def add_tags(self, hashs: Sequence[str], tags: Sequence[str]) -> WriterResult:
        """经 ``DownloadChain.set_torrents_tag`` 写标签，只有返回 True 才算成功."""
        wanted = _clean_hashs(hashs)
        wanted_tags = _clean_tags(tags)
        if not wanted or not wanted_tags:
            return WriterResult(WriterStatus.SKIPPED, "标签或任务 hash 为空，未调用下载器")
        if self._chain is None:
            return WriterResult(WriterStatus.FAILED, "回退通道不可用，未写入标签")
        return self._with_retry("写入标签", lambda: self._write_tags(wanted, wanted_tags))

    def _write_tags(self, hashs: list[str], tags: list[str]) -> WriterResult:
        """调用回退通道写标签并判定结果，``None``/``False`` 都不算成功."""
        accepted = self._chain.set_torrents_tag(hashs, tags, downloader=self._downloader)
        if accepted is True:
            return WriterResult(WriterStatus.OK, f"回退通道已写入 {len(tags)} 个标签")
        return WriterResult(WriterStatus.FAILED, f"set_torrents_tag 返回 {accepted!r}，未确认为成功")

    def set_category(self, hash_value: str, category: str) -> WriterResult:
        """经 ``DownloadChain.update_torrent(category=)`` 写分类，空分类直接跳过."""
        key = _text(hash_value)
        text = _text(category)
        if not text:
            return WriterResult(WriterStatus.SKIPPED, "分类为空，未调用分类写入以避免清空任务分类")
        if not key:
            return WriterResult(WriterStatus.SKIPPED, "任务 hash 为空，未调用分类写入")
        if self._chain is None:
            return WriterResult(WriterStatus.FAILED, "回退通道不可用，未写入分类")
        return self._with_retry("写入分类", lambda: self._write_category(key, text))

    def _write_category(self, hash_value: str, category: str) -> WriterResult:
        """解析 ``update_torrent`` 的返回结构，缺键或非布尔值都记失败而不是成功."""
        result = self._chain.update_torrent(hash_value, downloader=self._downloader, category=category)
        if result is None:
            return WriterResult(WriterStatus.FAILED, "update_torrent 返回 None，无法确认分类写入")
        if not isinstance(result, Mapping):
            return WriterResult(WriterStatus.FAILED, f"update_torrent 返回 {type(result).__name__}，结构无法识别")
        if "category" not in result:
            return WriterResult(WriterStatus.FAILED, "update_torrent 返回缺少 category 键，无法确认分类写入")
        value = result["category"]
        if value is True:
            return WriterResult(WriterStatus.OK, f"回退通道已写入分类 {category}")
        if value is False:
            return WriterResult(WriterStatus.UNSUPPORTED, "下载器报告不支持分类写入，仅标签可用")
        return WriterResult(WriterStatus.FAILED, f"category 结果 {value!r} 不是布尔值，未确认写入")


def _probe_capabilities(instance: Any, type_name: str) -> WriterCapabilities:
    """按实例方法是否存在与类型白名单探测写入能力，仅 qBittorrent 具备分类写入."""
    readers = {
        "qbittorrent": "get_torrents",
        "transmission": "get_torrent_tags",
        "rtorrent": "get_torrent_tags",
    }
    writers = {
        "qbittorrent": "set_torrents_tag",
        "transmission": "set_torrent_tag",
        "rtorrent": "set_torrents_tag",
    }
    reader = readers.get(type_name)
    writer_name = writers.get(type_name)
    if reader is None or writer_name is None:
        return WriterCapabilities(
            kind=type_name or "unknown", can_read_tags=False, can_add_tags=False, can_set_category=False
        )
    return WriterCapabilities(
        kind=type_name,
        can_read_tags=callable(getattr(instance, reader, None)),
        can_add_tags=callable(getattr(instance, writer_name, None)),
        can_set_category=type_name == "qbittorrent" and callable(getattr(instance, "set_torrent_category", None)),
    )


# 能力探测的进程内缓存：键包含实例标识，不同实例不共享结果；不落盘
_CAPABILITY_CACHE: dict[tuple[str, str, int], WriterCapabilities] = {}


def clear_capability_cache() -> None:
    """清空进程内能力探测缓存，测试可直接调用以隔离用例."""
    _CAPABILITY_CACHE.clear()


def capability_cache_size() -> int:
    """返回当前进程内能力缓存条目数，用于确认缓存未落盘且可清理."""
    return len(_CAPABILITY_CACHE)


def _cached_capabilities(downloader_name: str | None, instance: Any, type_name: str) -> WriterCapabilities:
    """按名称 + 类型 + 实例标识取缓存，未命中时探测一次并写入缓存."""
    key = (downloader_name or "", type_name, id(instance))
    cached = _CAPABILITY_CACHE.get(key)
    if cached is not None:
        return cached
    capabilities = _probe_capabilities(instance, type_name)
    _CAPABILITY_CACHE[key] = capabilities
    return capabilities


def _default_helper() -> Any:
    """构造宿主下载器服务发现入口，延迟导入避免模块级强依赖宿主."""
    from app.sdk.services import DownloaderHelper

    return DownloaderHelper()


def _default_chain() -> Any:
    """构造回退通道 ``DownloadChain``（不在 ``app.sdk`` 导出，属已评审的例外依赖）."""
    from app.chain.download import DownloadChain

    return DownloadChain()


def _query_service(downloader_name: str | None, helper_factory: Callable[[], Any] | None) -> Any:
    """按名称查询宿主服务实例，查询失败或无名称时返回 None 交给回退通道."""
    if not downloader_name:
        return None
    factory = helper_factory or _default_helper
    try:
        return factory().get_service(downloader_name)
    except Exception as err:  # 服务目录未装配等异常按"无实例"处理，不影响回退
        logger.warning(f"[订阅下载分类与标签] 下载器服务查询失败，改用回退通道：{err}")
        return None


def _build_chain(chain_factory: Callable[[], Any] | None) -> Any:
    """构造回退通道实例，构造失败时返回 None 由适配层记 failed."""
    factory = chain_factory or _default_chain
    try:
        return factory()
    except Exception as err:  # 回退通道不可用时只降级，不向外抛异常
        logger.warning(f"[订阅下载分类与标签] 回退通道构造失败，本次写入将记失败：{err}")
        return None


def resolve_writer(
    downloader_name: str | None,
    *,
    helper_factory: Callable[[], Any] | None = None,
    chain_factory: Callable[[], Any] | None = None,
    sleep: SleepFunc | None = None,
    retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS,
) -> TorrentAttrWriter:
    """按下载器名称解析写入适配，实例不可用或类型不支持时回退 ``DownloadChain``.

    ``helper_factory`` / ``chain_factory`` 默认走真实宿主入口，仅供测试注入伪对象；
    ``sleep`` / ``retry_delays`` 用于注入重试节奏，默认两次重试共三次尝试。
    """
    info = _query_service(downloader_name, helper_factory)
    instance = getattr(info, "instance", None) if info is not None else None
    type_name = _text(getattr(info, "type", None)).lower() if info is not None else ""
    if instance is not None and type_name in SUPPORTED_TYPES:
        capabilities = _cached_capabilities(downloader_name, instance, type_name)
        return _InstanceWriter(instance, capabilities, sleep=sleep, retry_delays=retry_delays)
    return _ChainWriter(_build_chain(chain_factory), downloader_name, sleep=sleep, retry_delays=retry_delays)
