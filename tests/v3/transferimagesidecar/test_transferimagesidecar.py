"""媒体刮削文件转移（TransferImageSidecar）V3 实现的合同与纯逻辑单测。

按官方测试规范：通过生产命名空间 app.plugins.transferimagesidecar 导入插件
（tests/conftest.py 负责注入后端与 plugins.v3/ 插件目录），并用 object.__new__
绕过插件构造，只覆盖不依赖运行时组合根的合同与纯逻辑。
"""
from __future__ import annotations

import ast
import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "plugins.v3/transferimagesidecar/__init__.py"
PLUGIN_ID = "TransferImageSidecar"

LEGACY_MODULE_PREFIXES = ("app.core.", "app.helper.", "app.utils.", "app.db.models")
LEGACY_MODULES = {"app.log", "app.plugins"}
LEGACY_SYMBOLS = ("SessionFactory", "AsyncSessionFactory", "NotificationType")


def _load_plugin():
    """按生产命名空间导入 V3 插件模块。"""
    return importlib.import_module("app.plugins.transferimagesidecar")


def _make_plugin(module):
    """绕过运行时组装实例化插件，并补齐纯逻辑方法依赖的状态。"""
    plugin = object.__new__(module.TransferImageSidecar)
    plugin._enabled = True
    plugin._delay_seconds = 0
    plugin._enable_first_episode_promotion = True
    plugin._media_poster_season = 1
    plugin._media_poster_episode = 1
    plugin._generate_season_posters = True
    plugin._season_poster_episode = 1
    plugin._poster_asset_name = "poster"
    plugin._fanart_asset_name = "fanart"
    plugin._nfo_remove_tags = set()
    plugin._delayed_timer = None
    plugin._pending_transfer_events = []
    return plugin


def _imported_modules() -> list[str]:
    """返回插件源码中出现的所有导入模块名。"""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return modules


def _is_placeholder_stmt(stmt: ast.stmt) -> bool:
    """复刻宿主 ObjectUtils 的占位语句判定。"""
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
    """判断指定方法的方法体是否全部由占位语句组成。"""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    for cls in [node for node in tree.body if isinstance(node, ast.ClassDef)]:
        if cls.name != "TransferImageSidecar":
            continue
        for fn in cls.body:
            if isinstance(fn, ast.FunctionDef) and fn.name == method_name:
                return all(_is_placeholder_stmt(stmt) for stmt in fn.body)
    raise AssertionError(f"未找到方法 {method_name}")


# ---------------- 编制与索引合同 ----------------


def test_v3_metadata_and_indexes_are_aligned():
    """插件版本、V3 索引条目与旧索引禁用标记必须一致。"""
    module = _load_plugin()
    v3 = json.loads((ROOT / "package.v3.json").read_text(encoding="utf-8"))
    v2 = json.loads((ROOT / "package.v2.json").read_text(encoding="utf-8"))
    entry = v3[PLUGIN_ID]

    assert module.TransferImageSidecar.plugin_version == "1.0.0"
    assert entry["version"] == "1.0.0"
    assert entry["system_version"] == ">=3.0.0"
    assert next(iter(entry["history"])) == "1.0.0"
    # 门禁要求 V3 版本等于旧代主版本 + 1 并归零
    assert entry["version"] == f"{int(v2[PLUGIN_ID]['version'].split('.')[0]) + 1}.0.0"
    assert v2[PLUGIN_ID]["v3"] is False


def test_plugin_metadata_matches_market_entry():
    """插件类自报元数据应与市场索引展示字段保持一致。"""
    module = _load_plugin()
    entry = json.loads((ROOT / "package.v3.json").read_text(encoding="utf-8"))[PLUGIN_ID]
    plugin_class = module.TransferImageSidecar

    assert plugin_class.plugin_name == entry["name"]
    assert plugin_class.plugin_desc == entry["description"]
    assert plugin_class.plugin_icon == entry["icon"]
    assert plugin_class.plugin_author == entry["author"]
    assert plugin_class.plugin_config_prefix == "transfer_image_sidecar_"


def test_plugin_only_uses_stable_v3_imports():
    """V3 专用实现不得再依赖旧导入路径与宿主内部 Model。"""
    modules = _imported_modules()
    for name in modules:
        assert not name.startswith(LEGACY_MODULE_PREFIXES), name
        assert name not in LEGACY_MODULES, name

    text = SOURCE.read_text(encoding="utf-8")
    for symbol in LEGACY_SYMBOLS:
        assert symbol not in text, symbol
    assert any(name.startswith("app.sdk.") for name in modules)


# ---------------- 生命周期与页面合同 ----------------


def test_plugin_registers_no_api_command_or_module():
    """插件不注册 API/命令/模块，启用状态跟随配置。"""
    plugin = _make_plugin(_load_plugin())

    assert plugin.get_api() == []
    assert plugin.get_command() == []
    assert plugin.get_module() == {}
    assert plugin.get_state() is True

    plugin._enabled = False
    assert plugin.get_state() is False


def test_get_page_stays_placeholder_so_host_hides_detail_page():
    """没有详情页时必须保持占位方法体，否则宿主会放出数据页入口。"""
    plugin = _make_plugin(_load_plugin())

    assert plugin.get_page() is None
    # 宿主按「方法体是否全为占位语句」判定 has_page，写成 return None 会被视为已实现
    assert _method_body_is_placeholder("get_page") is True


def test_init_plugin_applies_config():
    """初始化应完整读取配置项。"""
    plugin = _make_plugin(_load_plugin())
    plugin.init_plugin({
        "enabled": False,
        "delay_seconds": 45,
        "enable_first_episode_promotion": False,
        "media_poster_season": 2,
        "media_poster_episode": 3,
        "generate_season_posters": False,
        "season_poster_episode": 4,
        "poster_asset_name": "cover",
        "fanart_asset_name": "backdrop",
        "nfo_remove_tags": ["genre", "Actor"],
    })

    assert plugin.get_state() is False
    assert plugin._delay_seconds == 45
    assert plugin._enable_first_episode_promotion is False
    assert plugin._media_poster_season == 2
    assert plugin._media_poster_episode == 3
    assert plugin._generate_season_posters is False
    assert plugin._season_poster_episode == 4
    assert plugin._poster_asset_name == "cover"
    assert plugin._fanart_asset_name == "backdrop"
    assert plugin._nfo_remove_tags == {"genre", "actor"}


def test_init_plugin_accepts_legacy_comma_string():
    """旧配置是逗号分隔字符串，必须继续被解析成标签集合。"""
    plugin = _make_plugin(_load_plugin())
    plugin.init_plugin({"nfo_remove_tags": "genre, actor，tag"})

    assert plugin._nfo_remove_tags == {"genre", "actor", "tag"}


def test_init_plugin_without_config_keeps_defaults():
    """未传入配置时不得抛异常，状态回到默认值。"""
    plugin = _make_plugin(_load_plugin())
    plugin.init_plugin(None)

    assert plugin.get_state() is True
    assert plugin._delay_seconds == 20
    assert plugin._nfo_remove_tags == set()


def test_init_plugin_is_repeatable_and_releases_previous_timer():
    """配置重载时必须取消上一轮计时器并清空待处理队列。"""
    plugin = _make_plugin(_load_plugin())

    class _FakeTimer:
        """只记录 cancel 调用的计时器替身。"""

        def __init__(self):
            self.cancelled = False
            self._alive = True

        def is_alive(self):
            return self._alive

        def cancel(self):
            self.cancelled = True
            self._alive = False

    timer = _FakeTimer()
    plugin._delayed_timer = timer
    plugin._pending_transfer_events = [SimpleNamespace(file_list=["a"])]

    plugin.init_plugin({"delay_seconds": 10})

    assert timer.cancelled is True
    assert plugin._delayed_timer is None
    assert plugin._pending_transfer_events == []
    assert plugin._delay_seconds == 10


# ---------------- 纯解析逻辑 ----------------


def test_parse_season_episode_reads_sxxexx():
    """季集解析支持大小写 SxxExx，无法识别时返回 None。"""
    module = _load_plugin()

    assert module._parse_season_episode("Show.S01E02.mkv") == (1, 2)
    assert module._parse_season_episode("show.s12e34.mkv") == (12, 34)
    assert module._parse_season_episode("Show.2020.mkv") is None


def test_parse_nfo_remove_tags_accepts_all_stored_shapes():
    """标签解析兼容逗号字符串、列表、对象与命名空间前缀。"""
    module = _load_plugin()
    parse = module._parse_nfo_remove_tags

    assert parse(None) == set()
    assert parse("  ") == set()
    assert parse("genre,actor") == {"genre", "actor"}
    assert parse("genre，actor plot") == {"genre", "actor", "plot"}
    assert parse(["Genre", " tag "]) == {"genre", "tag"}
    assert parse(["{http://x}tag"]) == {"tag"}
    assert parse([{"title": "剧情", "value": "plot"}]) == {"plot"}
    assert parse(["a", "", None]) == {"a"}


def test_local_tag_name_strips_namespace_only():
    """命名空间前缀被剥离，普通标签名原样返回。"""
    module = _load_plugin()

    assert module._local_tag_name("{http://x}genre") == "genre"
    assert module._local_tag_name("genre") == "genre"


def test_is_media_file_excludes_sidecars():
    """sidecar 与 NFO 不算媒体文件本体。"""
    module = _load_plugin()

    assert module._is_media_file(Path("Show.mkv")) is True
    assert module._is_media_file(Path("Show-poster.JPG")) is False
    assert module._is_media_file(Path("Show.nfo")) is False


# ---------------- 资产匹配与命名 ----------------


def test_find_asset_for_stem_collects_every_poster_variant(tmp_path):
    """poster 允许 -/_/空格/. 分隔与多种图片后缀，NFO 只取同名单个文件。"""
    module = _load_plugin()
    stem = "Show.S01E01"
    for name in (f"{stem}-poster.jpg", f"{stem}_poster.png", f"{stem}.poster.webp", f"{stem}.nfo"):
        (tmp_path / name).write_bytes(b"x")

    posters = module._find_asset_for_stem(tmp_path, stem, "poster", "poster")
    assert isinstance(posters, list)
    assert {item.name for item in posters} == {
        f"{stem}-poster.jpg",
        f"{stem}_poster.png",
        f"{stem}.poster.webp",
    }

    nfo = module._find_asset_for_stem(tmp_path, stem, "nfo")
    assert nfo is not None
    assert nfo.name == f"{stem}.nfo"


def test_find_asset_for_stem_supports_custom_asset_name(tmp_path):
    """自定义后缀名生效，缺省名称不参与匹配。"""
    module = _load_plugin()
    (tmp_path / "Show.S01E01-cover.jpg").write_bytes(b"x")
    (tmp_path / "Show.S01E01-poster.jpg").write_bytes(b"x")

    covers = module._find_asset_for_stem(tmp_path, "Show.S01E01", "poster", "cover")

    assert [item.name for item in covers] == ["Show.S01E01-cover.jpg"]


def test_find_asset_for_stem_returns_empty_when_missing(tmp_path):
    """没有匹配资产时 poster 返回空列表，NFO 返回 None。"""
    module = _load_plugin()

    assert module._find_asset_for_stem(tmp_path, "Missing", "poster", "poster") == []
    assert module._find_asset_for_stem(tmp_path, "Missing", "nfo") is None


def test_build_renamed_sidecar_path_uses_target_media_stem(tmp_path):
    """侧载图最终以目标媒体文件的 stem 命名。"""
    module = _load_plugin()
    media = tmp_path / "Show.S01E01.mkv"
    sidecar = tmp_path / "Show.S01E01-poster.jpg"

    renamed = module._build_renamed_sidecar_path(tmp_path, media, sidecar, "poster")

    assert renamed == tmp_path / "Show.S01E01.jpg"


# ---------------- 文件写入方式 ----------------


def test_transfer_with_method_copy_keeps_source(tmp_path):
    """copy 方式保留源文件并复制内容。"""
    module = _load_plugin()
    src = tmp_path / "a.mkv"
    src.write_bytes(b"payload")
    dest = tmp_path / "out" / "a.mkv"
    dest.parent.mkdir()

    module._transfer_with_method(src, dest, "copy")

    assert src.exists()
    assert dest.read_bytes() == b"payload"


def test_transfer_with_method_move_removes_source(tmp_path):
    """move 方式搬走源文件。"""
    module = _load_plugin()
    src = tmp_path / "a.mkv"
    src.write_bytes(b"payload")
    dest = tmp_path / "a.mkv.bak"

    module._transfer_with_method(src, dest, "move")

    assert not src.exists()
    assert dest.read_bytes() == b"payload"


def test_transfer_with_method_hardlink_shares_inode(tmp_path):
    """hardlink 与别名 link/hard 等价，共享同一 inode。"""
    module = _load_plugin()
    src = tmp_path / "a.mkv"
    src.write_bytes(b"payload")
    dest = tmp_path / "b.mkv"

    module._transfer_with_method(src, dest, "link")

    assert os.stat(src).st_ino == os.stat(dest).st_ino


def test_transfer_with_method_symlink_points_to_source(tmp_path):
    """symlink 生成指向源文件的链接，环境不支持时跳过。"""
    module = _load_plugin()
    src = tmp_path / "a.mkv"
    src.write_bytes(b"payload")
    dest = tmp_path / "b.mkv"

    try:
        module._transfer_with_method(src, dest, "symlink")
    except OSError:
        pytest.skip("当前环境不支持创建符号链接")

    assert dest.is_symlink()
    assert dest.read_bytes() == b"payload"


def test_transfer_with_method_drops_conflicting_image_suffix(tmp_path):
    """写入新图片前清掉同 stem 的其它图片后缀，避免一份图片出现多份。"""
    module = _load_plugin()
    src = tmp_path / "src.png"
    src.write_bytes(b"new")
    stale = tmp_path / "poster.jpg"
    stale.write_bytes(b"old")

    module._transfer_with_method(src, tmp_path / "poster.png", "copy")

    assert not stale.exists()
    assert (tmp_path / "poster.png").read_bytes() == b"new"


def test_transfer_with_method_rejects_directory_target(tmp_path):
    """目标路径是目录时抛 IsADirectoryError，不得静默覆盖。"""
    module = _load_plugin()
    src = tmp_path / "src.png"
    src.write_bytes(b"new")
    (tmp_path / "poster.png").mkdir()

    with pytest.raises(IsADirectoryError):
        module._transfer_with_method(src, tmp_path / "poster.png", "copy")


# ---------------- NFO 标签过滤 ----------------


def test_rewrite_nfo_xml_removes_configured_tags(tmp_path):
    """只删除配置中的标签，其余结构与内容保留。"""
    module = _load_plugin()
    source = tmp_path / "a.nfo"
    source.write_text(
        "<movie><title>A</title><genre>Action</genre><actor><name>X</name></actor></movie>",
        encoding="utf-8",
    )
    dest = tmp_path / "out" / "a.nfo"

    module._rewrite_nfo_xml(source, dest, {"genre", "actor"})

    assert dest.exists()
    assert "genre" not in dest.read_text(encoding="utf-8")
    assert "actor" not in dest.read_text(encoding="utf-8")
    assert "<title>A</title>" in dest.read_text(encoding="utf-8")


def test_rewrite_nfo_xml_matches_namespaced_tags(tmp_path):
    """带命名空间的标签按本地名匹配。"""
    module = _load_plugin()
    source = tmp_path / "a.nfo"
    source.write_text(
        '<movie xmlns:t="http://x"><t:genre>g</t:genre><title>A</title></movie>',
        encoding="utf-8",
    )
    dest = tmp_path / "a.out.nfo"

    module._rewrite_nfo_xml(source, dest, {"genre"})

    text = dest.read_text(encoding="utf-8")
    assert "genre" not in text
    assert "<title>A</title>" in text


def test_rewrite_nfo_xml_without_tags_keeps_content(tmp_path):
    """未配置删除标签时仍写出一份合法 XML。"""
    module = _load_plugin()
    source = tmp_path / "a.nfo"
    source.write_text("<movie><genre>Action</genre></movie>", encoding="utf-8")
    dest = tmp_path / "a.out.nfo"

    module._rewrite_nfo_xml(source, dest, set())

    assert "<genre>Action</genre>" in dest.read_text(encoding="utf-8")


# ---------------- 转移流程 ----------------


def test_transfer_sidecars_and_nfo_copies_poster_and_filtered_nfo(tmp_path):
    """同时存在 poster 与 NFO 时，两者都会被整理到目标目录。"""
    plugin = _make_plugin(_load_plugin())
    plugin._nfo_remove_tags = {"genre"}

    source_dir = tmp_path / "downloads"
    source_dir.mkdir()
    target_dir = tmp_path / "library" / "Show" / "Season 01"
    target_dir.mkdir(parents=True)

    media = source_dir / "Show.S01E01.mkv"
    media.write_bytes(b"video")
    (source_dir / "Show.S01E01-poster.jpg").write_bytes(b"img")
    (source_dir / "Show.S01E01.nfo").write_text(
        "<movie><title>A</title><genre>Action</genre></movie>", encoding="utf-8"
    )
    new_media = target_dir / "Show.S01E01.mkv"

    plugin._transfer_sidecars_and_nfo(str(media), str(new_media), "copy")

    assert (target_dir / "Show.S01E01.jpg").read_bytes() == b"img"
    nfo_text = (target_dir / "Show.S01E01.nfo").read_text(encoding="utf-8")
    assert "<title>A</title>" in nfo_text
    assert "genre" not in nfo_text


def test_promote_media_assets_can_be_switched_off(tmp_path):
    """关闭媒体海报转移后，不得在媒体目录写出 poster/fanart。"""
    plugin = _make_plugin(_load_plugin())
    plugin._enable_first_episode_promotion = False

    source_dir = tmp_path / "downloads"
    source_dir.mkdir()
    (source_dir / "Show.S01E01.mkv").write_bytes(b"video")
    (source_dir / "Show.S01E01-poster.jpg").write_bytes(b"img")

    show_dir = tmp_path / "library" / "Show"
    season_dir = show_dir / "Season 01"
    season_dir.mkdir(parents=True)

    plugin._promote_first_episode_assets(
        source_dir,
        source_dir / "Show.S01E01.mkv",
        season_dir / "Show.S01E01.mkv",
        "copy",
    )

    assert not (show_dir / "poster.jpg").exists()


def test_promote_media_assets_writes_series_level_poster(tmp_path):
    """命中所配置季/集时，poster/fanart 提升到媒体目录。"""
    plugin = _make_plugin(_load_plugin())

    source_dir = tmp_path / "downloads"
    source_dir.mkdir()
    (source_dir / "Show.S01E01.mkv").write_bytes(b"video")
    (source_dir / "Show.S01E01-poster.jpg").write_bytes(b"img")
    (source_dir / "Show.S01E01-fanart.png").write_bytes(b"bg")

    show_dir = tmp_path / "library" / "Show"
    season_dir = show_dir / "Season 01"
    season_dir.mkdir(parents=True)

    plugin._promote_first_episode_assets(
        source_dir,
        source_dir / "Show.S01E01.mkv",
        season_dir / "Show.S01E01.mkv",
        "copy",
    )

    assert (show_dir / "poster.jpg").read_bytes() == b"img"
    assert (show_dir / "fanart.png").read_bytes() == b"bg"


def test_collect_and_apply_season_poster(tmp_path):
    """季海报取配置集素材，同时写出 Season 目录 poster 与 seasonXX-poster。"""
    plugin = _make_plugin(_load_plugin())
    plugin._season_poster_episode = 1

    source_dir = tmp_path / "downloads"
    source_dir.mkdir()
    (source_dir / "Show.S01E01.mkv").write_bytes(b"video")
    (source_dir / "Show.S01E01-poster.jpg").write_bytes(b"img")

    season_dir = tmp_path / "library" / "Show" / "Season 01"
    season_dir.mkdir(parents=True)

    collected = plugin._collect_season_posters_and_fanart(
        source_dir,
        source_dir / "Show.S01E01.mkv",
        season_dir / "Show.S01E01.mkv",
        {},
    )
    plugin._apply_generated_assets(collected, "copy")

    assert (season_dir / "poster.jpg").read_bytes() == b"img"
    assert (season_dir.parent / "season01-poster.jpg").read_bytes() == b"img"


# ---------------- 事件去重 ----------------


def test_transferinfo_signature_dedupes_identical_events():
    """内容相同的事件签名一致，任一字段变化即视为新事件。"""
    module = _load_plugin()
    signature = module.TransferImageSidecar._transferinfo_signature

    first = SimpleNamespace(file_list=["a.mkv"], file_list_new=["b.mkv"], transfer_type="copy")
    same = SimpleNamespace(file_list=["a.mkv"], file_list_new=["b.mkv"], transfer_type="copy")
    different = SimpleNamespace(file_list=["a.mkv"], file_list_new=["c.mkv"], transfer_type="copy")

    assert signature(first) == signature(same)
    assert signature(first) != signature(different)


def test_transferinfo_signature_tolerates_unhashable_values():
    """不可哈希属性回退为字符串，不得让去重逻辑整体中断。"""
    module = _load_plugin()
    signature = module.TransferImageSidecar._transferinfo_signature

    payload = SimpleNamespace(file_list=["a.mkv"], extra=object(), transfer_type="copy")

    assert isinstance(signature(payload), tuple)
