#!/usr/bin/env python3
"""Folder Diff - 文件夹对比工具，生成HTML报告"""

import argparse
import csv
import difflib
import hashlib
import json
import os
import sys
from pathlib import Path
from datetime import datetime

SAMPLE_CHUNKS = [
    (0, 4096),
    (1 / 3, 4096),
    (2 / 3, 4096),
    (-4096, 4096),
]

TEXT_EXTENSIONS = {
    ".txt", ".md", ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css",
    ".json", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".sh", ".bat", ".ps1", ".c", ".cpp", ".h", ".hpp", ".java", ".go",
    ".rs", ".rb", ".php", ".sql", ".r", ".m", ".swift", ".kt", ".scala",
    ".csv", ".log", ".env", ".gitignore", ".dockerignore", ".editorconfig",
    ".vue", ".svelte",
}


def is_hidden(path: str) -> bool:
    parts = Path(path).parts
    for part in parts:
        if part.startswith("."):
            return True
    return False


def should_skip(rel_path: str, ignore_exts: set, ignore_hidden: bool) -> bool:
    if ignore_hidden and is_hidden(rel_path):
        return True
    ext = os.path.splitext(rel_path)[1].lower()
    if ext in ignore_exts:
        return True
    return False


def sample_md5(file_path: str, sample_threshold: int) -> str:
    file_size = os.path.getsize(file_path)
    h = hashlib.md5()

    if file_size <= sample_threshold:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    else:
        with open(file_path, "rb") as f:
            for offset_ratio, length in SAMPLE_CHUNKS:
                if offset_ratio < 0:
                    offset = max(0, file_size + offset_ratio)
                else:
                    offset = int(file_size * offset_ratio)
                offset = min(offset, max(0, file_size - length))
                f.seek(offset)
                h.update(f.read(min(length, file_size - offset)))
        h.update(str(file_size).encode())

    return h.hexdigest()


def full_md5(file_path: str) -> str:
    h = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_folder(root: str, ignore_exts: set, ignore_hidden: bool, sample_threshold: int) -> dict:
    result = {}
    root_path = Path(root).resolve()
    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [d for d in dirnames if not (ignore_hidden and d.startswith("."))]
        for fname in filenames:
            full_path = os.path.join(dirpath, fname)
            rel_path = os.path.relpath(full_path, root_path).replace("\\", "/")
            if should_skip(rel_path, ignore_exts, ignore_hidden):
                continue
            try:
                stat = os.stat(full_path)
                md5_hash = sample_md5(full_path, sample_threshold)
                result[rel_path] = {
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "md5": md5_hash,
                }
            except (OSError, PermissionError) as e:
                result[rel_path] = {
                    "size": -1,
                    "mtime": 0,
                    "md5": "",
                    "error": str(e),
                }
    return result


def load_cache(cache_path: str) -> dict:
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def save_cache(cache_path: str, left_snapshot: dict, right_snapshot: dict) -> None:
    if not cache_path:
        return
    data = {
        "left": left_snapshot,
        "right": right_snapshot,
        "timestamp": datetime.now().isoformat(),
    }
    os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


def compare(left_snapshot: dict, right_snapshot: dict, prev_cache: dict,
            sample_threshold: int, left_root: str, right_root: str, show_diff: bool) -> dict:
    prev_left = prev_cache.get("left", {})
    prev_right = prev_cache.get("right", {})

    only_left = []
    only_right = []
    different = []
    identical = []

    all_files = set(left_snapshot.keys()) | set(right_snapshot.keys())

    for rel_path in sorted(all_files):
        in_left = rel_path in left_snapshot
        in_right = rel_path in right_snapshot

        if in_left and not in_right:
            prev_status = None
            if rel_path in prev_left and rel_path not in prev_right:
                if left_snapshot[rel_path].get("md5") == prev_left[rel_path].get("md5"):
                    prev_status = "unchanged"

            only_left.append({
                "path": rel_path,
                "size": left_snapshot[rel_path]["size"],
                "mtime": left_snapshot[rel_path].get("mtime", 0),
                "md5": left_snapshot[rel_path].get("md5", ""),
                "error": left_snapshot[rel_path].get("error", ""),
                "prev_status": prev_status,
            })

        elif not in_left and in_right:
            prev_status = None
            if rel_path not in prev_left and rel_path in prev_right:
                if right_snapshot[rel_path].get("md5") == prev_right[rel_path].get("md5"):
                    prev_status = "unchanged"

            only_right.append({
                "path": rel_path,
                "size": right_snapshot[rel_path]["size"],
                "mtime": right_snapshot[rel_path].get("mtime", 0),
                "md5": right_snapshot[rel_path].get("md5", ""),
                "error": right_snapshot[rel_path].get("error", ""),
                "prev_status": prev_status,
            })

        else:
            l_info = left_snapshot[rel_path]
            r_info = right_snapshot[rel_path]

            if l_info.get("md5") == r_info.get("md5") and l_info.get("size") == r_info.get("size"):
                identical.append({
                    "path": rel_path,
                    "size": l_info["size"],
                    "md5": l_info.get("md5", ""),
                })
            else:
                prev_status = None
                if rel_path in prev_left and rel_path in prev_right:
                    pl = prev_left[rel_path]
                    pr = prev_right[rel_path]
                    if pl.get("md5") == l_info.get("md5") and pr.get("md5") == r_info.get("md5"):
                        prev_status = "unchanged"

                l_full_md5 = l_info.get("md5", "")
                r_full_md5 = r_info.get("md5", "")

                if l_info.get("md5") != r_info.get("md5") and (
                    l_info.get("size", 0) > sample_threshold or r_info.get("size", 0) > sample_threshold
                ):
                    try:
                        l_full_md5 = full_md5(os.path.join(left_root, rel_path))
                        r_full_md5 = full_md5(os.path.join(right_root, rel_path))
                    except (OSError, PermissionError):
                        pass

                diff_lines = None
                if show_diff and l_info.get("md5") != r_info.get("md5"):
                    ext = os.path.splitext(rel_path)[1].lower()
                    if ext in TEXT_EXTENSIONS:
                        try:
                            with open(os.path.join(left_root, rel_path), "r", encoding="utf-8", errors="replace") as f:
                                left_content = f.readlines()
                            with open(os.path.join(right_root, rel_path), "r", encoding="utf-8", errors="replace") as f:
                                right_content = f.readlines()
                            diff_lines = list(difflib.unified_diff(
                                left_content, right_content,
                                fromfile=f"left/{rel_path}",
                                tofile=f"right/{rel_path}",
                                lineterm="",
                            ))[:500]
                        except (OSError, PermissionError):
                            diff_lines = ["[无法读取文件内容]"]

                different.append({
                    "path": rel_path,
                    "left_size": l_info["size"],
                    "right_size": r_info["size"],
                    "left_mtime": l_info.get("mtime", 0),
                    "right_mtime": r_info.get("mtime", 0),
                    "left_md5": l_full_md5,
                    "right_md5": r_full_md5,
                    "left_error": l_info.get("error", ""),
                    "right_error": r_info.get("error", ""),
                    "diff": diff_lines,
                    "prev_status": prev_status,
                })

    return {
        "only_left": only_left,
        "only_right": only_right,
        "different": different,
        "identical": identical,
        "total_left": len(left_snapshot),
        "total_right": len(right_snapshot),
    }


def diff_to_html_table(diff_lines: list) -> str:
    if not diff_lines:
        return ""
    rows = []
    for line in diff_lines:
        line_escaped = (line
                        .replace("&", "&amp;")
                        .replace("<", "&lt;")
                        .replace(">", "&gt;")
                        .replace('"', "&quot;"))
        css_class = ""
        if line.startswith("+"):
            css_class = "diff-add"
        elif line.startswith("-"):
            css_class = "diff-del"
        elif line.startswith("@@"):
            css_class = "diff-hunk"
        rows.append(f'<tr class="{css_class}"><td class="diff-line-num"></td><td class="diff-content"><pre>{line_escaped}</pre></td></tr>')
    return "\n".join(rows)


def generate_rsync_commands(only_left: list, only_right: list, different: list,
                            left_root: str, right_root: str) -> list:
    commands = []
    left_escaped = left_root.replace("\\", "/")
    right_escaped = right_root.replace("\\", "/")

    if only_left:
        cmds = " && ".join(
            f'rsync -avR "{left_escaped}/./{item["path"]}" "{right_escaped}/"'
            for item in only_left
        )
        commands.append(f"# 将左边独有的文件同步到右边\n{cmds}")

    if only_right:
        cmds = " && ".join(
            f'rsync -avR "{right_escaped}/./{item["path"]}" "{left_escaped}/"'
            for item in only_right
        )
        commands.append(f"# 将右边独有的文件同步到左边\n{cmds}")

    if different:
        cmds_left_to_right = " && ".join(
            f'rsync -avR "{left_escaped}/./{item["path"]}" "{right_escaped}/"'
            for item in different
        )
        cmds_right_to_left = " && ".join(
            f'rsync -avR "{right_escaped}/./{item["path"]}" "{left_escaped}/"'
            for item in different
        )
        commands.append(f"# 用左边版本覆盖右边（差异文件）\n{cmds_left_to_right}")
        commands.append(f"# 用右边版本覆盖左边（差异文件）\n{cmds_right_to_left}")

    return commands


def format_size(size: int) -> str:
    if size < 0:
        return "N/A"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(size) < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024
    return f"{size:.1f} PB"


def format_time(ts: float) -> str:
    if ts <= 0:
        return "N/A"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def generate_html(report: dict, left_root: str, right_root: str,
                  show_diff: bool, rsync_commands: list) -> str:
    only_left = report["only_left"]
    only_right = report["only_right"]
    different = report["different"]
    identical = report["identical"]

    left_count = len(only_left)
    right_count = len(only_right)
    diff_count = len(different)
    same_count = len(identical)

    rsync_html = ""
    for cmd in rsync_commands:
        cmd_escaped = cmd.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        rsync_html += f'<pre class="rsync-cmd">{cmd_escaped}</pre>'

    def build_file_rows(items, mode):
        rows = []
        for item in items:
            path = item["path"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            new_tag = ""
            if item.get("prev_status") == "unchanged":
                new_tag = ""
            else:
                new_tag = ' <span class="badge badge-new">NEW</span>' if item.get("prev_status") is None else ""

            if mode == "left":
                rows.append(f'''
                <tr>
                    <td>{new_tag}</td>
                    <td class="file-path">{path}</td>
                    <td>{format_size(item["size"])}</td>
                    <td>{format_time(item["mtime"])}</td>
                    <td class="md5-cell">{item.get("md5", "")[:16]}...</td>
                    <td>{item.get("error", "")}</td>
                </tr>''')
            elif mode == "right":
                rows.append(f'''
                <tr>
                    <td>{new_tag}</td>
                    <td class="file-path">{path}</td>
                    <td>{format_size(item["size"])}</td>
                    <td>{format_time(item["mtime"])}</td>
                    <td class="md5-cell">{item.get("md5", "")[:16]}...</td>
                    <td>{item.get("error", "")}</td>
                </tr>''')
            elif mode == "diff":
                diff_id = f'diff-{hashlib.md5(path.encode()).hexdigest()[:8]}'
                new_tag_html = ""
                if item.get("prev_status") == "unchanged":
                    new_tag_html = ""
                else:
                    new_tag_html = ' <span class="badge badge-new">NEW</span>' if item.get("prev_status") is None else ""

                diff_html = ""
                if item.get("diff"):
                    diff_html = f'''
                    <tr class="diff-row" id="{diff_id}-row" style="display:none;">
                        <td colspan="7">
                            <div class="diff-container">
                                <table class="diff-table">{diff_to_html_table(item["diff"])}</table>
                            </div>
                        </td>
                    </tr>'''

                toggle_btn = ""
                if item.get("diff"):
                    toggle_btn = f'<button class="btn-toggle-diff" onclick="toggleDiff(\'{diff_id}\')">查看Diff</button>'

                rows.append(f'''
                <tr class="diff-header-row" data-diff-id="{diff_id}">
                    <td>{new_tag_html}</td>
                    <td class="file-path">{path}</td>
                    <td>{format_size(item["left_size"])} → {format_size(item["right_size"])}</td>
                    <td>{format_time(item["left_mtime"])} → {format_time(item["right_mtime"])}</td>
                    <td class="md5-cell" title="Left: {item.get("left_md5", "")}\nRight: {item.get("right_md5", "")}">
                        {item.get("left_md5", "")[:8]}... / {item.get("right_md5", "")[:8]}...
                    </td>
                    <td>{toggle_btn}</td>
                </tr>
                {diff_html}''')
        return "\n".join(rows)

    left_rows = build_file_rows(only_left, "left")
    right_rows = build_file_rows(only_right, "right")
    diff_rows = build_file_rows(different, "diff")

    identical_rows = ""
    for item in identical:
        path = item["path"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        identical_rows += f'''
        <tr>
            <td class="file-path">{path}</td>
            <td>{format_size(item["size"])}</td>
            <td class="md5-cell">{item.get("md5", "")[:16]}...</td>
        </tr>'''

    diff_section_style = "" if show_diff else ""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>文件夹对比报告</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f5f7fa; color: #333; }}
.header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 24px 32px; }}
.header h1 {{ font-size: 24px; margin-bottom: 8px; }}
.header .subtitle {{ font-size: 13px; opacity: 0.85; }}
.summary {{ display: flex; gap: 16px; padding: 20px 32px; background: white; border-bottom: 1px solid #e8ecf1; flex-wrap: wrap; }}
.summary-card {{ flex: 1; min-width: 150px; padding: 16px; border-radius: 8px; text-align: center; border: 1px solid #e8ecf1; }}
.summary-card .num {{ font-size: 32px; font-weight: 700; }}
.summary-card .label {{ font-size: 13px; color: #666; margin-top: 4px; }}
.summary-card.left-only {{ border-left: 4px solid #f59e0b; }}
.summary-card.right-only {{ border-left: 4px solid #3b82f6; }}
.summary-card.different {{ border-left: 4px solid #ef4444; }}
.summary-card.identical {{ border-left: 4px solid #10b981; }}
.tabs {{ display: flex; gap: 0; padding: 0 32px; background: white; border-bottom: 2px solid #e8ecf1; }}
.tab-btn {{ padding: 12px 24px; border: none; background: none; cursor: pointer; font-size: 14px; color: #666; border-bottom: 2px solid transparent; margin-bottom: -2px; transition: all 0.2s; }}
.tab-btn:hover {{ color: #333; }}
.tab-btn.active {{ color: #667eea; border-bottom-color: #667eea; font-weight: 600; }}
.tab-content {{ display: none; padding: 24px 32px; }}
.tab-content.active {{ display: block; }}
.container {{ max-width: 1400px; margin: 0 auto; }}
table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
th, td {{ padding: 10px 14px; text-align: left; border-bottom: 1px solid #f0f2f5; font-size: 13px; }}
th {{ background: #f8f9fb; font-weight: 600; color: #555; white-space: nowrap; }}
tr:hover {{ background: #f8f9fb; }}
.file-path {{ font-family: "SF Mono", "Cascadia Code", Consolas, monospace; font-size: 12px; word-break: break-all; }}
.md5-cell {{ font-family: monospace; font-size: 11px; color: #888; }}
.badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }}
.badge-new {{ background: #fef3c7; color: #92400e; }}
.btn {{ display: inline-block; padding: 6px 16px; border: 1px solid #d1d5db; border-radius: 6px; background: white; cursor: pointer; font-size: 13px; transition: all 0.15s; }}
.btn:hover {{ background: #f3f4f6; }}
.btn-primary {{ background: #667eea; color: white; border-color: #667eea; }}
.btn-primary:hover {{ background: #5a6fd6; }}
.btn-toggle-diff {{ padding: 2px 10px; font-size: 11px; border: 1px solid #d1d5db; border-radius: 4px; background: #f9fafb; cursor: pointer; }}
.btn-toggle-diff:hover {{ background: #e5e7eb; }}
.diff-container {{ max-height: 400px; overflow: auto; background: #1e1e1e; border-radius: 6px; padding: 8px 0; }}
.diff-table {{ width: 100%; border-collapse: collapse; box-shadow: none; border-radius: 0; }}
.diff-table td {{ padding: 1px 12px; border: none; font-size: 12px; }}
.diff-table tr:hover {{ background: inherit; }}
.diff-add {{ background: rgba(16, 185, 129, 0.15); }}
.diff-add pre {{ color: #6ee7b7; }}
.diff-del {{ background: rgba(239, 68, 68, 0.15); }}
.diff-del pre {{ color: #fca5a5; }}
.diff-hunk {{ background: rgba(59, 130, 246, 0.15); }}
.diff-hunk pre {{ color: #93c5fd; }}
.diff-table pre {{ margin: 0; white-space: pre-wrap; word-break: break-all; font-family: "SF Mono", "Cascadia Code", Consolas, monospace; font-size: 11px; color: #d4d4d4; }}
.diff-line-num {{ width: 40px; text-align: right; color: #666; }}
.rsync-cmd {{ background: #1e1e1e; color: #6ee7b7; padding: 12px 16px; border-radius: 6px; margin: 8px 0; font-size: 12px; overflow-x: auto; white-space: pre-wrap; word-break: break-all; font-family: "SF Mono", "Cascadia Code", Consolas, monospace; }}
.toolbar {{ display: flex; gap: 8px; padding: 16px 32px; flex-wrap: wrap; align-items: center; }}
.toolbar .spacer {{ flex: 1; }}
.search-box {{ padding: 6px 12px; border: 1px solid #d1d5db; border-radius: 6px; font-size: 13px; width: 240px; }}
.stats-info {{ font-size: 12px; color: #888; }}

@media (max-width: 768px) {{
    .summary {{ flex-direction: column; }}
    .tabs {{ overflow-x: auto; }}
}}
</style>
</head>
<body>
<div class="container">
<div class="header">
    <h1>📁 文件夹对比报告</h1>
    <div class="subtitle">
        左边: {left_root} &nbsp;|&nbsp; 右边: {right_root} &nbsp;|&nbsp; 生成时间: {now_str}
    </div>
</div>

<div class="summary">
    <div class="summary-card left-only">
        <div class="num" style="color:#f59e0b;">{left_count}</div>
        <div class="label">仅在左边</div>
    </div>
    <div class="summary-card right-only">
        <div class="num" style="color:#3b82f6;">{right_count}</div>
        <div class="label">仅在右边</div>
    </div>
    <div class="summary-card different">
        <div class="num" style="color:#ef4444;">{diff_count}</div>
        <div class="label">内容不同</div>
    </div>
    <div class="summary-card identical">
        <div class="num" style="color:#10b981;">{same_count}</div>
        <div class="label">完全相同</div>
    </div>
</div>

<div class="toolbar">
    <input type="text" class="search-box" id="searchInput" placeholder="🔍 过滤文件名..." oninput="filterTable()">
    <button class="btn" onclick="exportCSV()">📥 导出CSV</button>
    <button class="btn" id="btnSync" onclick="toggleSyncPanel()">🔄 同步命令</button>
    <span class="spacer"></span>
    <span class="stats-info">左边 {report["total_left"]} 个文件 | 右边 {report["total_right"]} 个文件</span>
</div>

<div id="syncPanel" style="display:none; padding: 0 32px 16px 32px;">
    <h3 style="margin-bottom:8px;">建议的 rsync 命令</h3>
    {rsync_html}
    <p style="font-size:12px;color:#888;margin-top:4px;">⚠ 这些命令仅供参考，请根据实际需求手动执行。</p>
</div>

<div class="tabs">
    <button class="tab-btn active" onclick="switchTab('tab-left')">仅在左边 ({left_count})</button>
    <button class="tab-btn" onclick="switchTab('tab-right')">仅在右边 ({right_count})</button>
    <button class="tab-btn" onclick="switchTab('tab-diff')">内容不同 ({diff_count})</button>
    <button class="tab-btn" onclick="switchTab('tab-same')">完全相同 ({same_count})</button>
</div>

<div id="tab-left" class="tab-content active">
    <table>
        <thead>
            <tr>
                <th style="width:60px;"></th>
                <th>文件路径</th>
                <th style="width:100px;">大小</th>
                <th style="width:160px;">修改时间</th>
                <th style="width:150px;">MD5</th>
                <th style="width:80px;">错误</th>
            </tr>
        </thead>
        <tbody>{left_rows}</tbody>
    </table>
</div>

<div id="tab-right" class="tab-content">
    <table>
        <thead>
            <tr>
                <th style="width:60px;"></th>
                <th>文件路径</th>
                <th style="width:100px;">大小</th>
                <th style="width:160px;">修改时间</th>
                <th style="width:150px;">MD5</th>
                <th style="width:80px;">错误</th>
            </tr>
        </thead>
        <tbody>{right_rows}</tbody>
    </table>
</div>

<div id="tab-diff" class="tab-content">
    <table>
        <thead>
            <tr>
                <th style="width:60px;"></th>
                <th>文件路径</th>
                <th style="width:120px;">大小 (左→右)</th>
                <th style="width:260px;">修改时间 (左→右)</th>
                <th style="width:200px;">MD5 (左 / 右)</th>
                <th style="width:100px;">操作</th>
            </tr>
        </thead>
        <tbody>{diff_rows}</tbody>
    </table>
</div>

<div id="tab-same" class="tab-content">
    <table>
        <thead>
            <tr>
                <th>文件路径</th>
                <th style="width:100px;">大小</th>
                <th style="width:150px;">MD5</th>
            </tr>
        </thead>
        <tbody>{identical_rows}</tbody>
    </table>
</div>

</div>

<script>
const reportData = {json.dumps(report, default=str, ensure_ascii=False)};
const leftRoot = {json.dumps(left_root, ensure_ascii=False)};
const rightRoot = {json.dumps(right_root, ensure_ascii=False)};

function switchTab(tabId) {{
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.getElementById(tabId).classList.add('active');
    event.target.classList.add('active');
}}

function toggleSyncPanel() {{
    const panel = document.getElementById('syncPanel');
    panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
}}

function toggleDiff(diffId) {{
    const row = document.getElementById(diffId + '-row');
    if (row) {{
        row.style.display = row.style.display === 'none' ? 'table-row' : 'none';
        const btns = document.querySelectorAll('[data-diff-id="' + diffId + '"] .btn-toggle-diff');
        btns.forEach(btn => {{
            btn.textContent = row.style.display === 'none' ? '查看Diff' : '隐藏Diff';
        }});
    }}
}}

function filterTable() {{
    const query = document.getElementById('searchInput').value.toLowerCase();
    document.querySelectorAll('.tab-content.active tbody tr').forEach(row => {{
        const text = row.textContent.toLowerCase();
        if (query === '' || text.includes(query)) {{
            row.style.display = '';
            if (row.classList.contains('diff-header-row')) {{
                const diffId = row.getAttribute('data-diff-id');
                const diffRow = document.getElementById(diffId + '-row');
            }}
        }} else {{
            row.style.display = 'none';
            if (row.classList.contains('diff-header-row')) {{
                const diffId = row.getAttribute('data-diff-id');
                const diffRow = document.getElementById(diffId + '-row');
                if (diffRow) diffRow.style.display = 'none';
            }}
        }}
    }});
}}

function exportCSV() {{
    let csv = '\\uFEFF状态,文件路径,大小_左,大小_右,修改时间_左,修改时间_右,MD5_左,MD5_右,错误\\n';

    reportData.only_left.forEach(item => {{
        csv += '仅在左边,' + item.path + ',' + item.size + ',,,' + (item.mtime||'') + ',,' + (item.md5||'') + ',,' + (item.error||'') + '\\n';
    }});

    reportData.only_right.forEach(item => {{
        csv += '仅在右边,' + item.path + ',,,' + item.size + ',,' + (item.mtime||'') + ',,' + (item.md5||'') + ',' + (item.error||'') + '\\n';
    }});

    reportData.different.forEach(item => {{
        csv += '内容不同,' + item.path + ',' + (item.left_size||'') + ',' + (item.right_size||'') + ',' + (item.left_mtime||'') + ',' + (item.right_mtime||'') + ',' + (item.left_md5||'') + ',' + (item.right_md5||'') + ',' + (item.left_error||'') + (item.right_error||'') + '\\n';
    }});

    reportData.identical.forEach(item => {{
        csv += '完全相同,' + item.path + ',' + item.size + ',' + item.size + ',,,' + (item.md5||'') + ',' + (item.md5||'') + ',\\n';
    }});

    const blob = new Blob([csv], {{type: 'text/csv;charset=utf-8;'}});
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = 'folder_diff_report.csv';
    link.click();
}}
</script>
</body>
</html>'''


def export_csv(report: dict, csv_path: str) -> None:
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["状态", "文件路径", "大小_左", "大小_右", "修改时间_左", "修改时间_右",
                          "MD5_左", "MD5_右", "错误"])
        for item in report["only_left"]:
            writer.writerow(["仅在左边", item["path"], item["size"], "", item.get("mtime", ""), "",
                             item.get("md5", ""), "", item.get("error", "")])
        for item in report["only_right"]:
            writer.writerow(["仅在右边", item["path"], "", "", "", item.get("mtime", ""),
                             "", item.get("md5", ""), item.get("error", "")])
        for item in report["different"]:
            writer.writerow(["内容不同", item["path"], item["left_size"], item["right_size"],
                             item.get("left_mtime", ""), item.get("right_mtime", ""),
                             item.get("left_md5", ""), item.get("right_md5", ""),
                             item.get("left_error", "") + " | " + item.get("right_error", "")])
        for item in report["identical"]:
            writer.writerow(["完全相同", item["path"], item["size"], item["size"], "", "",
                             item.get("md5", ""), item.get("md5", ""), ""])


def main():
    parser = argparse.ArgumentParser(
        description="文件夹对比工具 - 对比两个文件夹并生成HTML报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python folder_diff.py --left ./dir_a --right ./dir_b
  python folder_diff.py --left ./dir_a --right ./dir_b --ignore-ext .log .tmp --ignore-hidden
  python folder_diff.py --left ./dir_a --right ./dir_b --show-diff --sample-threshold 1048576
  python folder_diff.py --left ./dir_a --right ./dir_b --export-csv report.csv
  python folder_diff.py --left ./dir_a --right ./dir_b --cache ./snapshot.json
        """,
    )
    parser.add_argument("--left", required=True, help="左边文件夹路径")
    parser.add_argument("--right", required=True, help="右边文件夹路径")
    parser.add_argument("--output", "-o", default="folder_diff_report.html", help="HTML报告输出路径 (默认: folder_diff_report.html)")
    parser.add_argument("--ignore-ext", nargs="*", default=[], help="忽略的文件扩展名列表，如 .log .tmp")
    parser.add_argument("--ignore-hidden", action="store_true", help="忽略隐藏文件（以.开头的文件/目录）")
    parser.add_argument("--sample-threshold", type=int, default=1048576,
                        help="采样MD5阈值（字节），超过此大小的文件使用采样MD5 (默认: 1MB)")
    parser.add_argument("--show-diff", action="store_true", help="在报告中展示文本文件的diff预览")
    parser.add_argument("--export-csv", help="同时导出CSV文件到指定路径")
    parser.add_argument("--cache", help="快照缓存文件路径，用于增量对比")
    parser.add_argument("--no-cache", action="store_true", help="不保存新的缓存快照")

    args = parser.parse_args()

    left_root = os.path.abspath(args.left)
    right_root = os.path.abspath(args.right)

    if not os.path.isdir(left_root):
        print(f"错误: 左边路径不存在或不是文件夹: {left_root}", file=sys.stderr)
        sys.exit(1)
    if not os.path.isdir(right_root):
        print(f"错误: 右边路径不存在或不是文件夹: {right_root}", file=sys.stderr)
        sys.exit(1)

    ignore_exts = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in args.ignore_ext}

    print(f"正在扫描左边文件夹: {left_root}")
    left_snapshot = scan_folder(left_root, ignore_exts, args.ignore_hidden, args.sample_threshold)
    print(f"  找到 {len(left_snapshot)} 个文件")

    print(f"正在扫描右边文件夹: {right_root}")
    right_snapshot = scan_folder(right_root, ignore_exts, args.ignore_hidden, args.sample_threshold)
    print(f"  找到 {len(right_snapshot)} 个文件")

    prev_cache = {}
    if args.cache:
        prev_cache = load_cache(args.cache)
        if prev_cache:
            print(f"已加载缓存快照: {args.cache} ({prev_cache.get('timestamp', 'unknown')})")

    print("正在对比文件...")
    report = compare(
        left_snapshot, right_snapshot, prev_cache,
        args.sample_threshold, left_root, right_root, args.show_diff,
    )

    rsync_commands = generate_rsync_commands(
        report["only_left"], report["only_right"], report["different"],
        left_root, right_root,
    )

    print(f"正在生成HTML报告: {args.output}")
    html = generate_html(report, left_root, right_root, args.show_diff, rsync_commands)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html)

    if args.export_csv:
        print(f"正在导出CSV: {args.export_csv}")
        export_csv(report, args.export_csv)

    if args.cache and not args.no_cache:
        save_cache(args.cache, left_snapshot, right_snapshot)
        print(f"已保存缓存快照: {args.cache}")

    print()
    print("=" * 60)
    print("  对比完成！")
    print(f"  仅在左边:  {len(report['only_left'])} 个文件")
    print(f"  仅在右边:  {len(report['only_right'])} 个文件")
    print(f"  内容不同:  {len(report['different'])} 个文件")
    print(f"  完全相同:  {len(report['identical'])} 个文件")
    print(f"  HTML报告:  {os.path.abspath(args.output)}")
    if args.export_csv:
        print(f"  CSV导出:   {os.path.abspath(args.export_csv)}")
    print("=" * 60)


if __name__ == "__main__":
    main()