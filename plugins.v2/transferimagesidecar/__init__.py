import os
import re
import shutil
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

    仅针对图片类型处理，避免误删 NFO 或其他非图片文件.
    若同一个 stem 通过 .jpg / .png / .webp / .jpeg 等不同后缀同时存在，
    则在写入前统一清理，保证最终目标位置只有一份图片.

    Args:
        src: 源文件路径，表示要被搬运的文件
        dest: 目标文件路径，表示搬运后的落点

    """
    if src.suffix.lower() not in IMAGE_EXTS and dest.suffix.lower() not in IMAGE_EXTS:
        return

    for ext in sorted(IMAGE_EXTS, key=lambda item: item.lower()):
        candidate = dest.with_suffix(ext)
        if candidate == dest:
            continue
        if candidate.exists() or candidate.is_symlink():
            if candidate.is_dir() and not candidate.is_symlink():
                raise IsADirectoryError(f"目标图片路径是目录，不能覆盖: {candidate}")
            candidate.unlink()

    if dest.exists() or dest.is_symlink():
        if dest.is_dir() and not dest.is_symlink():
            raise IsADirectoryError(f"目标路径是目录，不能覆盖: {dest}")
        dest.unlink()


def _transfer_with_method(src: Path, dest: Path, transfer_type: str) -> None:
    """根据当前转移方式把文件复制到目标目录.

    在实际写入前，会先检查目标位置是否已有同名图片文件；若存在则删除，避免覆盖冲突.

    Args:
        src: 源文件路径，表示要被搬运的文件
        dest: 目标文件路径，表示搬运后的落点
        transfer_type: 传输策略，支持 copy、move、hardlink、symlink

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
def _find_asset_for_stem(dir_path: Path, stem: str, kind: Literal["poster", "fanart"]) -> list[Path] | None: ...
@overload
def _find_asset_for_stem(dir_path: Path, stem: str, kind: Literal["nfo"]) -> Path | None: ...


def _find_asset_for_stem(
    dir_path: Path, stem: str, kind: Literal["poster", "fanart", "nfo"] = "poster"
) -> list[Path] | Path | None:
    """统一查找资产.

    Args:
        dir_path: 要搜索的目录，通常是媒体文件所在目录
        stem: 媒体文件名的主干，通常等于文件的 stem, 不含扩展名
        kind: 要查找的资产类型，支持 poster、fanart 和 nfo

    Returns:
        poster/fanart: 返回匹配到的图片路径列表
        nfo: 返回匹配到的 NFO 路径；未找到时返回 None

    """
    kind = cast(Literal["poster", "fanart", "nfo"], kind.lower())
    if kind in {"poster", "fanart"}:
        found: list[Path] = []
        for sep in SEPARATORS:
            for ext in IMAGE_EXTS:
                candidate = dir_path / f"{stem}{sep}{kind}{ext}"
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
    # 在文件名中查找 SxxExx 格式（例如 S01E03 或 s01e03），返回 (season, episode)
    m = re.search(r"[Ss](\d{1,2})[Ee](\d{1,2})", name)
    if m:
        try:
            season = int(m.group(1))
            episode = int(m.group(2))
            return season, episode
        except Exception:  # noqa: BLE001
            return None
    return None


def _build_renamed_sidecar_path(target_dir: Path, media_path: Path, sidecar_path: Path) -> Path:
    """按媒体文件的目标命名规则重命名剧集 poster 文件.

    Args:
        target_dir: 目标目录，通常是媒体文件的父目录
        media_path: 目标媒体文件路径，用于取其 stem 生成最终文件名
        sidecar_path: 源 sidecar 文件路径，用于提取原始后缀和类型

    Returns:
        最终应写入的目标文件路径

    """
    name_lower = sidecar_path.name.lower()
    for sep in SEPARATORS:
        suffix = f"{sep}poster"
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


class TransferImageSidecar(_PluginBase):  # noqa: D101
    # --- 插件元数据（前端/市场使用） ---
    # 在前端展示的插件名称
    plugin_name = "转移图片挂载文件"
    # 插件描述（可改为中文以便在市场中显示更友好）
    plugin_desc = "将媒体文件同目录下匹配的 fanart/poster 图片和 NFO 文件一并转移，适用于本地文件系统"
    # 插件图标文件名（放在静态资源目录）
    plugin_icon = "movie.jpg"
    # 插件版本（应与 package.v2.json 保持一致）
    plugin_version = "0.1.0"
    # 作者信息
    plugin_author = "cswhrdf"
    # 配置项前缀，用于配置存储中区分本插件项
    plugin_config_prefix = "transfer_image_sidecar_"
    # 插件加载顺序，值越小越早加载
    plugin_order = 25

    # 运行时启用开关，init_plugin 会根据配置覆盖
    _enabled: bool = True
    _delay_seconds: int = 20
    _enable_first_episode_promotion: bool = True
    _media_poster_season: int = 1
    _media_poster_episode: int = 1
    _generate_season_posters: bool = True
    _season_poster_episode: int = 1
    _delayed_timer: Timer | None = None

    def init_plugin(self, config: dict | None = None):
        """初始化插件: 根据传入配置设置启用状态.

        说明: 宿主在加载插件或配置变更时调用此方法。若需要注册事件或启动后台任务，
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

    def get_state(self) -> bool:
        """返回插件当前启用状态.

        Returns:
            True 表示插件已启用, False 表示插件当前被禁用。

        """
        return self._enabled

    # --- 转移处理：配对与流程 ---
    def _resolve_transfer_pairs(self, transferinfo: TransferInfo) -> list[tuple[str | None, str | None]]:
        """按索引将源文件和目标文件配对.

        Args:
            transferinfo: 转移事件传递的上下文对象，包含 file_list 和 file_list_new

        Returns:
            [(source_path, target_path)] 列表，用于一一对应原文件和目标文件

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
        """对 S01E01 这类首集媒体，提升同名 poster/fanart 到媒体级目录.

        这类文件通常会带有同名的 poster/fanart 侧载图，需在目标媒体目录中写出
        `poster.ext` 和 `fanart.ext`，便于宿主端在媒体层直接展示海报与背景图。

        Args:
            source_dir: 源媒体文件所在目录
            src_path: 源媒体文件路径
            new_path: 目标媒体文件路径
            transfer_type: 当前文件转移方式，支持 copy、move、hardlink、symlink

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
            path_list = _find_asset_for_stem(source_dir, src_path.stem, kind)
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
                logger.warn(f"转移媒体级 {kind} 失败 {asset_path} -> {media_dest}: {e}")

    def _transfer_sidecars_and_nfo(
        self,
        oldp: str | None,
        newp: str,
        transfer_type: str,
    ) -> None:
        """将源目录中的 poster/NFO 资源搬移到目标目录.

        Args:
            oldp: 源媒体文件路径。该文件本体必须是媒体文件，不是 sidecar 文件
            newp: 转移后的目标媒体文件路径
            transfer_type: 当前转移方式，影响 poster/NFO 复制方式，可为 copy、move、hardlink、symlink

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

        assets = _find_asset_for_stem(source_dir, stem, "poster")
        if not assets:
            return
        if not isinstance(assets, list):
            assets = [assets]
        for img in assets:
            dest = _build_renamed_sidecar_path(target_dir, new_path, img)
            try:
                _transfer_with_method(img, dest, transfer_type)
                logger.info(f"按 {transfer_type} 方式转移 poster: {img} -> {dest}")
            except Exception as e:  # noqa: BLE001
                logger.warn(f"转移 poster 失败 {img} -> {dest}: {e}")

        nfo = _find_asset_for_stem(source_dir, stem, "nfo")
        if not nfo:
            return
        if isinstance(nfo, list):
            nfo = nfo[0]
        dest_nfo = target_dir / f"{new_path.stem}.nfo"
        try:
            _transfer_with_method(nfo, dest_nfo, "copy")
            logger.info(f"按 copy 方式转移 NFO: {nfo} -> {dest_nfo}")
        except Exception as e:  # noqa: BLE001
            logger.warn(f"转移 NFO 失败 {nfo} -> {dest_nfo}: {e}")

    def _collect_season_posters_and_fanart(
        self,
        source_dir: Path,
        old_path: Path,
        new_path: Path,
        season_posters: dict[int, tuple[int, Path, Path]],
    ) -> dict[int, tuple[int, Path, Path]]:
        """根据目标文件名提取季/集信息，并记录该季最早的 poster 素材.

        Args:
            source_dir: 媒体源文件所在目录
            old_path: 当前已转移好的媒体源文件路径
            new_path: 当前已转移好的目标媒体文件路径
            season_posters: 已累计的季 poster 字典，键为 season 编号，值为 (episode, poster_path)

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
                candidate = source_dir / f"{episode_stem}{sep}poster{ext}"
                if candidate.exists():
                    ep_poster = candidate
                    break
            if ep_poster:
                break

        if ep_poster:
            target_episode = self._season_poster_episode
            if target_episode <= 0:
                target_episode = 1

            if episode == target_episode:
                season_posters[season] = (episode, ep_poster, new_path.parent)
            elif season not in season_posters:
                prev = season_posters.get(season)
                if prev is None or episode < prev[0]:
                    season_posters[season] = (episode, ep_poster, new_path.parent)

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
            season_posters: 季 poster 素材字典，键为 season 编号，值为 (episode, poster_path)
            transfer_type: 生成时使用的传输方式，例如 copy、move、hardlink、symlink

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
                logger.warn(f"设置第 {season} 季海报失败: {e}")

    def _process_transfer_event(self, transferinfo: TransferInfo) -> None:
        """延迟处理转移完成事件，并在等待窗口结束后执行 sidecar 与季 poster 整理.

        Args:
            transferinfo: 转移事件对象，包含 file_list、file_list_new 和 transfer_type

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
                    logger.warn(f"处理文件对 {oldp} -> {newp} 时出错: {inner_e}")

            if self._generate_season_posters:
                self._apply_generated_assets(season_posters, transfer_type)
            else:
                logger.debug("已关闭季海报生成，未执行 _apply_generated_assets")
        except Exception as e:  # noqa: BLE001
            logger.error(f"transfer_image_sidecar 执行失败: {e}")

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event):
        """处理 TransferComplete 事件: 将执行延后到媒体整理完成后，并给待刮削内容留出处理窗口."""
        if not self._enabled:
            return
        event_data = event.event_data or {}
        transferinfo: TransferInfo = event_data.get("transferinfo")
        if not transferinfo:
            return

        if self._delayed_timer and self._delayed_timer.is_alive():
            self._delayed_timer.cancel()

        self._delayed_timer = Timer(self._delay_seconds, self._process_transfer_event, args=(transferinfo,))
        self._delayed_timer.daemon = True
        self._delayed_timer.start()
        logger.info(f"transfer_image_sidecar 已延迟 {self._delay_seconds} 秒执行，等待媒体整理与待刮削项完成")

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
        - 季海报：默认按当前季处理，仅配置来源集编号，不支持跨季选择
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
                                            "label": "延迟执行秒数",
                                            "type": "number",
                                            "suffix": "秒",
                                            "hint": "等待媒体整理与待刮削项完成的延迟时间",
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
                                            "hint": "例如 1 表示第 1 季",
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
                                            "hint": "例如 1 表示第 1 集",
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
                                            "hint": "默认按当前季处理，不支持跨季选择",
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
                                            "hint": "例如 1 表示当前季第 1 集",
                                        },
                                    }
                                ],
                            },
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
        }

    def get_page(self):
        """返回插件详情页面."""
        pass

    def stop_service(self):
        """停止插件后台服务并释放资源."""
        return None

    def get_module(self):
        """返回插件模块声明.

        当前插件不覆写 transfer() 钩子，而是通过 TransferComplete 事件在转移完成后
        统一处理 sidecar、NFO 和季海报的生成，因此这里返回空字典即可。

        Returns:
            一个空字典，表示无额外模块注入项。

        """
        # 不覆写 transfer() 钩子；当前插件基于 TransferComplete 事件进行处理
        return {}
