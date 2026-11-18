"""订阅自动下载分类与标签插件（V3 实现）.

订阅触发自动下载时，按订阅目录规则为下载任务写入下载器的分类与标签。

已实现 ``EventType.DownloadAdded`` 事件处理：开关与来源过滤、媒体守卫、订阅保存路径
重定位与按媒体匹配回退、规则命中、``dry_run`` 分流、能力探测式写入与异常隔离；同时
包含纯逻辑规则层（``rules.py``）、宿主只读适配、下载器写入适配（``writer.py``）、
处理记录与 7 天去重表的插件数据持久化、按宿主实时目录生成的配置页，以及只读处理
记录的详情页。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

# 例外依赖（已评审接受，不在 app.sdk 稳定面）：目录匹配与订阅自身的只读保存路径
from app.application.directory import DirectoryHelper
from app.db.oper.subscribe import SubscribeOper
from app.schemas.system import TransferDirectoryConf
from app.schemas.types import EventType, SystemConfigKey
from app.sdk.config import settings
from app.sdk.events import eventmanager
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

from .rules import (
    DEFAULT_SYSTEM_TAG,
    directory_key,
    match_rule,
    merge_tags,
    normalize_config,
    resolve_directory_keys,
)
from .writer import TagReadResult, TorrentAttrWriter, WriterResult, WriterStatus, resolve_writer

# 插件数据键：处理记录与 7 天去重表；只经基类 save_data/get_data 持久化
RECORDS_KEY = "records"
SEEN_KEY = "seen"
# 处理记录条数上限：源码常量，不放进配置、不暴露给 UI；测试可注入更小值
MAX_RECORDS = 200
# 去重与记录保留窗口
SEEN_TTL = timedelta(days=7)

# 记录状态：dry_run 为独立状态，其余与 writer.WriterStatus 同名
STATUS_DRY_RUN = "dry_run"

# 详情页状态展示：记录里的原始 status 保持不变, 页面按该映射显示标签与颜色
PAGE_STATUS_DISPLAY: dict[str, dict[str, Any]] = {
    WriterStatus.OK.value: {
        "label": "成功",
        "color": "success",
        "success": True,
        "hint": "分类与标签已按规则写入下载器",
    },
    WriterStatus.UNSUPPORTED.value: {
        "label": "部分不支持（非成功）",
        "color": "warning",
        "success": False,
        "hint": "下载器不支持对应能力（如分类写入），未完整写入",
    },
    WriterStatus.FAILED.value: {
        "label": "失败",
        "color": "error",
        "success": False,
        "hint": "写入失败，未登记去重，可重试",
    },
    WriterStatus.SKIPPED.value: {
        "label": "已跳过",
        "color": "grey",
        "success": False,
        "hint": "未命中规则或未写入下载器",
    },
    STATUS_DRY_RUN: {
        "label": "仅记录未写入（非成功）",
        "color": "info",
        "success": False,
        "hint": "dry_run：只记录拟写入的分类与标签，未调用下载器",
    },
}
# 详情页状态分布标签的固定顺序：成功在前, dry_run 在最后
PAGE_STATUS_ORDER = (
    WriterStatus.OK.value,
    WriterStatus.UNSUPPORTED.value,
    WriterStatus.FAILED.value,
    WriterStatus.SKIPPED.value,
    STATUS_DRY_RUN,
)
# 详情页表格列：与记录九个字段一一对应, 顺序与持久化结构一致
PAGE_HEADERS = (
    {"title": "时间", "key": "time"},
    {"title": "任务 Hash", "key": "hash"},
    {"title": "标题", "key": "title"},
    {"title": "目录键", "key": "directory_key"},
    {"title": "规则层级", "key": "level"},
    {"title": "分类", "key": "category"},
    {"title": "标签", "key": "tags"},
    {"title": "状态", "key": "status"},
    {"title": "原因", "key": "reason"},
)

# 目录规则的模型字段路径前缀：动态行按目录键绑定, 键本身始终取自 rules 生成的目录键
DIRECTORY_RULE_PREFIX = "directory_rules"
DEFAULT_RULE_PREFIX = "default_rule"

# 订阅来源前缀：下载器适配层把订阅信息以 ``Subscribe|{json}`` 形式写进 source
SUBSCRIPTION_SOURCE_PREFIX = "Subscribe|"


def _form_component(component: str, props: Mapping[str, Any]) -> dict[str, Any]:
    """构造一个不带子节点的表单组件描述, 只复制 props 便于 json 序列化."""
    return {"component": component, "props": dict(props)}


def _form_col(
    component: str,
    props: Mapping[str, Any],
    *,
    cols: int = 12,
    md: int = 4,
) -> dict[str, Any]:
    """构造 VCol 包裹的单组件列, 统一栅格参数."""
    return {
        "component": "VCol",
        "props": {"cols": cols, "md": md},
        "content": [_form_component(component, props)],
    }


def _utc_now() -> datetime:
    """返回带时区的当前时间, 作为默认可注入时间源."""
    return datetime.now(UTC)


def _as_aware(value: datetime) -> datetime:
    """把时间统一为带时区值, 无时区输入按 UTC 处理."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _parse_time(value: Any) -> datetime | None:
    """把 ISO 字符串、时间戳或 datetime 解析为带时区时间, 无法识别时返回 None."""
    if isinstance(value, datetime):
        return _as_aware(value)
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(float(value), UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return _as_aware(datetime.fromisoformat(text.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _text(value: Any) -> str:
    """把任意值转成去首尾空白的 str, None 返回空串."""
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else str(value).strip()


def _status_display(status: Any) -> dict[str, Any]:
    """返回记录状态的只读展示标签与颜色, 未识别状态按中性样式展示."""
    text = _text(status)
    display = PAGE_STATUS_DISPLAY.get(text)
    if display is None:
        return {"label": text or "未知", "color": "grey", "success": False, "hint": "未识别的状态值"}
    return dict(display)


class SubscribeDownloadAttr(_PluginBase):
    """按订阅目录规则为自动下载任务写入下载器分类与标签."""

    # --- 插件元数据（前端/市场使用） ---
    # 在前端展示的插件名称
    plugin_name = "订阅下载分类与标签"
    # 插件描述
    plugin_desc = "订阅自动下载时按目录规则为下载任务写入下载器分类与标签"
    # 插件图标文件名（位于仓库 icons/ 目录）
    plugin_icon = "directory.png"
    # 插件版本（应与 package.v3.json 保持一致）
    plugin_version = "0.1.0"
    # 作者信息
    plugin_author = "cswhrdf"
    # 配置项前缀，用于在宿主配置中区分本插件
    plugin_config_prefix = "subscribedownloadattr_"
    # 插件加载顺序，值越小越早加载
    plugin_order = 51
    # 可使用该插件的用户级别
    auth_level = 1

    # 运行时启用开关，init_plugin 会根据配置覆盖；类级默认值保证不经过 __init__
    # 直接构造实例（如单测）时读取状态与停止服务也是安全的
    _enabled: bool = False
    # 规范化后的配置快照与规范化提示；同为零副作用的最小运行态
    _config: dict[str, Any] | None = None
    _config_issues: tuple[str, ...] = ()
    # 宿主只读适配结果：校验后的目录快照、目录键结构、降级提示与系统标签
    _directories: tuple[dict[str, Any], ...] = ()
    _directory_keys: dict[str, Any] | None = None
    _host_issues: tuple[str, ...] = ()
    _system_tag: str = DEFAULT_SYSTEM_TAG
    # 事件处理运行态：可注入时间源、可注入记录上限、冲突告警去重与持久化内存兜底
    _clock: Callable[[], datetime] = staticmethod(_utc_now)
    _max_records: int = MAX_RECORDS
    _conflicts_warned: bool = False
    _records_cache: list[dict[str, Any]] | None = None
    _seen_cache: dict[str, str] | None = None
    _reporting_failure: bool = False

    def init_plugin(self, config: dict[str, Any] | None = None) -> None:
        """初始化插件：复位运行状态，规范化配置并实时读取宿主只读数据.

        Args:
            config: 插件配置字典；None 按空配置处理，非对象类型回退默认值并记提示。

        """
        self.stop_service()

        normalized, issues = normalize_config(config)
        self._config = normalized
        self._config_issues = tuple(issues)
        self._enabled = bool(normalized["enabled"])
        self._conflicts_warned = False
        self._records_cache = None
        self._seen_cache = None

        # 每次初始化都实时读取宿主目录与系统标签，不做持久化缓存
        self._system_tag, tag_issues = self._load_system_tag()
        directories, keys, directory_issues = self._load_directories()
        self._directories = directories
        self._directory_keys = keys
        self._host_issues = tuple(tag_issues + directory_issues)
        for issue in self._host_issues:
            logger.warning(f"[{self.plugin_name}] {issue}")
        logger.info(
            f"[{self.plugin_name}] 初始化完成：enabled={self._enabled}，目录 {len(directories)} 条，"
            f"有效规则键 {len((keys or {}).get('effective_keys') or ())} 个"
        )
        logger.debug(f"[{self.plugin_name}] 规范化配置：{normalized}")

    @property
    def config_issues(self) -> tuple[str, ...]:
        """返回最近一次配置规范化产生的提示, 供配置页展示.

        Returns:
            不可变的提示元组；为空表示输入完全可用。

        """
        return self._config_issues

    @property
    def directory_entries(self) -> tuple[dict[str, Any], ...]:
        """返回最近一次初始化校验通过的目录条目, 已按宿主优先级升序排列.

        Returns:
            普通 dict 元组；宿主读取失败或目录不可用时为空元组。

        """
        return self._directories

    @property
    def directory_keys(self) -> dict[str, Any] | None:
        """返回最近一次初始化生成的目录键结构, 无可用目录时为 None.

        Returns:
            ``rules.resolve_directory_keys`` 产出的普通结构（条目/有效键/冲突）。

        """
        return self._directory_keys

    @property
    def host_issues(self) -> tuple[str, ...]:
        """返回最近一次初始化产生的宿主只读降级提示."""
        return self._host_issues

    @property
    def system_tag(self) -> str:
        """返回运行时系统标签, 缺失或为空时回退 ``rules.DEFAULT_SYSTEM_TAG``."""
        return self._system_tag

    # ---------------- 宿主只读适配 ----------------

    def _load_system_tag(self) -> tuple[str, list[str]]:
        """读取宿主运行时系统标签, 空或缺失时回退默认值并给出提示.

        Returns:
            ``(标签, 提示列表)``；标签始终是普通 str，供后续标签合并注入。

        """
        try:
            value = settings.TORRENT_TAG
        except AttributeError:  # 宿主未提供该设置时按缺失降级
            value = None
        text = value.strip() if isinstance(value, str) else ""
        if text:
            return text, []
        return DEFAULT_SYSTEM_TAG, [
            f"宿主系统标签 TORRENT_TAG 缺失或为空，已回退默认值 {DEFAULT_SYSTEM_TAG}",
        ]

    def _read_directory_config(self) -> tuple[Any, list[str]]:
        """调用宿主系统配置读取原始目录列表, 失败时降级为 None 并记录原因.

        Returns:
            ``(原始值, 提示列表)``；宿主未注入读取器或读取抛错时原始值为 None。

        """
        oper = getattr(self, "systemconfig", None)
        if oper is None:
            return None, ["宿主未注入系统配置读取器，目录规则键按空处理"]
        try:
            return oper.get(SystemConfigKey.Directories), []
        except Exception as err:  # 宿主读取失败不得打断插件初始化
            return None, [f"读取宿主目录配置失败，目录规则键按空处理：{err}"]

    def _load_directories(
        self,
    ) -> tuple[tuple[dict[str, Any], ...], dict[str, Any] | None, list[str]]:
        """实时读取并校验宿主目录配置, 生成目录键内存结构.

        Returns:
            ``(目录条目, 目录键结构, 提示列表)``；空配置、非列表、无可用下载目录
            时返回空元组与 None，坏条目跳过但保留有效项，任何情况都不向上抛异常.

        """
        raw, issues = self._read_directory_config()
        if raw is None:
            if not issues:
                issues.append("宿主目录配置为空，目录规则键按空处理")
            return (), None, issues
        if not isinstance(raw, list):
            issues.append(f"宿主目录配置应为列表，实际为 {type(raw).__name__}，目录规则键按空处理")
            return (), None, issues

        entries: list[dict[str, Any]] = []
        for index, item in enumerate(raw, 1):
            if not isinstance(item, Mapping):
                issues.append(f"宿主第 {index} 条目录配置不是对象，已跳过")
                continue
            try:
                data = TransferDirectoryConf(**dict(item)).model_dump()
            except Exception as err:  # 单条坏配置只跳过该项
                issues.append(f"宿主第 {index} 条目录配置校验失败，已跳过：{err}")
                continue
            if not data.get("download_path"):
                # 与宿主 get_download_dirs 同一定义域：无下载目录不参与键与序号
                continue
            entries.append(data)

        if not entries:
            issues.append(f"宿主目录配置无可用下载目录（共 {len(raw)} 条），目录规则键按空处理")
            return (), None, issues

        # 与宿主 get_download_dirs 同序：priority or 0 升序，同优先级保持配置顺序
        entries.sort(key=lambda entry: entry.get("priority") or 0)
        return tuple(entries), resolve_directory_keys(entries), issues

    @staticmethod
    def _attribute_text(source: Any, name: str) -> str:
        """按对象属性读取文本字段, 枚举取 value, 缺失与空值统一返回空串."""
        value = getattr(source, name, None)
        if isinstance(value, Enum):
            value = value.value
        if value is None:
            return ""
        return value.strip() if isinstance(value, str) else str(value).strip()

    @classmethod
    def _media_snapshot(cls, media_info: Any) -> dict[str, Any] | None:
        """把事件媒体对象转成普通快照, 无媒体或类型为空时返回 None.

        Returns:
            ``{media_type, media_category, title, year}`` 普通 dict；四个字段都是
            str，缺失字段为空串。``media_type`` 为空表示无媒体，调用方须跳过目录解析。

        """
        if media_info is None:
            return None
        media_type = cls._attribute_text(media_info, "type")
        if not media_type:
            return None
        return {
            "media_type": media_type,
            "media_category": cls._attribute_text(media_info, "category"),
            "title": cls._attribute_text(media_info, "title"),
            "year": cls._attribute_text(media_info, "year"),
        }

    @staticmethod
    def _directory_plain(directory: Any) -> dict[str, Any] | None:
        """把宿主目录对象转成用于键定位的普通字段字典, 无法识别时返回 None."""
        if directory is None:
            return None
        if isinstance(directory, Mapping):
            return dict(directory)
        dump = getattr(directory, "model_dump", None)
        if callable(dump):
            data = dump()
            return dict(data) if isinstance(data, Mapping) else None
        fields = ("name", "storage", "download_path", "priority")
        if not any(hasattr(directory, field) for field in fields):
            return None
        return {field: getattr(directory, field, None) for field in fields}

    def _locate_directory_entry(self, directory: Any) -> dict[str, Any] | None:
        """在最近一次校验通过的目录序列中定位宿主命中的目录条目.

        Returns:
            命中的目录键条目（同一条 ``resolve_directory_keys`` 序列、首条有效项）；
            未命中或当前没有目录快照时返回 None，不自行拼键或重排。

        """
        keys = self._directory_keys
        if not keys:
            return None
        plain = self._directory_plain(directory)
        if plain is None:
            return None
        for entry in keys.get("entries") or ():
            if directory_key(plain, entry["sequence"]) == entry["key"]:
                return dict(entry)
        return None

    def _resolve_directory_entry(self, media_info: Any) -> dict[str, Any] | None:
        """按媒体信息定位宿主实际选中的下载目录条目.

        守卫在前：媒体缺失或 ``type`` 为空时直接返回 None，不调用宿主
        ``get_download_dir``（宿主对空媒体返回“匹配”，会误取首个目录）。

        Returns:
            命中的目录键条目；无媒体、未命中或宿主抛错时返回 None。

        """
        if self._media_snapshot(media_info) is None:
            return None
        try:
            matched = DirectoryHelper().get_download_dir(media_info)
        except Exception as err:  # 宿主匹配异常只降级为未命中
            logger.warning(f"[{self.plugin_name}] 宿主目录匹配失败，本次按未命中处理：{err}")
            return None
        return self._locate_directory_entry(matched)

    # ---------------- DownloadAdded 事件处理 ----------------

    @eventmanager.register(EventType.DownloadAdded)
    def on_download_added(self, event: Any) -> None:
        """处理 DownloadAdded 事件：按目录规则写入分类与标签并记录结果.

        处理器自身吞掉全部异常，任何时候都不向宿主事件总线抛出，避免打断订阅与下载。
        """
        logger.debug(f"[{self.plugin_name}] 收到 DownloadAdded 事件")
        try:
            self._handle_download_added(event)
        except Exception as err:  # 插件异常绝不打断宿主订阅与下载
            logger.error(f"[{self.plugin_name}] 处理 DownloadAdded 事件失败：{err}")
            self._record_exception_failure(event, err)

    def _handle_download_added(self, event: Any) -> None:
        """执行 DownloadAdded 的判定与写入流程, 每一步都产生可展示的记录."""
        if not self._enabled:
            logger.debug(f"[{self.plugin_name}] 插件未启用，忽略 DownloadAdded 事件")
            return

        now = self._now()
        payload = getattr(event, "event_data", None)
        prepared = self._preflight_event(payload, now)
        if prepared is None:
            return
        hash_value, title, context, media_info, config = prepared
        logger.info(f"[{self.plugin_name}] 处理下载任务：hash={hash_value}，标题={title}")

        self._warn_directory_conflicts()
        source = payload.get("source") if isinstance(payload, Mapping) else None
        entry, reason = self._resolve_event_directory(media_info, context, source)
        if entry is None:
            logger.debug(f"[{self.plugin_name}] 目录未命中：{reason or '未找到可用的下载目录'}")
            self._record_skip(
                now=now,
                hash_value=hash_value,
                title=title,
                reason=reason or "未找到可用的下载目录，跳过写入",
            )
            return

        key = entry["key"]
        logger.debug(f"[{self.plugin_name}] 目录解析命中：key={key}")
        self._apply_match(
            now=now,
            payload=payload,
            hash_value=hash_value,
            title=title,
            directory_key=key,
            config=config,
            match=match_rule(config, directory_key=key),
        )

    def _apply_match(
        self,
        *,
        now: datetime,
        payload: Any,
        hash_value: str,
        title: str,
        directory_key: str,
        config: Mapping[str, Any],
        match: Mapping[str, Any],
    ) -> None:
        """按同一份匹配结果决定跳过、``dry_run`` 或实际写入, 并写一条记录.

        ``dry_run`` 与实际写入都用调用方传入的同一个 ``match``，保证记录与拟写入值一致；
        分类为空时不调用分类写入，标签为空时既不读也不写标签。
        """
        rule = match["rule"]
        level = match["level"]
        category = rule["category"] if config["apply_category"] else ""
        tags = list(rule["tags"]) if config["apply_tags"] else []
        if not match["matched"]:
            logger.debug(
                f"[{self.plugin_name}] 任务 {hash_value} 未命中有效规则（层级 {level} 无分类与标签），跳过写入"
            )
            self._record_skip(
                now=now,
                hash_value=hash_value,
                title=title,
                directory_key=directory_key,
                level=level,
                category=category,
                tags=tags,
                reason=f"命中层级 {level} 无有效规则：分类与标签均为空",
            )
            return
        if not category and not tags:
            logger.debug(f"[{self.plugin_name}] 任务 {hash_value} 分类与标签写入均被配置关闭，跳过写入")
            self._record_skip(
                now=now,
                hash_value=hash_value,
                title=title,
                directory_key=directory_key,
                level=level,
                reason="分类与标签写入均被配置关闭，无可写入项",
            )
            return
        if config["dry_run"]:
            logger.info(
                f"[{self.plugin_name}] 任务 {hash_value} dry_run：拟写入分类 {category or '-'}、"
                f"标签 {tags or '-'}，未调用下载器"
            )
            self._record(
                now=now,
                hash_value=hash_value,
                title=title,
                directory_key=directory_key,
                level=level,
                category=category,
                tags=tags,
                status=STATUS_DRY_RUN,
                reason=f"dry_run：将写入分类 {category or '-'}、标签 {tags or '-'}，未调用下载器",
                mark_seen=True,
            )
            return

        downloader = payload.get("downloader") if isinstance(payload, Mapping) else None
        writer = self._writer_for(downloader)
        results, notes = self._apply_writes(writer, hash_value, category, tags, config)
        status = self._aggregate_status(results)
        reason_text = "；".join(note for note in notes if note)
        if status == WriterStatus.FAILED.value:
            logger.error(f"[{self.plugin_name}] 任务 {hash_value} 写入失败：{reason_text}")
        else:
            logger.info(
                f"[{self.plugin_name}] 任务 {hash_value} 写入完成：status={status}，"
                f"分类={category or '-'}，标签={tags or '-'}"
            )
        self._record(
            now=now,
            hash_value=hash_value,
            title=title,
            directory_key=directory_key,
            level=level,
            category=category,
            tags=tags,
            status=status,
            reason=reason_text,
            mark_seen=status != WriterStatus.FAILED.value,
        )

    def _preflight_event(
        self,
        payload: Any,
        now: datetime,
    ) -> tuple[str, str, Any, Any, dict[str, Any]] | None:
        """执行 payload、hash、来源、媒体与去重的前置判定, 需跳过时写记录并返回 None."""
        if not isinstance(payload, Mapping):
            logger.debug(f"[{self.plugin_name}] 跳过事件：payload 不是对象")
            self._record_skip(now=now, hash_value="", title="", reason="事件 payload 不是对象，已跳过")
            return None
        hash_value = _text(payload.get("hash"))
        if not hash_value:
            logger.debug(f"[{self.plugin_name}] 跳过事件：payload 缺少任务 hash")
            self._record_skip(now=now, hash_value="", title="", reason="事件缺少任务 hash，已跳过")
            return None
        config = self._config_snapshot()
        if config["trigger"] == "subscribe" and not _text(payload.get("source")).startswith(SUBSCRIPTION_SOURCE_PREFIX):
            logger.debug(f"[{self.plugin_name}] 跳过任务 {hash_value}：非订阅来源")
            self._record_skip(
                now=now,
                hash_value=hash_value,
                title="",
                reason="非订阅来源，trigger=subscribe 仅处理订阅下载",
            )
            return None
        context = payload.get("context")
        media_info = self._context_value(context, "media_info")
        snapshot = self._media_snapshot(media_info)
        if snapshot is None:
            logger.debug(f"[{self.plugin_name}] 跳过任务 {hash_value}：媒体信息缺失或类型为空")
            self._record_skip(
                now=now,
                hash_value=hash_value,
                title="",
                reason="媒体信息缺失或类型为空，跳过目录解析与写入",
            )
            return None
        title = _text(snapshot.get("title"))
        if self._is_seen(hash_value, now):
            logger.debug(f"[{self.plugin_name}] 跳过任务 {hash_value}：7 天内已处理，本次不重复写入")
            self._record_skip(
                now=now,
                hash_value=hash_value,
                title=title,
                reason="同一 hash 在 7 天内已处理，本次不重复写入",
            )
            return None
        return hash_value, title, context, media_info, config

    def _record_skip(
        self,
        *,
        now: datetime,
        hash_value: str,
        title: str,
        reason: str,
        directory_key: str = "",
        level: str = "",
        category: str = "",
        tags: list[str] | None = None,
    ) -> None:
        """写入一条 skipped 记录, 统一空值字段."""
        self._record(
            now=now,
            hash_value=hash_value,
            title=title,
            directory_key=directory_key,
            level=level,
            category=category,
            tags=list(tags or []),
            status=WriterStatus.SKIPPED.value,
            reason=reason,
        )

    def _writer_for(self, downloader: Any) -> TorrentAttrWriter:
        """解析下载器写入适配, 生产走 ``writer.resolve_writer``, 测试可替换该外部入口."""
        return resolve_writer(downloader)

    def _apply_writes(
        self,
        writer: TorrentAttrWriter,
        hash_value: str,
        category: str,
        tags: list[str],
        config: Mapping[str, Any],
    ) -> tuple[list[WriterResult], list[str]]:
        """按配置写入标签与分类, 返回每步结果与可读原因.

        标签先读原标签再合并（``read_tags_ex``）：读取失败时放弃本次标签写入，避免
        Transmission 覆盖语义丢失原标签；读取 ``unsupported`` 时按追加语义继续写入，
        但结果保留 ``unsupported`` 与原因，不伪装成 ``ok``。分类仅在能力探测未明确
        返回 ``False`` 时调用，``None`` 按实际写入结果判断。
        """
        results: list[WriterResult] = []
        notes: list[str] = []
        if tags:
            try:
                read = writer.read_tags_ex([hash_value])
            except Exception as err:  # 读取异常与读取失败同义：不盲写
                read = TagReadResult({}, WriterResult(WriterStatus.FAILED, f"读取任务标签抛异常：{err}"))
            logger.debug(
                f"[{self.plugin_name}] 读取任务 {hash_value} 原标签：status={read.result.status.value}，"
                f"tags={read.tags}，reason={read.result.reason or '-'}"
            )
            if read.result.status is WriterStatus.FAILED:
                results.append(read.result)
                notes.append(read.result.reason)
            elif read.result.status is WriterStatus.OK:
                base = self._lookup_tags(read.tags, hash_value)
                merged = merge_tags(
                    base,
                    tags,
                    bool(config["keep_system_tag"]),
                    system_tag=self.system_tag,
                )
                result = self._call_writer(writer.add_tags, [hash_value], merged)
                logger.debug(
                    f"[{self.plugin_name}] 调用 add_tags：hash={hash_value}，原标签={base}，"
                    f"合并后={merged}，结果={result.describe()}"
                )
                results.append(result)
                notes.append(result.reason)
            else:
                result = self._call_writer(writer.add_tags, [hash_value], tags)
                logger.debug(
                    f"[{self.plugin_name}] 原标签不可读（{read.result.reason}），调用 add_tags："
                    f"hash={hash_value}，标签={tags}，结果={result.describe()}"
                )
                if result.ok:
                    result = WriterResult(
                        WriterStatus.UNSUPPORTED,
                        f"读取原标签不可用（{read.result.reason}），已按追加语义写入规则标签",
                        result.attempts,
                    )
                else:
                    result = WriterResult(
                        result.status,
                        f"读取原标签不可用（{read.result.reason}）；{result.reason}",
                        result.attempts,
                    )
                results.append(result)
                notes.append(result.reason)
        if category:
            # 能力探测确定为不支持时不调用分类写入，但仍记 unsupported 供详情页展示；
            # 回退通道返回 None（类型未知）时照常调用，以实际结果为准。
            supported = getattr(writer, "supports_category", None)
            if supported is False:
                result = WriterResult(
                    WriterStatus.UNSUPPORTED,
                    f"下载器不支持分类写入，已跳过分类 {category}",
                )
            else:
                result = self._call_writer(writer.set_category, hash_value, category)
            logger.debug(
                f"[{self.plugin_name}] 分类写入：supports_category={supported}，hash={hash_value}，"
                f"分类={category}，结果={result.describe()}"
            )
            results.append(result)
            notes.append(result.reason)
        return results, notes

    @staticmethod
    def _call_writer(action: Callable[..., WriterResult], *args: Any) -> WriterResult:
        """调用 writer 动作并收敛异常, 保证单步失败不影响其余步骤."""
        try:
            result = action(*args)
        except Exception as err:  # 写入异常只收敛为 failed，不打断事件
            return WriterResult(WriterStatus.FAILED, f"写入调用抛异常：{err}")
        if not isinstance(result, WriterResult):
            return WriterResult(WriterStatus.FAILED, f"写入返回 {type(result).__name__}，不符合统一结果协议")
        return result

    @staticmethod
    def _aggregate_status(results: list[WriterResult]) -> str:
        """按「任一 failed → failed；否则任一 unsupported → unsupported」汇总状态."""
        if not results:
            return WriterStatus.SKIPPED.value
        statuses = [result.status for result in results]
        if WriterStatus.FAILED in statuses:
            return WriterStatus.FAILED.value
        if WriterStatus.UNSUPPORTED in statuses:
            return WriterStatus.UNSUPPORTED.value
        if all(status is WriterStatus.SKIPPED for status in statuses):
            return WriterStatus.SKIPPED.value
        return WriterStatus.OK.value

    @staticmethod
    def _lookup_tags(tags: Mapping[str, list[str]], hash_value: str) -> list[str]:
        """按 hash 取回原标签, 大小写不一致时做一次不区分大小写兜底."""
        value = tags.get(hash_value)
        if value is None:
            lowered = hash_value.lower()
            for key, item in tags.items():
                if key.lower() == lowered:
                    value = item
                    break
        return list(value or [])

    def _resolve_event_directory(
        self,
        media_info: Any,
        context: Any,
        source: Any,
    ) -> tuple[dict[str, Any] | None, str]:
        """解析事件对应的目录条目, 订阅保存路径优先, 其余按媒体匹配回退.

        守卫在前：媒体缺失或 ``type`` 为空时直接返回 None，不调用宿主目录解析。
        订阅来源先用订阅自身的 ``save_path`` 做最长前缀重定位: 命中后即使失败也不再
        回退 ``get_download_dir``, 避免把订阅目录与同等优先级的其它目录混为一谈。
        """
        if self._media_snapshot(media_info) is None:
            return None, "媒体信息缺失或类型为空，跳过目录解析与写入"
        sub_path = self._subscription_save_path(source)
        if sub_path:
            return self._resolve_event_directory_by_task_path(media_info, sub_path)
        entry, reason = self._resolve_event_directory_by_media(media_info)
        if entry is None:
            return None, reason
        # 非订阅来源没有权威落点: 多个目录等价时无法判断真实保存路径, 跳过而不是猜
        if not _text(source).startswith(SUBSCRIPTION_SOURCE_PREFIX) and self._media_matches_multiple(media_info):
            return None, "非订阅来源且媒体匹配多个下载目录，缺少可靠保存路径，已跳过"
        return entry, ""

    def _resolve_event_directory_by_task_path(
        self,
        media_info: Any,
        task_path: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """按订阅保存路径做最长前缀重定位, 命中后不再回退按媒体匹配."""
        try:
            relocated = DirectoryHelper().get_download_dir_by_task_path(media_info, task_path)
        except Exception as err:  # 宿主最长前缀匹配异常只降级为跳过
            return None, f"订阅保存路径重定位失败：{err}"
        if relocated is None:
            return None, f"订阅保存路径 {task_path} 未匹配到任何下载目录，已跳过"
        return self._effective_event_entry(
            relocated,
            missing_reason="订阅保存路径命中的目录不在当前目录配置中",
            ineffective_reason="订阅保存路径命中的目录键 {key} 是重复键中的非生效项",
        )

    def _resolve_event_directory_by_media(self, media_info: Any) -> tuple[dict[str, Any] | None, str]:
        """按宿主媒体匹配取首个目录, 与订阅保存路径缺失时的回退语义一致."""
        try:
            matched = DirectoryHelper().get_download_dir(media_info)
        except Exception as err:  # 宿主匹配异常只降级为跳过
            return None, f"宿主目录匹配失败：{err}"
        if matched is None:
            return None, "宿主未匹配到下载目录"
        return self._effective_event_entry(
            matched,
            missing_reason="宿主命中的下载目录不在当前目录配置中",
            ineffective_reason="目录键 {key} 命中重复键中的非生效项，已跳过",
        )

    def _effective_event_entry(
        self,
        directory: Any,
        *,
        missing_reason: str,
        ineffective_reason: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """把宿主返回的目录对象定位成生效条目, 未命中或非生效项按调用方措辞跳过."""
        entry = self._find_event_directory_entry(directory)
        if entry is None:
            return None, missing_reason
        if not entry["effective"]:
            return None, ineffective_reason.format(key=entry["key"])
        return entry, ""

    @staticmethod
    def _subscription_id_from_source(source: Any) -> int | None:
        """从 ``Subscribe|{json}`` 来源中解析订阅 ID, 非订阅来源或解析失败返回 None."""
        text = _text(source)
        if not text.startswith(SUBSCRIPTION_SOURCE_PREFIX):
            return None
        try:
            payload = json.loads(text[len(SUBSCRIPTION_SOURCE_PREFIX) :])
        except ValueError:
            return None
        if not isinstance(payload, Mapping):
            return None
        value = payload.get("id")
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def _subscription_save_path(self, source: Any) -> str:
        """只读订阅自身的保存路径, 订阅不存在或读取失败时返回空串并按媒体回退."""
        sid = self._subscription_id_from_source(source)
        if sid is None:
            return ""
        try:
            subscribe = SubscribeOper().get(sid)
        except Exception as err:  # 订阅读取失败不打断事件处理, 降级为按媒体匹配
            logger.warning(f"[{self.plugin_name}] 读取订阅 {sid} 的保存路径失败，回退按媒体匹配目录：{err}")
            return ""
        if subscribe is None:
            return ""
        return _text(getattr(subscribe, "save_path", None))

    def _media_matches_multiple(self, media_info: Any) -> bool:
        """判断媒体是否同时匹配多个下载目录, 匹配数不超过 1 时不算歧义."""
        helper = DirectoryHelper()
        directories = helper.get_download_dirs()
        return sum(1 for directory in directories if helper.matches_media(directory, media_info)) > 1

    def _find_event_directory_entry(self, directory: Any) -> dict[str, Any] | None:
        """按宿主命中的目录对象定位条目, 同名重复键时优先精确匹配目录名."""
        keys = self._directory_keys
        if not keys:
            return None
        plain = self._directory_plain(directory)
        if plain is None:
            return None
        matches = [
            entry for entry in keys.get("entries") or () if directory_key(plain, entry["sequence"]) == entry["key"]
        ]
        if not matches:
            return None
        name = _text(plain.get("name"))
        exact = [entry for entry in matches if entry["name"] == name]
        return dict((exact or matches)[0])

    def _warn_directory_conflicts(self) -> None:
        """目录存在完整键重复时每次初始化只记一条 warning, 供运行期排查."""
        if self._conflicts_warned:
            return
        conflicts = (self._directory_keys or {}).get("conflicts") or []
        if not conflicts:
            return
        self._conflicts_warned = True
        details = "；".join(f"{item['key']}（{item['count']} 条）" for item in conflicts)
        logger.warning(f"[{self.plugin_name}] 目录存在完整键重复，按 priority 升序首条生效：{details}")

    @classmethod
    def _context_value(cls, context: Any, name: str) -> Any:
        """按映射键或同名属性读取事件上下文顶层字段, 两者都缺失时返回 None."""
        if isinstance(context, Mapping):
            return context.get(name)
        return getattr(context, name, None)

    def _config_snapshot(self) -> dict[str, Any]:
        """返回当前规范化配置, 未初始化时按默认配置运行."""
        config = self._config
        if isinstance(config, Mapping):
            return dict(config)
        normalized, _ = normalize_config(None)
        return normalized

    # ---------------- 记录与持久化 ----------------

    def _now(self) -> datetime:
        """返回当前时间, 时间源可注入且统一为带时区值."""
        value = self._clock()
        return _as_aware(value) if isinstance(value, datetime) else _utc_now()

    def _record_limit(self) -> int:
        """返回处理记录条数上限, 非法注入值回退源码常量."""
        value = getattr(self, "_max_records", MAX_RECORDS)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            return MAX_RECORDS
        return value

    def _is_seen(self, hash_value: str, now: datetime) -> bool:
        """判断同一 hash 是否在 7 天内处理过."""
        stamp = _parse_time(self._load_seen().get(hash_value))
        return stamp is not None and stamp >= now - SEEN_TTL

    def _record(
        self,
        *,
        now: datetime,
        hash_value: str,
        title: str,
        directory_key: str,
        level: str,
        category: str,
        tags: list[str],
        status: str,
        reason: str,
        mark_seen: bool = False,
    ) -> None:
        """追加一条处理记录并顺带清理过期数据、按上限裁剪."""
        record = {
            "time": now.isoformat(),
            "hash": hash_value,
            "title": _text(title),
            "directory_key": _text(directory_key),
            "level": _text(level),
            "category": _text(category),
            "tags": [_text(tag) for tag in tags if _text(tag)],
            "status": _text(status),
            "reason": _text(reason),
        }
        records = self._load_records()
        records.append(record)
        seen = self._load_seen()
        if mark_seen and hash_value:
            seen[hash_value] = record["time"]
        self._persist(records, seen, now)

    def _record_exception_failure(self, event: Any, err: Exception) -> None:
        """异常兜底记录, 自身失败只记日志, 不递归记录、不向宿主抛出."""
        if getattr(self, "_reporting_failure", False):
            return
        self._reporting_failure = True
        try:
            payload = getattr(event, "event_data", None)
            hash_value = _text(payload.get("hash")) if isinstance(payload, Mapping) else ""
            self._record(
                now=self._now(),
                hash_value=hash_value,
                title="",
                directory_key="",
                level="",
                category="",
                tags=[],
                status=WriterStatus.FAILED.value,
                reason=f"处理事件异常：{err}",
            )
        except Exception as inner:  # 兜底记录本身失败时只记日志
            logger.warning(f"[{self.plugin_name}] 记录事件处理异常失败：{inner}")
        finally:
            self._reporting_failure = False

    def _load_records(self) -> list[dict[str, Any]]:
        """读取处理记录, 类型不符或读取失败时降级为内存缓存/空列表."""
        try:
            raw = self.get_data(RECORDS_KEY)
        except Exception as err:  # 读取失败时保留内存可见性
            logger.warning(f"[{self.plugin_name}] 读取处理记录失败，本次使用内存缓存：{err}")
            raw = self._records_cache
        if raw is None:
            records: list[dict[str, Any]] = []
        elif isinstance(raw, list):
            records = [dict(item) for item in raw if isinstance(item, Mapping)]
        else:
            logger.warning(f"[{self.plugin_name}] 处理记录类型 {type(raw).__name__} 不符合预期，已按空列表处理")
            records = list(self._records_cache or [])
        self._records_cache = records
        return records

    def _load_seen(self) -> dict[str, str]:
        """读取 7 天去重表, 类型不符或读取失败时降级为内存缓存/空字典."""
        try:
            raw = self.get_data(SEEN_KEY)
        except Exception as err:  # 读取失败时保留内存可见性
            logger.warning(f"[{self.plugin_name}] 读取去重表失败，本次使用内存缓存：{err}")
            raw = self._seen_cache
        if raw is None:
            seen: dict[str, str] = {}
        elif isinstance(raw, Mapping):
            seen = {key: value for key, value in raw.items() if isinstance(key, str)}
        else:
            logger.warning(f"[{self.plugin_name}] 去重表类型 {type(raw).__name__} 不符合预期，已按空表处理")
            seen = dict(self._seen_cache or {})
        self._seen_cache = seen
        return seen

    def _persist(self, records: list[dict[str, Any]], seen: dict[str, str], now: datetime) -> None:
        """清理 7 天前记录与去重项、按上限裁剪并持久化, 保存异常只记 warning."""
        cutoff = now - SEEN_TTL
        kept_seen = {
            key: value for key, value in seen.items() if (stamp := _parse_time(value)) is not None and stamp >= cutoff
        }
        kept_records = [item for item in records if (stamp := _parse_time(item.get("time"))) is None or stamp >= cutoff]
        kept_records = kept_records[-self._record_limit() :]
        self._records_cache = kept_records
        self._seen_cache = kept_seen
        try:
            self.save_data(RECORDS_KEY, kept_records)
            self.save_data(SEEN_KEY, kept_seen)
        except Exception as err:  # 持久化失败不抛给事件层，内存缓存已保留本次结果
            logger.warning(f"[{self.plugin_name}] 保存处理记录失败，本次结果只保留在内存：{err}")
        else:
            logger.debug(
                f"[{self.plugin_name}] 处理记录与去重表已保存：{len(kept_records)} 条记录，{len(kept_seen)} 条去重"
            )

    def get_state(self) -> bool:
        """返回插件当前启用状态.

        Returns:
            True 表示已启用，False 表示已停用。

        """
        return self._enabled

    @staticmethod
    def get_command() -> list[dict[str, Any]]:
        """返回插件远程命令列表.

        本期不注册任何远程命令，空列表表示钩子已实现且无内容。
        """
        return []

    def get_api(self) -> list[dict[str, Any]]:
        """返回插件 API 列表.

        本期不注册任何 API，空列表表示钩子已实现且无内容。

        Returns:
            空列表。

        """
        return []

    # ---------------- 配置页 ----------------

    def get_form(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """返回配置页 JSON 与默认配置模型.

        表单分三块：基础开关与触发来源、默认规则、按宿主实时目录生成的目录规则面板；只读
        提示（配置/宿主 issue、失效目录键、完整键冲突、优先级说明）只作为页面组件出现，
        不进入默认模型。目录规则区每个目录使用一个默认折叠面板，标题只展示目录摘要，
        展开后显示下载路径与分类/标签输入；不生效目录只显示合并警告。目录面板与规则键
        全部取自最近一次 ``init_plugin`` 生成的 ``_directories`` / ``_directory_keys``，
        本方法不重新读取、不重新排序、也不写回宿主配置或下载器。

        Returns:
            ``(form, default_model)``. ``form`` 是可 ``json.dumps`` 的 Vuetify JSON；
            ``default_model`` 的键集合与 ``rules.CONFIG_KEYS`` 完全一致。

        """
        normalized = self._form_base_config()
        content: list[dict[str, Any]] = [
            *self._form_issue_alerts(),
            *self._form_basic_sections(),
            *self._form_directory_sections(normalized),
        ]
        return [{"component": "VForm", "content": content}], self._form_default_model(normalized)

    def _form_base_config(self) -> Mapping[str, Any]:
        """返回配置页基准配置：优先运行态规范化配置, 未初始化时用空配置默认值."""
        config = self._config
        if isinstance(config, Mapping):
            return config
        normalized, _ = normalize_config(None)
        return normalized

    def _form_issue_alerts(self) -> list[dict[str, Any]]:
        """把配置提示与宿主只读提示渲染为只读告警, 不写入默认模型."""
        alerts: list[dict[str, Any]] = []
        for title, issues in (("配置提示", self._config_issues), ("宿主读取提示", self._host_issues)):
            if not issues:
                continue
            alerts.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "warning",
                        "variant": "tonal",
                        "class": "mb-4 text-pre-line",
                        "title": title,
                        "text": "\n".join(str(issue) for issue in issues),
                    },
                }
            )
        return alerts

    @staticmethod
    def _form_basic_sections() -> list[dict[str, Any]]:
        """生成基础开关、触发来源与默认规则行."""
        return [
            {
                "component": "VRow",
                "props": {"class": "mb-3"},
                "content": [
                    _form_col("VSwitch", {"model": "enabled", "label": "启用插件", "hint": "关闭后不处理任何下载事件"}),
                    _form_col(
                        "VCombobox",
                        {
                            "model": "trigger",
                            "label": "触发来源",
                            "items": ["subscribe", "all"],
                            "clearable": False,
                            "hint": "subscribe 仅处理订阅来源的下载；all 处理全部下载",
                        },
                    ),
                    _form_col(
                        "VSwitch",
                        {
                            "model": "dry_run",
                            "label": "仅记录不写入",
                            "hint": "开启后只记录将要写入的分类与标签，不调用下载器",
                        },
                    ),
                ],
            },
            {
                "component": "VRow",
                "props": {"class": "mb-3"},
                "content": [
                    _form_col("VSwitch", {"model": "apply_tags", "label": "写入标签"}),
                    _form_col("VSwitch", {"model": "apply_category", "label": "写入分类"}),
                    _form_col(
                        "VSwitch",
                        {
                            "model": "keep_system_tag",
                            "label": "保留系统标签",
                            "hint": "关闭后写入时移除宿主的系统标签（TORRENT_TAG）",
                        },
                    ),
                ],
            },
            {
                "component": "VRow",
                "props": {"class": "mb-4"},
                "content": [
                    _form_col(
                        "VTextField",
                        {
                            "model": f"{DEFAULT_RULE_PREFIX}.category",
                            "label": "默认分类",
                            "hint": "未命中目录规则时使用的分类；留空表示不修改任务分类",
                        },
                        md=6,
                    ),
                    _form_col(
                        "VCombobox",
                        {
                            "model": f"{DEFAULT_RULE_PREFIX}.tags",
                            "label": "默认标签",
                            "multiple": True,
                            "chips": True,
                            "closable-chips": True,
                            "clearable": True,
                            "hint": "输入标签后回车添加，点 × 删除；与任务既有标签、系统标签合并去重",
                        },
                        md=6,
                    ),
                ],
            },
        ]

    @staticmethod
    def _form_priority_alert() -> dict[str, Any]:
        """返回目录规则优先级与匹配标识说明（只读, 不绑定模型）."""
        return {
            "component": "VAlert",
            "props": {
                "type": "info",
                "variant": "tonal",
                "class": "mb-3 text-pre-line",
                "title": "目录规则优先级",
                "text": (
                    "目录行按宿主的目录优先级（priority）升序排列，数值小者优先。\n"
                    "相同优先级保持宿主配置顺序。\n"
                    "同一下载目录对应同一个目录匹配标识，标识由系统自动生成。\n"
                    "只需在各目录的分类/标签输入框中配置，无需修改匹配标识。\n"
                    "匹配顺序：目录规则 > 默认规则。\n"
                    "同一目录只采用首条生效的目录规则。\n"
                    "目录列表来自系统目录配置，修改系统目录后需在本页保存后刷新。"
                ),
            },
        }

    @staticmethod
    def _form_stale_keys(normalized: Mapping[str, Any], keys: Mapping[str, Any] | None) -> list[str]:
        """返回配置里存在、但当前有效目录键中不存在的目录键."""
        configured = normalized.get("directory_rules") or {}
        effective = set((keys or {}).get("effective_keys") or {})
        return [str(key) for key in configured if key not in effective]

    @staticmethod
    def _quoted_names(names: Sequence[str]) -> str:
        """把目录名列表按中文习惯拼接成「A」、「B」和「C」."""
        quoted = [f"“{name}”" for name in names]
        if len(quoted) == 1:
            return quoted[0]
        return "、".join(quoted[:-1]) + f"和{quoted[-1]}"

    @classmethod
    def _form_conflict_text(cls, conflict: Mapping[str, Any]) -> str:
        """按首条生效项说明哪些目录共用同一规则, 只用目录名, 不出现内部键."""
        names = [str(name).strip() or "未命名目录" for name in conflict.get("names") or ()]
        if len(names) < 2:
            return ""
        return (
            f"{cls._quoted_names(names)}对应同一目录规则，"
            f"请只在{cls._quoted_names(names[:1])}中设置分类和标签；"
            f"当前{cls._quoted_names(names[1:])}不生效。"
        )

    def _form_directory_alerts(self, normalized: Mapping[str, Any]) -> list[dict[str, Any]]:
        """生成目录规则冲突与已失效目录键的只读提示."""
        keys = self._directory_keys
        alerts: list[dict[str, Any]] = []
        for conflict in (keys or {}).get("conflicts") or ():
            conflict_text = self._form_conflict_text(conflict)
            alerts.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "warning",
                        "variant": "tonal",
                        "class": "mb-3",
                        "title": "目录规则冲突",
                        "text": conflict_text,
                    },
                }
            )
        stale = self._form_stale_keys(normalized, keys)
        if stale:
            alerts.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "warning",
                        "variant": "tonal",
                        "class": "text-pre-line",
                        "title": f"已失效目录键（{len(stale)} 个）",
                        "text": "\n".join(
                            f"{key} → 已失效：当前宿主目录配置中不存在该键，对应规则不会命中" for key in stale
                        ),
                    },
                }
            )
        return alerts

    def _form_directory_sections(self, normalized: Mapping[str, Any]) -> list[dict[str, Any]]:
        """生成目录规则区：全局提示与默认折叠的逐目录编辑面板."""
        raw_entries = (self._directory_keys or {}).get("entries")
        entries: list[dict[str, Any]] = []
        if isinstance(raw_entries, (list, tuple)):
            entries = [dict(entry) for entry in raw_entries if isinstance(entry, Mapping)]
        sections: list[dict[str, Any]] = [self._form_priority_alert(), *self._form_directory_alerts(normalized)]
        if not entries:
            sections.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "title": "暂无可编辑的目录规则行",
                        "text": ("当前宿主目录配置中没有 download_path 非空的目录；仅可使用默认规则"),
                    },
                }
            )
            return sections
        sections.append(
            {
                "component": "VExpansionPanels",
                "props": {"class": "mb-4"},
                "content": [
                    self._form_directory_row(entry, self._directory_snapshot(entry.get("sequence")))
                    for entry in entries
                ],
            }
        )
        return sections

    def _directory_snapshot(self, sequence: Any) -> Mapping[str, Any]:
        """按 1-based 序号取同一序列的宿主目录快照, 仅供展示媒体类型/类别."""
        directories = self._directories
        if isinstance(sequence, int) and 1 <= sequence <= len(directories):
            item = directories[sequence - 1]
            if isinstance(item, Mapping):
                return item
        return {}

    @staticmethod
    def _form_directory_location(entry: Mapping[str, Any]) -> str:
        """返回目录的下载位置, 不展示内部规则键或宿主字段名."""
        storage = str(entry.get("storage") or "").strip()
        download_path = str(entry.get("download_path") or "").strip()
        return f"{storage}:{download_path}" if storage else download_path

    @staticmethod
    def _form_directory_title(entry: Mapping[str, Any], source: Mapping[str, Any]) -> str:
        """生成折叠面板短标题: 序号、目录名、可选媒体信息与生效状态."""
        name = str(entry.get("name") or "").strip() or "未命名目录"
        media_type = str(source.get("media_type") or "").strip()
        media_category = str(source.get("media_category") or "").strip()
        media = " / ".join(part for part in (media_type, media_category) if part)
        segments = [f"#{entry.get('sequence')} · {name}"]
        if media:
            segments.append(media)
        if not entry.get("effective"):
            segments.append("不生效")
        return " · ".join(segments)

    @staticmethod
    def _form_directory_row(entry: Mapping[str, Any], source: Mapping[str, Any]) -> dict[str, Any]:
        """按一条目录键条目生成默认折叠面板, 非生效条目不提供输入控件."""
        key = str(entry.get("key") or "")
        location = SubscribeDownloadAttr._form_directory_location(entry)
        if not entry.get("effective"):
            body = [
                _form_component(
                    "VAlert",
                    {
                        "type": "warning",
                        "variant": "tonal",
                        "density": "compact",
                        "title": "目录规则不生效",
                        "text": (f"下载路径：{location}；与前面的目录对应同一规则，请只在前面的目录中设置分类和标签"),
                    },
                )
            ]
        else:
            body = [
                _form_component(
                    "VAlert",
                    {
                        "type": "info",
                        "variant": "tonal",
                        "density": "compact",
                        "text": f"下载路径：{location}",
                    },
                ),
                {
                    "component": "VRow",
                    "props": {"class": "mt-2"},
                    "content": [
                        _form_col(
                            "VTextField",
                            {
                                "model": f"{DIRECTORY_RULE_PREFIX}.{key}.category",
                                "label": "目录分类",
                                "density": "compact",
                                "hint": "填写 qBittorrent 分类名称，例如：电视剧、动漫；留空则不修改分类",
                            },
                            cols=12,
                            md=4,
                        ),
                        _form_col(
                            "VCombobox",
                            {
                                "model": f"{DIRECTORY_RULE_PREFIX}.{key}.tags",
                                "label": "目录标签",
                                "density": "compact",
                                "multiple": True,
                                "chips": True,
                                "closable-chips": True,
                                "clearable": True,
                                "hint": "回车添加标签，可添加多个；留空则不修改标签",
                            },
                            cols=12,
                            md=8,
                        ),
                    ],
                },
            ]
        return {
            "component": "VExpansionPanel",
            "props": {"title": SubscribeDownloadAttr._form_directory_title(entry, source)},
            "content": [{"component": "VExpansionPanelText", "content": body}],
        }

    @staticmethod
    def _form_default_model(normalized: Mapping[str, Any]) -> dict[str, Any]:
        """按八键契约构造默认模型：规则结构深拷贝, 不额外附带提示字段."""
        default_rule = normalized.get("default_rule") or {}
        directory_rules = normalized.get("directory_rules") or {}
        return {
            "enabled": bool(normalized.get("enabled")),
            "trigger": str(normalized.get("trigger") or "subscribe"),
            "apply_tags": bool(normalized.get("apply_tags")),
            "apply_category": bool(normalized.get("apply_category")),
            "keep_system_tag": bool(normalized.get("keep_system_tag")),
            "dry_run": bool(normalized.get("dry_run")),
            "default_rule": {
                "category": str(default_rule.get("category") or ""),
                "tags": [str(tag) for tag in default_rule.get("tags") or []],
            },
            "directory_rules": {
                str(key): {
                    "category": str((rule or {}).get("category") or ""),
                    "tags": [str(tag) for tag in (rule or {}).get("tags") or []],
                }
                for key, rule in directory_rules.items()
            },
        }

    # ---------------- 详情页（只读记录） ----------------

    def get_page(self) -> list[dict[str, Any]] | None:
        """返回详情页：只读处理记录区块.

        页面不写插件数据、不调用下载器写入适配，记录只经 ``_load_records()`` 读取，
        读取异常按既有降级规则处理。

        Returns:
            可 ``json.dumps`` 的 Vuetify JSON：无记录时为一个 ``VAlert`` 空态，有记录时为
            单个「最近处理记录」说明块、状态分布 ``VChip`` 行、逐条摘要与只读 ``VDataTable``。

        """
        return self._page_record_section()

    def _page_record_section(self) -> list[dict[str, Any]]:
        """返回说明、状态分布、逐条摘要与只读表格, 无记录时收缩为空态."""
        records = self._page_records()
        if not records:
            return [self._page_empty_alert()]
        return [
            self._page_overview_alert(len(records)),
            self._page_status_chips(records),
            self._page_summaries(records),
            self._page_table(records),
        ]

    @staticmethod
    def _record_summary(record: Mapping[str, Any]) -> str:
        """把一条处理记录转成按状态区分的人类可读摘要."""
        item = SubscribeDownloadAttr._page_item(record)
        title = item["title"]
        category = item["category"]
        tags = "、".join(item["tags"])
        status = item["status"]
        reason = item["reason"]
        if status in {WriterStatus.OK.value, STATUS_DRY_RUN}:
            attributes = []
            if category:
                attributes.append(f"分类「{category}」")
            if tags:
                attributes.append(f"标签「{tags}」")
            if status == WriterStatus.OK.value:
                if not attributes:
                    return f"《{title}》已处理"
                return f"《{title}》注入了{'、'.join(attributes)}"
            suffix = "（未调用下载器）"
            if not attributes:
                return f"《{title}》拟写入{suffix}"
            return f"《{title}》拟写入{'、'.join(attributes)}{suffix}"
        if status == WriterStatus.FAILED.value:
            return f"《{title}》写入失败：{reason}"
        if status == WriterStatus.UNSUPPORTED.value:
            return f"《{title}》部分写入：{reason}"
        if status == WriterStatus.SKIPPED.value:
            return f"《{title}》未写入：{reason}"
        return f"《{title}》：{reason}"

    def _page_records(self) -> list[dict[str, Any]]:
        """读取处理记录并转成最近优先的 JSON 安全拷贝, 持久化顺序与内容不变."""
        return [self._page_item(record) for record in reversed(self._load_records())]

    @staticmethod
    def _page_item(record: Mapping[str, Any]) -> dict[str, Any]:
        """把一条记录收敛为九字段的 JSON 安全普通 dict, 不改动业务取值."""
        raw_tags = record.get("tags")
        if isinstance(raw_tags, str):
            tags = [text for tag in (raw_tags,) if (text := _text(tag))]
        elif isinstance(raw_tags, Sequence):
            tags = [text for tag in raw_tags if (text := _text(tag))]
        else:
            tags = []
        return {
            "time": _text(record.get("time")),
            "hash": _text(record.get("hash")),
            "title": _text(record.get("title")),
            "directory_key": _text(record.get("directory_key")),
            "level": _text(record.get("level")),
            "category": _text(record.get("category")),
            "tags": tags,
            "status": _text(record.get("status")),
            "reason": _text(record.get("reason")),
        }

    @staticmethod
    def _page_empty_alert() -> dict[str, Any]:
        """返回无处理记录时的只读空态提示, 不抛异常也不引入刷新入口."""
        return {
            "component": "VAlert",
            "props": {
                "type": "info",
                "variant": "tonal",
                "title": "暂无处理记录",
                "text": (
                    "启用插件并触发订阅下载后，最近 7 天的分类与标签写入结果会显示在这里；"
                    "记录区只读，不提供刷新或重写操作。"
                ),
            },
        }

    @staticmethod
    def _page_overview_alert(total: int) -> dict[str, Any]:
        """返回记录区唯一的说明块, 只描述倒序范围与只读语义.

        状态含义由下方带颜色标签直接展示，说明块不另外罗列 ``状态=标签``。
        """
        return {
            "component": "VAlert",
            "props": {
                "type": "info",
                "variant": "tonal",
                "title": f"最近处理记录（最近在前，共 {total} 条）",
                "text": f"倒序展示最近 7 天内、最多 {MAX_RECORDS} 条；记录区只读。",
            },
        }

    @staticmethod
    def _page_status_chips(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """按记录中出现的状态生成只读 VChip 行, 标签与颜色取自状态映射."""
        counts: dict[str, int] = {}
        for record in records:
            status = _text(record.get("status"))
            counts[status] = counts.get(status, 0) + 1
        statuses = [status for status in PAGE_STATUS_ORDER if counts.get(status)]
        statuses.extend(sorted(status for status in counts if status not in PAGE_STATUS_DISPLAY))
        chips: list[dict[str, Any]] = []
        for status in statuses:
            display = _status_display(status)
            chips.append(
                {
                    "component": "VChip",
                    "props": {
                        "color": display["color"],
                        "variant": "tonal",
                        "size": "small",
                        "class": "mr-2",
                    },
                    "text": f"{display['label']} {counts[status]}",
                }
            )
        return {
            "component": "VRow",
            "props": {"class": "mt-2"},
            "content": [{"component": "VCol", "props": {"cols": 12}, "content": chips}],
        }

    @staticmethod
    def _page_summaries(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """返回逐条处理明细列表, 标题与摘要正文分离且复用统一摘要文案."""
        items = []
        for record in records:
            title = f"《{_text(record.get('title'))}》"
            summary = SubscribeDownloadAttr._record_summary(record)
            subtitle = summary.removeprefix(title).removeprefix("：")
            items.append(
                {
                    "component": "VListItem",
                    "props": {"title": title, "subtitle": subtitle},
                }
            )
        return {
            "component": "VRow",
            "props": {"class": "mt-2"},
            "content": [
                {
                    "component": "VCol",
                    "props": {"cols": 12},
                    "content": [
                        {
                            "component": "VList",
                            "props": {"density": "compact", "class": "py-0"},
                            "content": [
                                {"component": "VListSubheader", "text": "处理明细"},
                                *items,
                            ],
                        }
                    ],
                }
            ],
        }

    @staticmethod
    def _page_table(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """返回只读 VDataTable：九列表头与记录字段一一对应, 不提供任何编辑能力."""
        return {
            "component": "VDataTable",
            "props": {
                "headers": [dict(header) for header in PAGE_HEADERS],
                "items": [dict(record) for record in records],
                "density": "compact",
                "hide-default-footer": True,
                "items-per-page": -1,
                "no-data-text": "暂无处理记录",
                "class": "mt-2 pb-16",
            },
        }

    def stop_service(self) -> None:
        """停止插件并复位启用状态、配置与宿主只读运行态.

        可重复调用，不持有后台线程、定时器或事件监听。
        """
        self._enabled = False
        self._config = None
        self._config_issues = ()
        self._directories = ()
        self._directory_keys = None
        self._host_issues = ()
        self._system_tag = DEFAULT_SYSTEM_TAG
        self._conflicts_warned = False
        self._records_cache = None
        self._seen_cache = None
        self._reporting_failure = False

    def get_module(self) -> dict[str, Any] | None:
        """返回插件模块声明.

        当前为占位实现，不劫持任何系统模块实现。
        """
        pass
