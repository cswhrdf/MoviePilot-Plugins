import os
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from threading import Timer
from typing import Literal, cast, overload

from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import TransferInfo
from app.schemas.types import EventType

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
NFO_EXTS = {".nfo"}
SEPARATORS = ["-", "_", " ", "."]


def _remove_conflicting_image_targets(src: Path, dest: Path) -> None:
    """删除目标目录中与待写入图片同 stem 的其他图片文件.

    仅针对图片类型处理, 避免误删 NFO 或其他非图片文件.
    若同一个 stem 通过 .jpg / .png / .webp / .jpeg 等不同后缀同时存在,
    则在写入前统一清理, 保证最终目标位置只有一份图片.

    Args:
        src: 源文件路径, 表示要被搬运的文件
        dest: 目标文件路径, 表示搬运后的落点

    """
    if src.suffix.lower() not in IMAGE_EXTS and dest.suffix.lower() not in IMAGE_EXTS:
        return

    for ext in sorted(IMAGE_EXTS, key=lambda item: item.lower()):
        candidate = dest.with_suffix(ext)
        if candidate == dest:
            continue
        if candidate.exists() or candidate.is_symlink():
            if candidate.is_dir() and not candidate.is_symlink():
                raise IsADirectoryError(f"目标图片路径是目录, 不能覆盖: {candidate}")
            candidate.unlink()

    if dest.exists() or dest.is_symlink():
        if dest.is_dir() and not dest.is_symlink():
            raise IsADirectoryError(f"目标路径是目录, 不能覆盖: {dest}")
        dest.unlink()


def _transfer_with_method(src: Path, dest: Path, transfer_type: str) -> None:
    """根据当前转移方式把文件复制到目标目录.

    在实际写入前, 会先检查目标位置是否已有同名图片文件；若存在则删除, 避免覆盖冲突.

    Args:
        src: 源文件路径, 表示要被搬运的文件
        dest: 目标文件路径, 表示搬运后的落点
        transfer_type: 传输策略, 支持 copy、move、hardlink、symlink

    """
    method = (transfer_type or "copy").lower()

    # 兼容宿主侧常见的别名: link / hard / hardlink / hard_link
    if method in {"hard", "hardlink", "hard_link", "link"}:
        method = "hardlink"
    elif method in {"soft", "symlink", "softlink", "soft_link"}:
        method = "symlink"

    _remove_conflicting_image_targets(src, dest)

    match method:
        case "hardlink":
            os.link(str(src), str(dest))
        case "symlink":
            os.symlink(str(src), str(dest))
        case "move":
            shutil.move(str(src), str(dest))
        case "copy" | _:
            shutil.copy2(str(src), str(dest))


@overload
def _find_asset_for_stem(
    dir_path: Path, stem: str, kind: Literal["poster", "fanart"], asset_name: str | None = None
) -> list[Path] | None: ...
@overload
def _find_asset_for_stem(
    dir_path: Path, stem: str, kind: Literal["nfo"], asset_name: str | None = None
) -> Path | None: ...


def _find_asset_for_stem(
    dir_path: Path, stem: str, kind: Literal["poster", "fanart", "nfo"] = "poster", asset_name: str | None = None
) -> list[Path] | Path | None:
    """统一查找资产.

    Args:
        dir_path: 要搜索的目录, 通常是媒体文件所在目录
        stem: 媒体文件名的主干, 通常等于文件的 stem, 不含扩展名
        kind: 要查找的资产类型, 支持 poster、fanart 和 nfo
        asset_name: 自定义资产名后缀, 例如 poster/fanart 的外部配置值

    Returns:
        poster/fanart: 返回匹配到的图片路径列表
        nfo: 返回匹配到的 NFO 路径；未找到时返回 None

    """
    kind = cast(Literal["poster", "fanart", "nfo"], kind.lower())
    normalized_asset_name = (asset_name or kind).strip()
    if not normalized_asset_name:
        normalized_asset_name = kind
    if kind in {"poster", "fanart"}:
        found: list[Path] = []
        for sep in SEPARATORS:
            for ext in IMAGE_EXTS:
                candidate = dir_path / f"{stem}{sep}{normalized_asset_name}{ext}"
                if candidate.exists():
                    found.append(candidate)
        return found
    if kind == "nfo":
        for ext in NFO_EXTS:
            candidate = dir_path / f"{stem}{ext}"
            if candidate.exists():
                return candidate
    return None


def _parse_season_episode(name: str) -> tuple[int, int] | None:
    """从文件名解析季和集编号.

    Args:
        name: 需要解析的文件名或路径名

    Returns:
        返回 (season, episode) 二元组；若未匹配到 SxxExx 格式则返回 None

    """
    # 在文件名中查找 SxxExx 格式（例如 S01E03 或 s01e03）, 返回 (season, episode)
    m = re.search(r"[Ss](\d{1,2})[Ee](\d{1,2})", name)
    if m:
        try:
            season = int(m.group(1))
            episode = int(m.group(2))
            return season, episode
        except Exception:  # noqa: BLE001
            return None
    return None


def _build_renamed_sidecar_path(
    target_dir: Path,
    media_path: Path,
    sidecar_path: Path,
    poster_asset_name: str = "poster",
) -> Path:
    """按媒体文件的目标命名规则重命名剧集 poster 文件.

    Args:
        target_dir: 目标目录, 通常是媒体文件的父目录
        media_path: 目标媒体文件路径, 用于取其 stem 生成最终文件名
        sidecar_path: 源 sidecar 文件路径, 用于提取原始后缀和类型
        poster_asset_name: 用于识别 poster 侧载图的名称后缀, 默认为 poster

    Returns:
        最终应写入的目标文件路径

    """
    name_lower = sidecar_path.name.lower()
    normalized_poster_name = (poster_asset_name or "poster").strip().lower()
    for sep in SEPARATORS:
        suffix = f"{sep}{normalized_poster_name}"
        if name_lower.endswith(f"{suffix}{sidecar_path.suffix.lower()}"):
            return target_dir / f"{media_path.stem}{sidecar_path.suffix}"

    return target_dir / f"{media_path.stem}{sidecar_path.suffix}"


def _is_media_file(path: Path) -> bool:
    """判断路径是否属于真正的媒体文件.

    Args:
        path: 要检查的文件路径

    Returns:
        True 表示它不是 poster/fanart/NFO sidecar, 而是媒体文件本体; False 表示它是衍生资源文件

    """
    return path.suffix.lower() not in IMAGE_EXTS and path.suffix.lower() not in NFO_EXTS


def _local_tag_name(tag_name: str) -> str:
    """返回 XML 标签的本地名称, 忽略命名空间前缀.

    Args:
        tag_name: XML 标签名, 可能带命名空间前缀, 例如 "{http://example.com}title"。

    Returns:
        去除命名空间后缀后的标签名。若原值不包含命名空间, 则原样返回。

    """
    if tag_name.startswith("{") and "}" in tag_name:
        return tag_name.rsplit("}", 1)[1]
    return tag_name


def _parse_nfo_remove_tags(raw_tags: str | None) -> set[str]:
    """解析前端传入的 NFO 删除标签配置.

    约定：前端传入的是标签名字符串, 多标签使用逗号分隔, 支持中英文逗号以及
    大小写不敏感匹配；标签内容本身不参与配置。

    Args:
        raw_tags: 由前端传入的标签名称文本, 多个标签使用逗号或中文逗号分隔。

    Returns:
        规范化后的标签名集合, 全部转换为小写并去掉命名空间前缀。

    """
    if raw_tags is None:
        return set()

    parts = re.split(r"[，,]+", str(raw_tags))
    tags: set[str] = set()
    for part in parts:
        name = (part or "").strip()
        if not name:
            continue
        normalized = _local_tag_name(name).strip().lower()
        if normalized:
            tags.add(normalized)
    return tags


def _rewrite_nfo_xml(source_nfo: Path, dest_nfo: Path, tag_names: str | None) -> None:
    """读取 NFO XML 文件, 删除指定标签后写入目标位置.

    该函数不会直接复制原始 NFO 文件, 而是会解析 XML, 并仅删除与配置中标签名
    匹配的节点。这样可以保留其它 XML 内容, 确保目标 NFO 仍保持合法的 XML 结构。

    Args:
        source_nfo: 源 NFO 文件路径。
        dest_nfo: 目标文件路径, 写入后的位置。
        tag_names: 前端传入的标签名字符串, 多个标签用逗号分隔。

    Returns:
        无返回值, 函数会直接将处理后的 XML 写入目标文件。

    Raises:
        FileNotFoundError: 当源 NFO 文件不存在时抛出。
        ET.ParseError: 当源 NFO 文件不是合法 XML 时抛出。
        IsADirectoryError: 当目标路径已存在且为目录时抛出。

    """
    remove_tags = _parse_nfo_remove_tags(tag_names)
    logger.debug(f"解析 NFO 删除标签配置: {remove_tags}")

    tree = ET.parse(source_nfo)
    root = tree.getroot()

    def _strip_matching_children(node: ET.Element) -> None:
        for child in list(node):
            child_tag = _local_tag_name(child.tag).lower()
            if child_tag in remove_tags:
                logger.debug(f"删除 NFO 标签: {child_tag}")
                node.remove(child)
                continue
            _strip_matching_children(child)

    if remove_tags:
        _strip_matching_children(root)

    parent = dest_nfo.parent
    if parent and not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
    if dest_nfo.exists() or dest_nfo.is_symlink():
        if dest_nfo.is_dir() and not dest_nfo.is_symlink():
            raise IsADirectoryError(f"目标 NFO 路径是目录, 不能覆盖: {dest_nfo}")
        dest_nfo.unlink()

    ET.indent(tree, space="    ", level=0)
    tree.write(dest_nfo, encoding="utf-8", xml_declaration=True)


class TransferImageSidecar(_PluginBase):  # noqa: D101
    # --- 插件元数据（前端/市场使用） ---
    # 在前端展示的插件名称
    plugin_name = "媒体刮削文件转移"
    # 插件描述（可改为中文以便在市场中显示更友好）
    plugin_desc = "转移媒体文件目录下的fanart/poster和nfo,适用与已刮削完成的媒体文件"
    # 插件图标文件名（放在静态资源目录）
    plugin_icon = "movie.jpg"
    # 插件版本（应与 package.v2.json 保持一致）
    plugin_version = "0.1.3"
    # 作者信息
    plugin_author = "cswhrdf"
    # 配置项前缀, 用于配置存储中区分本插件项
    plugin_config_prefix = "transfer_image_sidecar_"
    # 插件加载顺序, 值越小越早加载
    plugin_order = 25

    # 运行时启用开关, init_plugin 会根据配置覆盖
    _enabled: bool = True
    _delay_seconds: int = 20  # 延迟秒数执行
    _enable_first_episode_promotion: bool = True  # 启用媒体海报转移
    _media_poster_season: int = 1  # 媒体海报季
    _media_poster_episode: int = 1  # 媒体海报集
    _generate_season_posters: bool = True  # 启用转移季海报
    _season_poster_episode: int = 1  # 季海报集
    _poster_asset_name: str = "poster"  # 集封面匹配后缀
    _fanart_asset_name: str = "fanart"  # 集艺术图匹配后缀
    _nfo_remove_tags: str = ""  # NFO XML 中要删除的标签名称, 多个标签用逗号分隔
    _delayed_timer: Timer | None = None
    _pending_transfer_events: list[TransferInfo] = []

    def init_plugin(self, config: dict | None = None):
        """初始化插件: 根据传入配置设置启用状态.

        说明: 宿主在加载插件或配置变更时调用此方法。若需要注册事件或启动后台任务,
        可在此处完成（建议使用非阻塞方式）
        """
        config = config or {}
        self._enabled = bool(config.get("enabled", True))
        self._delay_seconds = max(0, int(config.get("delay_seconds", 20)))
        self._enable_first_episode_promotion = bool(config.get("enable_first_episode_promotion", True))
        self._media_poster_season = max(1, int(config.get("media_poster_season", 1)))
        self._media_poster_episode = max(1, int(config.get("media_poster_episode", 1)))
        self._generate_season_posters = bool(config.get("generate_season_posters", True))
        self._season_poster_episode = max(1, int(config.get("season_poster_episode", 1)))
        self._poster_asset_name = (config.get("poster_asset_name") or "poster").strip() or "poster"
        self._fanart_asset_name = (config.get("fanart_asset_name") or "fanart").strip() or "fanart"
        self._nfo_remove_tags = str(config.get("nfo_remove_tags") or "").strip()
        self._pending_transfer_events = []

    def get_state(self) -> bool:
        """返回插件当前启用状态.

        Returns:
            True 表示插件已启用, False 表示插件当前被禁用。

        """
        return self._enabled

    def _notify_transfer_failure(self, context: str, exc: Exception, level: str = "warn") -> None:
        """统一记录转移失败日志并向 MoviePilot 发送系统通知.

        Args:
            context: 失败上下文描述
            exc: 具体异常对象
            level: 日志级别, 可为 "warn" 或 "error"；用于统一控制本地日志输出。

        """
        message = f"转移图片挂载文件处理失败：{context}\n错误：{exc}"
        log_level = (level or "warn").lower()
        if log_level == "error":
            logger.error(message)
        else:
            logger.warn(message)

        try:
            self.post_message(channel="system", title="转移图片挂载文件异常", text=message)
            return
        except Exception:  # noqa: BLE001
            pass
        try:
            self.post_message(title="转移图片挂载文件异常", text=message)
        except Exception:  # noqa: BLE001
            logger.error(f"系统通知发送失败: {message}")

    # --- 转移处理：配对与流程 ---
    def _resolve_transfer_pairs(self, transferinfo: TransferInfo) -> list[tuple[str | None, str | None]]:
        """按索引将源文件和目标文件配对.

        Args:
            transferinfo: 转移事件传递的上下文对象, 包含 file_list 和 file_list_new

        Returns:
            [(source_path, target_path)] 列表, 用于一一对应原文件和目标文件

        """
        src_list: list[str] = transferinfo.file_list or []
        new_list: list[str] = transferinfo.file_list_new or []
        return [(src_list[i] if i < len(src_list) else None, newp) for i, newp in enumerate(new_list)]

    def _promote_first_episode_assets(
        self,
        source_dir: Path,
        src_path: Path,
        new_path: Path,
        transfer_type: str,
    ) -> None:
        """对 S01E01 这类首集媒体, 提升同名 poster/fanart 到媒体级目录.

        这类文件通常会带有同名的 poster/fanart 侧载图, 需在目标媒体目录中写出
        `poster.ext` 和 `fanart.ext`, 便于宿主端在媒体层直接展示海报与背景图。

        Args:
            source_dir: 源媒体文件所在目录
            src_path: 源媒体文件路径
            new_path: 目标媒体文件路径
            transfer_type: 当前文件转移方式, 支持 copy、move、hardlink、symlink

        """
        parsed = _parse_season_episode(new_path.name)
        if parsed is None:
            return

        season, episode = parsed
        target_season = self._media_poster_season
        target_episode = self._media_poster_episode
        if season != target_season or episode != target_episode:
            return

        target_dir = new_path.parent.parent
        for kind in ("poster", "fanart"):
            asset_name = self._poster_asset_name if kind == "poster" else self._fanart_asset_name
            path_list = _find_asset_for_stem(source_dir, src_path.stem, kind, asset_name)
            if not path_list:
                continue
            asset_path = path_list[0] if isinstance(path_list, list) else path_list
            media_dest = target_dir / f"{kind}{asset_path.suffix}"
            try:
                _transfer_with_method(asset_path, media_dest, transfer_type)
                logger.info(
                    f"按 {transfer_type} 方式转移媒体级 {kind}（第 {target_episode} 集）：{asset_path} -> {media_dest}"
                )
            except Exception as e:  # noqa: BLE001
                self._notify_transfer_failure(f"转移媒体级 {kind} 失败 {asset_path} -> {media_dest}", e, level="warn")

    def _transfer_sidecars_and_nfo(
        self,
        oldp: str | None,
        newp: str,
        transfer_type: str,
    ) -> None:
        """将源目录中的 poster/NFO 资源搬移到目标目录.

        Args:
            oldp: 源媒体文件路径。该文件本体必须是媒体文件, 不是 sidecar 文件
            newp: 转移后的目标媒体文件路径
            transfer_type: 当前转移方式, 影响 poster/NFO 复制方式, 可为 copy、move、hardlink、symlink

        """
        if not oldp or not newp:
            return

        src_path = Path(oldp)
        new_path = Path(newp)
        target_dir = new_path.parent
        source_dir = src_path.parent
        if not _is_media_file(src_path) or not source_dir.exists():
            return

        self._promote_first_episode_assets(source_dir, src_path, new_path, transfer_type)

        stem = src_path.stem

        assets = _find_asset_for_stem(source_dir, stem, "poster", self._poster_asset_name)
        if not assets:
            return
        if not isinstance(assets, list):
            assets = [assets]
        for img in assets:
            dest = _build_renamed_sidecar_path(target_dir, new_path, img, self._poster_asset_name)
            try:
                _transfer_with_method(img, dest, transfer_type)
                logger.info(f"按 {transfer_type} 方式转移 poster: {img} -> {dest}")
            except Exception as e:  # noqa: BLE001
                self._notify_transfer_failure(f"转移 poster 失败 {img} -> {dest}", e, level="warn")

        nfo = _find_asset_for_stem(source_dir, stem, "nfo")
        if not nfo:
            return
        if isinstance(nfo, list):
            nfo = nfo[0]
        dest_nfo = target_dir / f"{new_path.stem}.nfo"
        try:
            _rewrite_nfo_xml(nfo, dest_nfo, self._nfo_remove_tags)
            logger.info(f"已按配置过滤 NFO 标签后写入: {nfo} -> {dest_nfo}")
        except Exception as e:  # noqa: BLE001
            self._notify_transfer_failure(f"转移并过滤 NFO 失败 {nfo} -> {dest_nfo}", e, level="warn")

    def _collect_season_posters_and_fanart(
        self,
        source_dir: Path,
        old_path: Path,
        new_path: Path,
        season_posters: dict[int, tuple[int, Path, Path]],
    ) -> dict[int, tuple[int, Path, Path]]:
        """根据目标文件名提取季/集信息, 并记录该季最早的 poster 素材.

        Args:
            source_dir: 媒体源文件所在目录
            old_path: 当前已转移好的媒体源文件路径
            new_path: 当前已转移好的目标媒体文件路径
            season_posters: 已累计的季 poster 字典, 键为 season 编号, 值为 (episode, poster_path)

        Returns:
            更新后的 season_posters 字典

        """
        parsed = _parse_season_episode(new_path.name)
        if not parsed:
            return season_posters

        season, episode = parsed
        episode_stem = old_path.stem
        ep_poster = None
        for sep in SEPARATORS:
            for ext in IMAGE_EXTS:
                candidate = source_dir / f"{episode_stem}{sep}{self._poster_asset_name}{ext}"
                if candidate.exists():
                    ep_poster = candidate
                    break
            if ep_poster:
                break
        logger.debug(f"解析季/集信息: 第 {season} 季第 {episode} 集 -> {ep_poster}")

        if ep_poster:
            target_episode = self._season_poster_episode

            if episode == target_episode:
                season_posters[season] = (episode, ep_poster, new_path.parent)
                logger.debug(f"收集季海报素材: 第 {season} 季第 {episode} 集 -> {ep_poster}")

        return season_posters

    def _apply_generated_assets(
        self,
        season_posters: dict[int, tuple[int, Path, Path]],
        transfer_type: str,
    ) -> None:
        """生成并写出季级 poster.

        在媒体目录下输出 seasonXX 文件；
        在 Season 目录下输出 poster 文件

        Args:
            season_posters: 季 poster 素材字典, 键为 season 编号, 值为 (episode, poster_path)
            transfer_type: 生成时使用的传输方式, 例如 copy、move、hardlink、symlink

        """
        for season, (_, poster_path, new_path) in season_posters.items():
            try:
                season_label = f"season{season:02d}"

                season_dest = new_path / f"poster{poster_path.suffix}"
                _transfer_with_method(poster_path, season_dest, transfer_type)
                logger.info(f"按 {transfer_type} 方式转移季海报: {poster_path} -> {season_dest}")

                season_dest = new_path.parent / f"{season_label}-poster{poster_path.suffix}"
                _transfer_with_method(poster_path, season_dest, transfer_type)
                logger.info(f"按 {transfer_type} 方式转移季海报: {poster_path} -> {season_dest}")
            except Exception as e:  # noqa: BLE001
                self._notify_transfer_failure(f"设置第 {season} 季海报失败", e, level="warn")

    def _process_transfer_event(self, transferinfo: TransferInfo) -> None:
        """延迟处理转移完成事件, 并在等待窗口结束后执行 sidecar 与季 poster 整理.

        Args:
            transferinfo: 转移事件对象, 包含 file_list、file_list_new 和 transfer_type

        """
        try:
            transfer_type = (transferinfo.transfer_type or "copy").lower()
            season_posters: dict[int, tuple[int, Path, Path]] = {}
            processed_dirs: set[tuple[str, str]] = set()

            for oldp, newp in self._resolve_transfer_pairs(transferinfo):
                try:
                    if not oldp or not newp:
                        continue

                    src_path = Path(oldp)
                    new_path = Path(newp)
                    logger.debug(f"处理转移文件对: {src_path.name} -> {new_path.name}")
                    dir_key = (str(src_path.parent), str(new_path.parent))
                    if dir_key in processed_dirs:
                        continue
                    processed_dirs.add(dir_key)

                    self._transfer_sidecars_and_nfo(oldp, newp, transfer_type)
                    season_posters = self._collect_season_posters_and_fanart(
                        src_path.parent,
                        src_path,
                        new_path,
                        season_posters,
                    )
                except Exception as inner_e:  # noqa: BLE001
                    self._notify_transfer_failure(f"处理文件对 {oldp} -> {newp} 时出错", inner_e, level="warn")

            if self._generate_season_posters:
                self._apply_generated_assets(season_posters, transfer_type)
            else:
                logger.debug("已关闭季海报生成, 未执行 _apply_generated_assets")
        except Exception as e:  # noqa: BLE001
            self._notify_transfer_failure("transfer_image_sidecar 执行失败", e, level="error")

    @staticmethod
    def _normalize_transferinfo_key(value):
        """把 TransferInfo 转换成可哈希的稳定键，便于去重.

        这里不仅处理 list/dict/set, 还对不能直接 hash 的对象回退到字符串，避免
        事件签名因为某个属性对象不可哈希而导致整个去重逻辑中断
        """
        if isinstance(value, (str, int, float, bool, type(None))):
            return value
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, (list, tuple)):
            return tuple(TransferImageSidecar._normalize_transferinfo_key(item) for item in value)
        if isinstance(value, dict):
            return tuple(
                sorted((str(k), TransferImageSidecar._normalize_transferinfo_key(v)) for k, v in value.items())
            )
        if isinstance(value, set):
            return tuple(sorted(TransferImageSidecar._normalize_transferinfo_key(item) for item in value))
        try:
            hash(value)
            return value
        except TypeError:
            return repr(value)

    @classmethod
    def _transferinfo_signature(cls, transferinfo: TransferInfo) -> tuple:
        """基于事件内容生成去重签名，确保相同事件不会重复入队."""
        data = getattr(transferinfo, "__dict__", {})
        return tuple(sorted((str(k), cls._normalize_transferinfo_key(v)) for k, v in data.items()))

    def _refresh_delay_timer(self) -> None:
        """重置当前延迟计时器，刷新等待窗口."""
        if self._delayed_timer and self._delayed_timer.is_alive():
            self._delayed_timer.cancel()

        self._delayed_timer = Timer(self._delay_seconds, self._process_delayed_pending_events)
        self._delayed_timer.daemon = True
        self._delayed_timer.start()

    def _process_delayed_pending_events(self) -> None:
        """处理在延迟窗口内累计的多个 TransferComplete 事件.

        在同一批次中，多个事件可能内容完全相同；这些重复事件不需要重复入队，
        但同一批次中新出现的有效事件应刷新当前的等待时间，避免丢失等待窗口
        """
        try:
            pending_events = self._pending_transfer_events
            self._pending_transfer_events = []
            self._delayed_timer = None

            if not pending_events:
                logger.debug("没有待处理的延迟事件")
                return

            logger.debug(f"开始处理 {len(pending_events)} 个累计的 TransferComplete 事件")
            for transferinfo in pending_events:
                self._process_transfer_event(transferinfo)
        except Exception as e:  # noqa: BLE001
            self._notify_transfer_failure("处理累积的延迟事件失败", e, level="error")

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event):
        """处理 TransferComplete 事件: 将执行延后到媒体整理完成后, 并给待刮削内容留出处理窗口."""
        if not self._enabled:
            return
        event_data = event.event_data or {}
        transferinfo: TransferInfo = event_data.get("transferinfo")
        if not transferinfo:
            return
        logger.debug(f"收到 TransferComplete 事件: {transferinfo}")

        signature = self._transferinfo_signature(transferinfo)
        existing_signatures = {self._transferinfo_signature(item) for item in self._pending_transfer_events}

        if signature in existing_signatures:
            logger.debug(
                "忽略重复 TransferComplete 事件, 仅刷新队列等待时间: "
                f"{transferinfo.file_list if getattr(transferinfo, 'file_list', None) else []}"
            )
            if (self._delayed_timer and self._delayed_timer.is_alive()) or self._pending_transfer_events:
                self._refresh_delay_timer()
            return

        self._pending_transfer_events.append(transferinfo)
        logger.debug(
            f"收到 TransferComplete 事件, 当前累计队列大小={len(self._pending_transfer_events)}, "
            f"延迟={self._delay_seconds} 秒"
        )

        if self._delayed_timer and self._delayed_timer.is_alive():
            logger.debug("已有延迟任务在运行, 刷新等待时间并继续累积事件")
            self._refresh_delay_timer()
            return

        self._refresh_delay_timer()
        logger.info(f"transfer_image_sidecar 已延迟 {self._delay_seconds} 秒执行, 等待媒体整理与待刮削项完成")

    @staticmethod
    def get_command() -> list:
        """返回插件远程命令列表."""
        return []

    def get_api(self) -> list:
        """返回插件 API 列表."""
        return []

    def get_form(self):
        """返回插件配置表单与默认配置.

        按功能分组显示：
        - 基础执行参数：启用插件、延迟执行
        - 媒体海报：媒体级 poster/fanart 的来源季/集
        - 季海报：默认按当前季处理, 仅配置来源集编号, 不支持跨季选择
        """
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "delay_seconds",
                                            "label": "延迟秒数执行",
                                            "type": "number",
                                            "suffix": "秒",
                                            "hint": "在媒体整理完成后等待待刮削项完成的延迟时间(\
                                                如果要刮削图片建议延长等待时间以确保图片刮削完成)",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enable_first_episode_promotion",
                                            "label": "启用媒体海报转移",
                                            "hint": "按配置的季/集将 poster/fanart 提升到媒体目录",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "media_poster_season",
                                            "label": "媒体海报季",
                                            "type": "number",
                                            "hint": "例如 1 表示使用第 1 季指定集的封面",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "media_poster_episode",
                                            "label": "媒体海报集",
                                            "type": "number",
                                            "hint": "例如 1 表示使用指定季的第 1 集封面",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "generate_season_posters",
                                            "label": "启用转移季海报",
                                            "hint": "默认按当前季处理, 不支持跨季选择",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "season_poster_episode",
                                            "label": "季海报集",
                                            "type": "number",
                                            "hint": "例如 1 表示使用当前季第 1 集的封面",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "poster_asset_name",
                                            "label": "集封面匹配后缀",
                                            "hint": "用于识别源目录里集封面图的后缀名, 默认 poster",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "fanart_asset_name",
                                            "label": "集艺术图匹配后缀",
                                            "hint": "用于识别源目录里集艺术图的后缀名, 默认 fanart",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "nfo_remove_tags",
                                            "label": "NFO 删除标签",
                                            "hint": "只填 XML 标签名, 多标签用逗号分隔, 例如：genre,actor,plot,演职员",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": True,
            "delay_seconds": 20,
            "enable_first_episode_promotion": True,
            "media_poster_season": 1,
            "media_poster_episode": 1,
            "generate_season_posters": True,
            "season_poster_episode": 1,
            "poster_asset_name": "poster",
            "fanart_asset_name": "fanart",
            "nfo_remove_tags": "",
        }

    def get_page(self):
        """返回插件详情页面."""
        pass

    def stop_service(self):
        """停止插件后台服务并释放资源."""
        return None

    def get_module(self):
        """返回插件模块声明.

        当前插件不覆写 transfer() 钩子, 而是通过 TransferComplete 事件在转移完成后
        统一处理 sidecar、NFO 和季海报的生成, 因此这里返回空字典即可。

        Returns:
            一个空字典, 表示无额外模块注入项。

        """
        # 不覆写 transfer() 钩子；当前插件基于 TransferComplete 事件进行处理
        return {}
