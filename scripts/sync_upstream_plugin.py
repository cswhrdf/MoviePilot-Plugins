#!/usr/bin/env python3
"""同步上游 remoteidentifiers 插件并重放免代理补丁.

脚本定期从 ``upstream/main`` 读取插件源码、v1 索引条目与图标，校验版本契约后同步到本仓库，
局部更新 ``package.json`` 的单个条目，并调用补丁脚本。所有校验通过后把摘要写入 PR 描述文件；
任一失败即退出，不生成可合并的同步 PR。
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PLUGIN_ID = "RemoteIdentifiers"
PLUGIN_DIR = "remoteidentifiers"
UPSTREAM_PLUGIN_DIR = f"plugins/{PLUGIN_DIR}"
INDEX_PATH = "package.json"
README_PATH = "README.md"
UPSTREAM_REMOTE = "upstream"
UPSTREAM_BRANCH = "main"
_VERSION_PATTERN = re.compile(r"v?(\d+(?:\.\d+)*)")

FORK_OVERRIDES: dict[str, Any] = {
    "name": "共享识别词(个人版)",
    "v2": True,
    "v3": True,
}


class SyncError(RuntimeError):
    """同步流程中的可预期失败."""


@dataclass(frozen=True)
class CheckResult:
    """一次校验命令的结果."""

    name: str
    status: str
    output: str


def semantic_version(value: object) -> tuple[int, ...] | None:
    """把版本文本解析为数字元组，兼容可选 ``v`` 前缀."""
    match = _VERSION_PATTERN.fullmatch(str(value or "").strip())
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def compare_versions(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    """按语义版本比较两个数字元组，缺失的小版本段按零处理."""
    width = max(len(left), len(right), 3)
    normalized_left = left + (0,) * (width - len(left))
    normalized_right = right + (0,) * (width - len(right))
    return (normalized_left > normalized_right) - (normalized_left < normalized_right)


def plugin_version_from_source(source: str) -> str | None:
    """从插件源码的类级属性中提取 ``plugin_version`` 字面量."""
    tree = ast.parse(source)
    for class_node in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        for node in class_node.body:
            value_node = None
            if isinstance(node, ast.Assign):
                if any(isinstance(target, ast.Name) and target.id == "plugin_version" for target in node.targets):
                    value_node = node.value
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "plugin_version"
            ):
                value_node = node.value
            if isinstance(value_node, ast.Constant) and isinstance(value_node.value, str):
                return value_node.value
    return None


def validate_upstream_contract(source: str, entry: dict[str, Any]) -> list[str]:
    """校验上游源码与索引条目的版本契约，返回全部错误文本."""
    errors: list[str] = []
    version = str(entry.get("version") or "").strip()
    parsed_version = semantic_version(version)
    if parsed_version is None:
        errors.append(f"上游索引版本 {version or '<空>'} 不是合法语义版本")
    source_version = plugin_version_from_source(source)
    if not source_version:
        errors.append("上游插件源码缺少类级 plugin_version")
    elif source_version != version:
        errors.append(f"上游 plugin_version={source_version} 与索引 version={version} 不一致")

    history = entry.get("history")
    if not isinstance(history, dict) or not history:
        errors.append("上游索引条目缺少非空 history")
        return errors

    keys = list(history)
    parsed = [semantic_version(key) for key in keys]
    for key, item in zip(keys, parsed, strict=True):
        if item is None:
            errors.append(f"上游 history 包含非法版本键 {key}")
    if parsed_version and parsed[0] and compare_versions(parsed[0], parsed_version) != 0:
        errors.append(f"上游 history 首项 {keys[0]} 与当前版本 {version} 语义不一致")
    for previous_key, previous, current_key, current in zip(keys, parsed, keys[1:], parsed[1:], strict=False):
        if previous and current and compare_versions(previous, current) <= 0:
            errors.append(f"上游 history 未按语义版本严格降序：{previous_key} 在 {current_key} 之前")
    return errors


def merge_index_entry(upstream_entry: dict[str, Any], local_entry: dict[str, Any] | None) -> dict[str, Any]:
    """合并上游元数据与本地额外字段，并应用 fork 专属覆盖（个人版命名与 v2/v3 兼容标记）."""
    merged = dict(upstream_entry)
    for key, value in (local_entry or {}).items():
        if key not in merged and key != "v3":
            merged[key] = value
    merged.update(FORK_OVERRIDES)
    return merged


def _skip_json_string(text: str, index: int) -> int:
    """返回从引号开始扫描到的字符串结束后的下标."""
    escaped = False
    for position in range(index + 1, len(text)):
        char = text[position]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            return position + 1
    raise SyncError("索引字符串未闭合")


def _find_json_value_end(text: str, start: int) -> int:
    """从值起始位置扫描到对象/数组结束，字符串内的括号不参与计数."""
    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if char == '"':
            index = _skip_json_string(text, index)
            continue
        if char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth == 0:
                return index + 1
        elif depth == 0 and char in ",\n":
            return index
        index += 1
    raise SyncError("无法定位索引条目的结束位置")


def replace_index_entry_text(raw_text: str, key: str, entry: dict[str, Any]) -> str:
    """局部插入或替换索引条目文本，不做整体格式化或键顺序重排."""
    encoded_key = json.dumps(key, ensure_ascii=False)
    entry_text = json.dumps(entry, ensure_ascii=False, indent=2).replace("\n", "\n  ")
    key_index = raw_text.find(encoded_key)
    if key_index != -1:
        colon = raw_text.find(":", key_index + len(encoded_key))
        if colon == -1:
            raise SyncError(f"索引条目 {key} 后缺少冒号")
        value_start = colon + 1
        while value_start < len(raw_text) and raw_text[value_start] in " \t\r\n":
            value_start += 1
        value_end = _find_json_value_end(raw_text, value_start)
        return raw_text[:value_start] + entry_text + raw_text[value_end:]

    stripped = raw_text.rstrip()
    if stripped == "{}":
        return "{\n  " + encoded_key + ": " + entry_text + "\n}\n"
    close_index = stripped.rfind("}")
    prefix = stripped[:close_index].rstrip()
    if not prefix.endswith(("{", ",")):
        prefix += ","
    return prefix + "\n  " + encoded_key + ": " + entry_text + "\n}\n"


def ensure_readme_entry(text: str, entry: dict[str, Any]) -> str:
    """首次导入时在根 README 的「插件清单」中补齐条目."""
    link = f"{UPSTREAM_PLUGIN_DIR}/README.md"
    if link in text:
        return text
    heading = "## 插件清单"
    heading_index = text.find(heading)
    if heading_index == -1:
        raise SyncError("根 README.md 缺少「插件清单」小节")
    insert_at = text.find("\n", heading_index)
    insert_at = len(text) if insert_at == -1 else insert_at + 1
    while insert_at < len(text) and text[insert_at] in "\r\n":
        insert_at += 1
    name = str(entry.get("name") or PLUGIN_ID)
    description = str(entry.get("description") or "").strip()
    block = f"### [{name}]({link})\n\n{description}\n\n"
    return text[:insert_at] + block + text[insert_at:]


def _run_git(repo_root: Path, *args: str) -> str:
    """执行 git 命令并返回标准输出，失败时抛出 SyncError."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=False,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise SyncError(f"git {' '.join(args)} 失败：{detail}")
    return result.stdout.decode("utf-8", errors="replace")


def _show_bytes(repo_root: Path, ref: str, path: str) -> bytes:
    """读取指定 revision 中的文件二进制内容."""
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=repo_root,
        capture_output=True,
        text=False,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise SyncError(f"读取上游文件 {path} 失败：{detail}")
    return result.stdout


def _list_upstream_files(repo_root: Path, ref: str) -> list[str]:
    """列出上游插件目录下的全部文件."""
    output = _run_git(repo_root, "ls-tree", "-r", "--name-only", ref, UPSTREAM_PLUGIN_DIR)
    return [line.strip() for line in output.splitlines() if line.strip()]


def _write_if_changed(path: Path, data: bytes) -> bool:
    """内容不同才写入文件，返回是否产生变更."""
    if path.exists() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return True


def _icon_case_conflict(icons_dir: Path, icon: str) -> str | None:
    """返回与目标图标仅大小写不同的现存文件名."""
    if not icons_dir.is_dir():
        return None
    for candidate in icons_dir.iterdir():
        if candidate.name.lower() == icon.lower() and candidate.name != icon:
            return candidate.name
    return None


def backend_available(repo_root: Path) -> bool:
    """判断 MoviePilot 后端是否可用于运行 tests/v1."""
    candidates: list[Path] = []
    raw_env = os.environ.get("MOVIEPILOT_BACKEND_PATH")
    if raw_env:
        candidates.append(Path(raw_env).expanduser())
    candidates.append(repo_root.parent / "MoviePilot")
    return any((candidate / "app").is_dir() for candidate in candidates)


def _run_check(repo_root: Path, name: str, command: list[str]) -> CheckResult:
    """执行一条校验命令，返回状态与截断后的输出."""
    result = subprocess.run(command, cwd=repo_root, capture_output=True, text=True, check=False)
    output = (result.stdout + result.stderr).strip()
    if len(output) > 1200:
        output = output[:1200] + "\n...（输出已截断）"
    status = "passed" if result.returncode == 0 else "failed"
    return CheckResult(name=name, status=status, output=output)


def _collect_checks(repo_root: Path) -> list[CheckResult]:
    """运行同步约定的全部校验；后端缺失时明确跳过 tests/v1."""
    commands = [
        ("ruff check", ["uv", "run", "ruff", "check", "."]),
        ("ruff format --check", ["uv", "run", "ruff", "format", "--check", "."]),
        ("pyright", ["uv", "run", "pyright"]),
        (
            "plugin version gate",
            [
                "uv",
                "run",
                "python",
                ".github/scripts/check_plugin_versions.py",
                "package.json",
                "package.v2.json",
                "package.v3.json",
            ],
        ),
        ("git diff --check", ["git", "diff", "--check"]),
    ]
    if backend_available(repo_root):
        commands.insert(3, ("pytest tests/v1", ["uv", "run", "pytest", "tests/v1"]))
    checks = [_run_check(repo_root, name, command) for name, command in commands]
    if not backend_available(repo_root):
        checks.append(
            CheckResult(
                name="pytest tests/v1",
                status="skipped",
                output="未找到 MoviePilot 后端（同级 MoviePilot/app 或 MOVIEPILOT_BACKEND_PATH），按设计跳过。",
            )
        )
    return checks


def _patch_summary(patch_result: Any) -> str:
    """把补丁结果转换为 PR 描述中的一行说明."""
    if patch_result.status == "upstream-bypass":
        return "上游已提供等效代理绕过实现，请人工评估关闭本补丁。"
    if patch_result.status == "unchanged":
        return "补丁已处于目标状态，无新增 diff。"
    return "已应用补丁组：" + "、".join(patch_result.applied)


def _build_body(
    version: str,
    patch_result: Any,
    changes: list[str],
    checks: list[CheckResult],
) -> str:
    """生成固定格式的 PR 描述正文."""
    lines = [
        "## 上游同步",
        "",
        f"- 上游版本：`{version}`",
        f"- 补丁状态：{_patch_summary(patch_result)}",
        "- 变更文件：" + ("、".join(f"`{path}`" for path in changes) if changes else "无（上游无变化且本地已同步）"),
        "",
        "### 校验",
        "",
        "| 检查 | 结果 |",
        "| --- | --- |",
    ]
    for check in checks:
        lines.append(f"| {check.name} | {check.status} |")
    failed = [check for check in checks if check.status == "failed"]
    if failed:
        lines.extend(["", "### 失败输出", ""])
        for check in failed:
            lines.extend([f"**{check.name}**", "", "```text", check.output, "```", ""])
    skipped = [check for check in checks if check.status == "skipped"]
    if skipped:
        lines.extend(["", "### 跳过项", ""])
        for check in skipped:
            lines.append(f"- {check.name}：{check.output}")
    lines.extend(["", "本 PR 由定时同步脚本生成；合并前请核对变更范围、版本契约与补丁 diff。"])
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class UpstreamSnapshot:
    """上游插件目录、源码、索引条目与图标的只读快照."""

    files: list[str]
    source_path: str
    source: str
    entry: dict[str, Any]
    icon: str
    icon_bytes: bytes


def _load_upstream(repo_root: Path, ref: str) -> UpstreamSnapshot:
    """读取并校验上游插件目录、源码、索引条目与图标."""
    files = _list_upstream_files(repo_root, ref)
    if not files:
        raise SyncError(f"上游 {ref} 中未找到 {UPSTREAM_PLUGIN_DIR}")
    source_path = f"{UPSTREAM_PLUGIN_DIR}/__init__.py"
    source = _show_bytes(repo_root, ref, source_path).decode("utf-8")
    entry = json.loads(_show_bytes(repo_root, ref, INDEX_PATH).decode("utf-8")).get(PLUGIN_ID)
    if not isinstance(entry, dict):
        raise SyncError(f"上游 {INDEX_PATH} 缺少 {PLUGIN_ID} 条目")
    errors = validate_upstream_contract(source, entry)
    if errors:
        raise SyncError("上游版本契约校验失败：\n" + "\n".join(f"- {error}" for error in errors))
    icon = str(entry.get("icon") or "").strip()
    if not icon:
        raise SyncError("上游索引条目缺少 icon")
    return UpstreamSnapshot(
        files=files,
        source_path=source_path,
        source=source,
        entry=entry,
        icon=icon,
        icon_bytes=_show_bytes(repo_root, ref, f"icons/{icon}"),
    )


def _patch_upstream(source: str) -> Any:
    """调用补丁脚本；锚点漂移时转换为可读的同步失败."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from apply_no_proxy_patch import PatchAnchorError, apply_no_proxy_patch

    try:
        return apply_no_proxy_patch(source)
    except PatchAnchorError as error:
        raise SyncError(f"补丁失败：{error}") from error


def _sync_plugin_files(repo_root: Path, ref: str, snapshot: UpstreamSnapshot, patch_result: Any) -> list[str]:
    """写入上游插件目录中的全部文件，源码使用补丁后的内容."""
    changes: list[str] = []
    for upstream_path in snapshot.files:
        relative = Path(upstream_path).relative_to(UPSTREAM_PLUGIN_DIR)
        data = _show_bytes(repo_root, ref, upstream_path)
        if upstream_path == snapshot.source_path:
            data = patch_result.source.encode("utf-8")
        target = repo_root / UPSTREAM_PLUGIN_DIR / relative
        if _write_if_changed(target, data):
            changes.append(target.relative_to(repo_root).as_posix())
    return changes


def _sync_icon(repo_root: Path, snapshot: UpstreamSnapshot) -> list[str]:
    """核对图标大小写并写入上游图标内容."""
    icons_dir = repo_root / "icons"
    conflict = _icon_case_conflict(icons_dir, snapshot.icon)
    if conflict:
        raise SyncError(f"图标 {snapshot.icon} 与磁盘上的 {conflict} 仅大小写不同，请按 Linux 大小写修正")
    if _write_if_changed(icons_dir / snapshot.icon, snapshot.icon_bytes):
        return [f"icons/{snapshot.icon}"]
    return []


def _sync_index(repo_root: Path, upstream_entry: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """局部插入或替换 package.json 的 v1 条目."""
    index_file = repo_root / INDEX_PATH
    raw_index = index_file.read_bytes().decode("utf-8") if index_file.exists() else "{}\n"
    local_index = json.loads(raw_index)
    local_entry = local_index.get(PLUGIN_ID) if isinstance(local_index.get(PLUGIN_ID), dict) else None
    merged_entry = merge_index_entry(upstream_entry, local_entry)
    new_index = replace_index_entry_text(raw_index, PLUGIN_ID, merged_entry)
    if json.loads(new_index).get(PLUGIN_ID) != merged_entry:
        raise SyncError("索引局部替换后条目内容不一致")
    if _write_if_changed(index_file, new_index.encode("utf-8")):
        return merged_entry, [INDEX_PATH]
    return merged_entry, []


def _sync_readme(repo_root: Path, entry: dict[str, Any]) -> list[str]:
    """首次导入时补齐根 README 的插件清单条目."""
    readme_file = repo_root / README_PATH
    if not readme_file.exists():
        return []
    raw_readme = readme_file.read_bytes().decode("utf-8")
    new_readme = ensure_readme_entry(raw_readme, entry)
    if _write_if_changed(readme_file, new_readme.encode("utf-8")):
        return [README_PATH]
    return []


def _verify_patch_state(repo_root: Path, source_path: str, patch_result: Any) -> None:
    """确认写入后的插件源码仍处于补丁目标状态."""
    if patch_result.status == "upstream-bypass":
        return
    current = _apply_no_proxy_patch_file(repo_root / source_path)
    if current.status != "unchanged":
        raise SyncError("写入后的插件源码未处于补丁目标状态")


def _apply_no_proxy_patch_file(path: Path) -> Any:
    """读取文件并调用补丁脚本，供同步后复核使用."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from apply_no_proxy_patch import apply_no_proxy_patch

    return apply_no_proxy_patch(path.read_bytes().decode("utf-8"))


def _sync(repo_root: Path, ref: str, skip_checks: bool, body_path: str | None) -> int:
    """执行完整同步流程并返回退出码."""
    _run_git(repo_root, "fetch", UPSTREAM_REMOTE, UPSTREAM_BRANCH)
    snapshot = _load_upstream(repo_root, ref)
    patch_result = _patch_upstream(snapshot.source)

    changes = _sync_plugin_files(repo_root, ref, snapshot, patch_result)
    changes.extend(_sync_icon(repo_root, snapshot))
    merged_entry, index_changes = _sync_index(repo_root, snapshot.entry)
    changes.extend(index_changes)
    changes.extend(_sync_readme(repo_root, merged_entry))
    _verify_patch_state(repo_root, snapshot.source_path, patch_result)

    checks = [] if skip_checks else _collect_checks(repo_root)
    failed = [check for check in checks if check.status == "failed"]
    body = _build_body(str(snapshot.entry.get("version") or ""), patch_result, changes, checks)
    print(body)
    if body_path:
        body_file = Path(body_path)
        body_file.parent.mkdir(parents=True, exist_ok=True)
        body_file.write_text(body, encoding="utf-8")
    if failed:
        raise SyncError("校验失败：" + "、".join(check.name for check in failed))
    return 0


def main(argv: list[str] | None = None) -> int:
    """命令行入口：同步上游插件、运行校验并输出 PR 描述."""
    parser = argparse.ArgumentParser(description="同步上游 remoteidentifiers 插件并重放免代理补丁")
    parser.add_argument("--body-path", help="把 PR 描述写入该文件")
    parser.add_argument("--skip-checks", action="store_true", help="跳过同步校验（仅本地调试）")
    parser.add_argument("--upstream-branch", default=UPSTREAM_BRANCH)
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    ref = f"{UPSTREAM_REMOTE}/{args.upstream_branch}"
    try:
        return _sync(repo_root, ref, args.skip_checks, args.body_path)
    except SyncError as error:
        print(f"同步失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
