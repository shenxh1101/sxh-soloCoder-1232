#!/usr/bin/env python3
"""Folder Diff - 文件夹对比工具，生成HTML报告"""

import argparse
import csv
import difflib
import fnmatch
import hashlib
import json
import os
import re
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

DIFF_COLLAPSE_THRESHOLD = 50
DIFF_MAX_LINES = 2000


def is_hidden(path: str) -> bool:
    parts = Path(path).parts
    for part in parts:
        if part.startswith("."):
            return True
    return False


def compute_md5(file_path: str, verify_mode: str, sample_threshold: int) -> tuple:
    file_size = os.path.getsize(file_path)
    if verify_mode == "fast" and file_size > sample_threshold:
        h = hashlib.md5()
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
        return h.hexdigest(), "sample"
    else:
        h = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest(), "full"


def full_md5(file_path: str) -> str:
    h = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class IgnoreRule:
    def __init__(self, pattern=None, glob_pattern=None, directory=None,
                 min_size=None, max_size=None, hidden=None):
        self.pattern = pattern
        self.glob_pattern = glob_pattern
        self.directory = directory
        self.min_size = min_size
        self.max_size = max_size
        self.hidden = hidden

    def matches(self, rel_path: str, file_size: int) -> bool:
        if self.hidden is not None and is_hidden(rel_path) == self.hidden:
            return True
        if self.pattern and fnmatch.fnmatch(rel_path, self.pattern):
            return True
        if self.glob_pattern and fnmatch.fnmatch(rel_path, self.glob_pattern):
            return True
        if self.directory:
            norm_dir = self.directory.replace("\\", "/").rstrip("/") + "/"
            if rel_path.startswith(norm_dir):
                return True
        if self.min_size is not None and file_size >= 0 and file_size < self.min_size:
            return True
        if self.max_size is not None and file_size >= 0 and file_size > self.max_size:
            return True
        return False


def load_ignore_config(config_path: str) -> list:
    if not os.path.exists(config_path):
        print(f"警告: 忽略规则配置文件不存在: {config_path}", file=sys.stderr)
        return []
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    rules = []
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        entries = data.get("ignore", data.get("rules", []))
    else:
        return rules
    for entry in entries:
        if isinstance(entry, str):
            if entry.startswith(".") and "/" not in entry:
                rules.append(IgnoreRule(glob_pattern="*" + entry))
            else:
                rules.append(IgnoreRule(glob_pattern=entry))
        elif isinstance(entry, dict):
            rules.append(IgnoreRule(
                pattern=entry.get("pattern"),
                glob_pattern=entry.get("glob"),
                directory=entry.get("directory"),
                min_size=entry.get("min_size"),
                max_size=entry.get("max_size"),
                hidden=entry.get("hidden"),
            ))
    return rules


def should_skip(rel_path: str, file_size: int, rules: list) -> bool:
    for rule in rules:
        if rule.matches(rel_path, file_size):
            return True
    return False


def scan_folder(root: str, rules: list, verify_mode: str, sample_threshold: int) -> dict:
    result = {}
    root_path = Path(root).resolve()
    for dirpath, dirnames, filenames in os.walk(root_path):
        for fname in filenames:
            full_path = os.path.join(dirpath, fname)
            rel_path = os.path.relpath(full_path, root_path).replace("\\", "/")
            try:
                fsize = os.path.getsize(full_path)
            except OSError:
                fsize = -1
            if should_skip(rel_path, fsize, rules):
                continue
            try:
                stat = os.stat(full_path)
                md5_hash, verify_method = compute_md5(full_path, verify_mode, sample_threshold)
                result[rel_path] = {
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "md5": md5_hash,
                    "verify_method": verify_method,
                }
            except (OSError, PermissionError) as e:
                result[rel_path] = {
                    "size": -1,
                    "mtime": 0,
                    "md5": "",
                    "verify_method": "error",
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


def save_cache(cache_path: str, left_snapshot: dict, right_snapshot: dict,
               report: dict) -> None:
    if not cache_path:
        return
    data = {
        "left": left_snapshot,
        "right": right_snapshot,
        "timestamp": datetime.now().isoformat(),
        "summary": {
            "only_left_count": len(report["only_left"]),
            "only_right_count": len(report["only_right"]),
            "different_count": len(report["different"]),
            "identical_count": len(report["identical"]),
        },
    }
    os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


def compute_delta_status(rel_path, in_left, in_right, l_info, r_info,
                         prev_left, prev_right, prev_report):
    if not prev_left and not prev_right:
        return None

    was_in_left = rel_path in prev_left
    was_in_right = rel_path in prev_right

    if was_in_left and was_in_right:
        pl = prev_left[rel_path]
        pr = prev_right[rel_path]
        was_different = pl.get("md5") != pr.get("md5") or pl.get("size") != pr.get("size")
        is_different_now = (in_left and in_right and
                            l_info.get("md5") != r_info.get("md5"))

        if not in_left and not in_right:
            return "deleted_both"
        elif not in_left:
            return "deleted_left"
        elif not in_right:
            return "deleted_right"
        elif was_different and not is_different_now:
            return "resolved"
        elif not was_different and is_different_now:
            return "new_different"
        elif was_different and is_different_now:
            if l_info.get("md5") == pl.get("md5") and r_info.get("md5") == pr.get("md5"):
                return "unchanged"
            else:
                return "modified"
        else:
            return None

    elif was_in_left and not was_in_right:
        if not in_left and not in_right:
            return "deleted"
        elif in_left and not in_right:
            if l_info.get("md5") == prev_left[rel_path].get("md5"):
                return "unchanged"
            return "modified"
        elif in_left and in_right:
            return "moved_to_both"
        elif not in_left and in_right:
            return "moved_to_right"

    elif not was_in_left and was_in_right:
        if not in_left and not in_right:
            return "deleted"
        elif not in_left and in_right:
            if r_info.get("md5") == prev_right[rel_path].get("md5"):
                return "unchanged"
            return "modified"
        elif in_left and in_right:
            return "moved_to_both"
        elif in_left and not in_right:
            return "moved_to_left"

    else:
        if in_left and not in_right:
            return "new_only_left"
        elif not in_left and in_right:
            return "new_only_right"
        elif in_left and in_right:
            return "new_both"

    return None


DELTA_LABELS = {
    "resolved": ("已解决", "badge-resolved"),
    "deleted": ("已删除", "badge-deleted"),
    "deleted_both": ("已删除", "badge-deleted"),
    "deleted_left": ("左边已删除", "badge-deleted"),
    "deleted_right": ("右边已删除", "badge-deleted"),
    "moved_to_both": ("变为两边都有", "badge-moved"),
    "moved_to_left": ("移到左边", "badge-moved"),
    "moved_to_right": ("移到右边", "badge-moved"),
    "new_different": ("新差异", "badge-new"),
    "new_only_left": ("新文件", "badge-new"),
    "new_only_right": ("新文件", "badge-new"),
    "new_both": ("新文件", "badge-new"),
    "modified": ("已修改", "badge-modified"),
    "unchanged": ("", ""),
}


def compare(left_snapshot: dict, right_snapshot: dict, prev_cache: dict,
            sample_threshold: int, left_root: str, right_root: str,
            show_diff: bool, verify_mode: str) -> dict:
    prev_left = prev_cache.get("left", {})
    prev_right = prev_cache.get("right", {})
    prev_report = prev_cache.get("summary", {})

    only_left = []
    only_right = []
    different = []
    identical = []
    deleted = []

    all_files = set(left_snapshot.keys()) | set(right_snapshot.keys())
    if prev_left or prev_right:
        all_files |= set(prev_left.keys()) | set(prev_right.keys())

    for rel_path in sorted(all_files):
        in_left = rel_path in left_snapshot
        in_right = rel_path in right_snapshot
        l_info = left_snapshot.get(rel_path, {})
        r_info = right_snapshot.get(rel_path, {})

        delta = compute_delta_status(
            rel_path, in_left, in_right, l_info, r_info,
            prev_left, prev_right, prev_report,
        )

        if not in_left and not in_right:
            deleted.append({
                "path": rel_path,
                "delta": delta,
            })

        elif in_left and not in_right:
            only_left.append({
                "path": rel_path,
                "size": l_info["size"],
                "mtime": l_info.get("mtime", 0),
                "md5": l_info.get("md5", ""),
                "verify_method": l_info.get("verify_method", "full"),
                "error": l_info.get("error", ""),
                "delta": delta,
            })

        elif not in_left and in_right:
            only_right.append({
                "path": rel_path,
                "size": r_info["size"],
                "mtime": r_info.get("mtime", 0),
                "md5": r_info.get("md5", ""),
                "verify_method": r_info.get("verify_method", "full"),
                "error": r_info.get("error", ""),
                "delta": delta,
            })

        else:
            if l_info.get("md5") == r_info.get("md5") and l_info.get("size") == r_info.get("size"):
                identical.append({
                    "path": rel_path,
                    "size": l_info["size"],
                    "md5": l_info.get("md5", ""),
                    "verify_method": l_info.get("verify_method", "full"),
                    "delta": delta,
                })
            else:
                l_md5 = l_info.get("md5", "")
                r_md5 = r_info.get("md5", "")
                l_method = l_info.get("verify_method", "full")
                r_method = r_info.get("verify_method", "full")

                if l_md5 != r_md5 and (l_method == "sample" or r_method == "sample"):
                    try:
                        l_md5 = full_md5(os.path.join(left_root, rel_path))
                        l_method = "full"
                    except (OSError, PermissionError):
                        pass
                    try:
                        r_md5 = full_md5(os.path.join(right_root, rel_path))
                        r_method = "full"
                    except (OSError, PermissionError):
                        pass

                if l_md5 == r_md5 and l_info.get("size") == r_info.get("size"):
                    identical.append({
                        "path": rel_path,
                        "size": l_info["size"],
                        "md5": l_md5,
                        "verify_method": "full",
                        "delta": delta,
                    })
                    continue

                diff_data = None
                if show_diff:
                    ext = os.path.splitext(rel_path)[1].lower()
                    if ext in TEXT_EXTENSIONS:
                        try:
                            with open(os.path.join(left_root, rel_path), "r", encoding="utf-8", errors="replace") as f:
                                left_content = f.readlines()
                            with open(os.path.join(right_root, rel_path), "r", encoding="utf-8", errors="replace") as f:
                                right_content = f.readlines()
                            total_lines = len(left_content) + len(right_content)
                            if total_lines > DIFF_MAX_LINES:
                                left_content = left_content[:DIFF_MAX_LINES // 2]
                                right_content = right_content[:DIFF_MAX_LINES // 2]
                            sm = difflib.SequenceMatcher(None, left_content, right_content)
                            opcodes = sm.get_opcodes()

                            diff_data = {
                                "left_lines": [],
                                "right_lines": [],
                                "rows": [],
                                "total_diff_lines": sum(1 for tag, _, _, _, _ in opcodes if tag != "equal"),
                                "truncated": total_lines > DIFF_MAX_LINES,
                            }
                            for tag, i1, i2, j1, j2 in opcodes:
                                left_chunk = left_content[i1:i2]
                                right_chunk = right_content[j1:j2]
                                max_len = max(len(left_chunk), len(right_chunk))
                                for k in range(max_len):
                                    l = left_chunk[k] if k < len(left_chunk) else ""
                                    r = right_chunk[k] if k < len(right_chunk) else ""
                                    diff_data["left_lines"].append((tag, l))
                                    diff_data["right_lines"].append((tag, r))
                                    diff_data["rows"].append({
                                        "tag": tag,
                                        "left": l,
                                        "right": r,
                                    })
                        except (OSError, PermissionError):
                            diff_data = {"error": "无法读取文件内容"}

                different.append({
                    "path": rel_path,
                    "left_size": l_info["size"],
                    "right_size": r_info["size"],
                    "left_mtime": l_info.get("mtime", 0),
                    "right_mtime": r_info.get("mtime", 0),
                    "left_md5": l_md5,
                    "right_md5": r_md5,
                    "left_verify_method": l_method,
                    "right_verify_method": r_method,
                    "left_error": l_info.get("error", ""),
                    "right_error": r_info.get("error", ""),
                    "diff": diff_data,
                    "delta": delta,
                })

    return {
        "only_left": only_left,
        "only_right": only_right,
        "different": different,
        "identical": identical,
        "deleted": deleted,
        "total_left": len(left_snapshot),
        "total_right": len(right_snapshot),
    }


def html_escape(text):
    if not isinstance(text, str):
        text = str(text)
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;"))


def json_escape_js(obj):
    return json.dumps(obj, ensure_ascii=False, default=str)


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
                  show_diff: bool, rsync_commands: list, verify_mode: str) -> str:
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
        rsync_html += f'<pre class="rsync-cmd">{html_escape(cmd)}</pre>'

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    report_json = json_escape_js(report)

    verify_badge = "可靠模式" if verify_mode == "reliable" else "快速模式"
    verify_class = "badge-reliable" if verify_mode == "reliable" else "badge-fast"

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
.tabs {{ display: flex; gap: 0; padding: 0 32px; background: white; border-bottom: 2px solid #e8ecf1; flex-wrap: wrap; }}
.tab-btn {{ padding: 12px 20px; border: none; background: none; cursor: pointer; font-size: 14px; color: #666; border-bottom: 2px solid transparent; margin-bottom: -2px; transition: all 0.2s; }}
.tab-btn:hover {{ color: #333; }}
.tab-btn.active {{ color: #667eea; border-bottom-color: #667eea; font-weight: 600; }}
.tab-content {{ display: none; padding: 24px 32px; }}
.tab-content.active {{ display: block; }}
.container {{ max-width: 1600px; margin: 0 auto; }}
table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
th, td {{ padding: 10px 14px; text-align: left; border-bottom: 1px solid #f0f2f5; font-size: 13px; }}
th {{ background: #f8f9fb; font-weight: 600; color: #555; white-space: nowrap; position: sticky; top: 0; z-index: 1; }}
tr:hover {{ background: #f8f9fb; }}
.file-path {{ font-family: "SF Mono", "Cascadia Code", Consolas, monospace; font-size: 12px; word-break: break-all; }}
.md5-cell {{ font-family: monospace; font-size: 11px; color: #888; max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; margin-left: 4px; }}
.badge-new {{ background: #fef3c7; color: #92400e; }}
.badge-resolved {{ background: #d1fae5; color: #065f46; }}
.badge-deleted {{ background: #fee2e2; color: #991b1b; }}
.badge-moved {{ background: #dbeafe; color: #1e40af; }}
.badge-modified {{ background: #ede9fe; color: #5b21b6; }}
.badge-reliable {{ background: #d1fae5; color: #065f46; }}
.badge-fast {{ background: #fef3c7; color: #92400e; }}
.badge-full {{ background: #d1fae5; color: #065f46; font-size: 10px; }}
.badge-sample {{ background: #fef3c7; color: #92400e; font-size: 10px; }}
.btn {{ display: inline-block; padding: 6px 16px; border: 1px solid #d1d5db; border-radius: 6px; background: white; cursor: pointer; font-size: 13px; transition: all 0.15s; }}
.btn:hover {{ background: #f3f4f6; }}
.btn-primary {{ background: #667eea; color: white; border-color: #667eea; }}
.btn-primary:hover {{ background: #5a6fd6; }}
.btn-toggle-diff {{ padding: 2px 10px; font-size: 11px; border: 1px solid #d1d5db; border-radius: 4px; background: #f9fafb; cursor: pointer; }}
.btn-toggle-diff:hover {{ background: #e5e7eb; }}
.diff-panel {{ margin-top: 8px; border: 1px solid #e5e7eb; border-radius: 8px; overflow: hidden; }}
.diff-toolbar {{ display: flex; gap: 6px; padding: 8px 12px; background: #f8f9fb; border-bottom: 1px solid #e5e7eb; align-items: center; flex-wrap: wrap; }}
.diff-toolbar .filter-btn {{ padding: 3px 10px; font-size: 11px; border: 1px solid #d1d5db; border-radius: 12px; background: white; cursor: pointer; }}
.diff-toolbar .filter-btn.active {{ background: #667eea; color: white; border-color: #667eea; }}
.diff-toolbar .filter-btn:hover {{ background: #e5e7eb; }}
.diff-toolbar .filter-btn.active:hover {{ background: #5a6fd6; }}
.diff-toolbar .diff-info {{ font-size: 11px; color: #888; margin-left: auto; }}
.diff-sidebyside {{ display: flex; gap: 0; max-height: 500px; overflow: auto; font-family: "SF Mono", "Cascadia Code", Consolas, monospace; font-size: 12px; }}
.diff-sidebyside.collapsed {{ max-height: 200px; }}
.diff-col {{ flex: 1; min-width: 0; }}
.diff-col-header {{ padding: 6px 12px; background: #f1f5f9; font-weight: 600; font-size: 11px; color: #555; border-bottom: 1px solid #e5e7eb; }}
.diff-line {{ display: flex; min-height: 20px; border-bottom: 1px solid #f8f9fb; }}
.diff-line-num {{ width: 45px; text-align: right; padding: 1px 8px; color: #999; font-size: 11px; flex-shrink: 0; user-select: none; }}
.diff-line-content {{ flex: 1; padding: 1px 8px; white-space: pre; overflow-x: auto; font-size: 11px; }}
.diff-line-eq {{ background: white; }}
.diff-line-eq .diff-line-content {{ color: #374151; }}
.diff-line-del {{ background: #fef2f2; }}
.diff-line-del .diff-line-content {{ color: #dc2626; }}
.diff-line-del .diff-line-num {{ background: #fee2e2; }}
.diff-line-add {{ background: #f0fdf4; }}
.diff-line-add .diff-line-content {{ color: #16a34a; }}
.diff-line-add .diff-line-num {{ background: #dcfce7; }}
.diff-line-replace {{ background: #fffbeb; }}
.diff-line-replace .diff-line-content {{ color: #92400e; }}
.diff-line-empty {{ background: #f9fafb; }}
.diff-collapse-btn {{ display: block; width: 100%; padding: 8px; text-align: center; font-size: 12px; color: #667eea; background: #f8f9fb; border: none; border-top: 1px solid #e5e7eb; cursor: pointer; }}
.diff-collapse-btn:hover {{ background: #e5e7eb; }}
.rsync-cmd {{ background: #1e1e1e; color: #6ee7b7; padding: 12px 16px; border-radius: 6px; margin: 8px 0; font-size: 12px; overflow-x: auto; white-space: pre-wrap; word-break: break-all; font-family: "SF Mono", "Cascadia Code", Consolas, monospace; }}
.toolbar {{ display: flex; gap: 8px; padding: 16px 32px; flex-wrap: wrap; align-items: center; }}
.toolbar .spacer {{ flex: 1; }}
.search-box {{ padding: 6px 12px; border: 1px solid #d1d5db; border-radius: 6px; font-size: 13px; width: 240px; }}
.stats-info {{ font-size: 12px; color: #888; }}
.delta-summary {{ display: flex; gap: 8px; padding: 12px 32px; background: #fffbeb; border-bottom: 1px solid #fde68a; flex-wrap: wrap; align-items: center; font-size: 13px; }}
.delta-summary .delta-item {{ padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }}
.delta-hidden {{ display: none; }}

@media (max-width: 768px) {{
    .summary {{ flex-direction: column; }}
    .tabs {{ overflow-x: auto; }}
    .diff-sidebyside {{ flex-direction: column; max-height: 600px; }}
}}
</style>
</head>
<body>
<div class="container">
<div class="header">
    <h1>文件夹对比报告</h1>
    <div class="subtitle">
        左边: {html_escape(left_root)} &nbsp;|&nbsp; 右边: {html_escape(right_root)} &nbsp;|&nbsp; 生成时间: {now_str}
        &nbsp;|&nbsp; <span class="badge {verify_class}">{verify_badge}</span>
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
    <input type="text" class="search-box" id="searchInput" placeholder="过滤文件名..." oninput="filterTable()">
    <button class="btn" onclick="exportCSV()">导出CSV</button>
    <button class="btn" onclick="exportJSON()">导出JSON</button>
    <button class="btn" id="btnSync" onclick="toggleSyncPanel()">同步命令</button>
    <span class="spacer"></span>
    <span class="stats-info">左边 {report["total_left"]} 个文件 | 右边 {report["total_right"]} 个文件</span>
</div>

<div id="syncPanel" style="display:none; padding: 0 32px 16px 32px;">
    <h3 style="margin-bottom:8px;">建议的 rsync 命令</h3>
    {rsync_html}
    <p style="font-size:12px;color:#888;margin-top:4px;">这些命令仅供参考，请根据实际需求手动执行。</p>
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
                <th style="width:90px;">状态</th>
                <th>文件路径</th>
                <th style="width:100px;">大小</th>
                <th style="width:160px;">修改时间</th>
                <th style="width:140px;">校验</th>
                <th style="width:150px;">MD5</th>
            </tr>
        </thead>
        <tbody id="tbody-left"></tbody>
    </table>
</div>

<div id="tab-right" class="tab-content">
    <table>
        <thead>
            <tr>
                <th style="width:90px;">状态</th>
                <th>文件路径</th>
                <th style="width:100px;">大小</th>
                <th style="width:160px;">修改时间</th>
                <th style="width:140px;">校验</th>
                <th style="width:150px;">MD5</th>
            </tr>
        </thead>
        <tbody id="tbody-right"></tbody>
    </table>
</div>

<div id="tab-diff" class="tab-content">
    <table>
        <thead>
            <tr>
                <th style="width:90px;">状态</th>
                <th>文件路径</th>
                <th style="width:120px;">大小 (左/右)</th>
                <th style="width:260px;">修改时间 (左/右)</th>
                <th style="width:140px;">校验</th>
                <th style="width:200px;">MD5 (左 / 右)</th>
                <th style="width:100px;">操作</th>
            </tr>
        </thead>
        <tbody id="tbody-diff"></tbody>
    </table>
</div>

<div id="tab-same" class="tab-content">
    <table>
        <thead>
            <tr>
                <th style="width:90px;">状态</th>
                <th>文件路径</th>
                <th style="width:100px;">大小</th>
                <th style="width:140px;">校验</th>
                <th style="width:150px;">MD5</th>
            </tr>
        </thead>
        <tbody id="tbody-same"></tbody>
    </table>
</div>

</div>

<script>
const REPORT = {report_json};
const LEFT = {json_escape_js(left_root)};
const RIGHT = {json_escape_js(right_root)};
const DELTA_LABELS = {json_escape_js(DELTA_LABELS)};
const DIFF_COLLAPSE_THRESHOLD = {DIFF_COLLAPSE_THRESHOLD};

function formatSize(size) {{
    if (size < 0) return "N/A";
    var units = ["B", "KB", "MB", "GB", "TB"];
    var s = size;
    for (var i = 0; i < units.length; i++) {{
        if (Math.abs(s) < 1024) return (i === 0 ? s : s.toFixed(1) + " " + units[i]);
        s /= 1024;
    }}
    return s.toFixed(1) + " PB";
}}

function formatTime(ts) {{
    if (ts <= 0) return "N/A";
    return new Date(ts * 1000).toLocaleString("zh-CN", {{hour12: false}});
}}

function escHtml(s) {{
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}}

function deltaBadge(delta) {{
    if (!delta) return "";
    var dl = DELTA_LABELS[delta];
    if (!dl || !dl[0]) return "";
    return ' <span class="badge ' + dl[1] + '">' + dl[0] + '</span>';
}}

function verifyBadge(method) {{
    if (method === "full") return ' <span class="badge badge-full">完整</span>';
    if (method === "sample") return ' <span class="badge badge-sample">采样</span>';
    return "";
}}

function escapeCSV(val) {{
    var s = String(val == null ? "" : val);
    if (s.includes(",") || s.includes('"') || s.includes("\\n") || s.includes("\\r")) {{
        return '"' + s.replace(/"/g, '""') + '"';
    }}
    return s;
}}

function buildLeftRows() {{
    var rows = [];
    REPORT.only_left.forEach(function(item) {{
        rows.push(
            '<tr>' +
            '<td>' + deltaBadge(item.delta) + '</td>' +
            '<td class="file-path">' + escHtml(item.path) + '</td>' +
            '<td>' + formatSize(item.size) + '</td>' +
            '<td>' + formatTime(item.mtime) + '</td>' +
            '<td>' + verifyBadge(item.verify_method) + '</td>' +
            '<td class="md5-cell" title="' + escHtml(item.md5 || "") + '">' + escHtml((item.md5 || "").substring(0, 16)) + '...</td>' +
            '</tr>'
        );
    }});
    document.getElementById("tbody-left").innerHTML = rows.join("");
}}

function buildRightRows() {{
    var rows = [];
    REPORT.only_right.forEach(function(item) {{
        rows.push(
            '<tr>' +
            '<td>' + deltaBadge(item.delta) + '</td>' +
            '<td class="file-path">' + escHtml(item.path) + '</td>' +
            '<td>' + formatSize(item.size) + '</td>' +
            '<td>' + formatTime(item.mtime) + '</td>' +
            '<td>' + verifyBadge(item.verify_method) + '</td>' +
            '<td class="md5-cell" title="' + escHtml(item.md5 || "") + '">' + escHtml((item.md5 || "").substring(0, 16)) + '...</td>' +
            '</tr>'
        );
    }});
    document.getElementById("tbody-right").innerHTML = rows.join("");
}}

function buildDiffDiffHTML(item, diffId) {{
    var d = item.diff;
    if (!d) return "";
    if (d.error) return '<div class="diff-panel"><div style="padding:12px;color:#999;">' + escHtml(d.error) + '</div></div>';

    var isLarge = d.total_diff_lines > DIFF_COLLAPSE_THRESHOLD;
    var collapsedClass = isLarge ? "collapsed" : "";
    var collapseBtn = isLarge ? '<button class="diff-collapse-btn" onclick="toggleCollapse(\'' + diffId + '-panel\')">展开全部差异 (' + d.total_diff_lines + ' 行)</button>' : "";

    var leftLines = [];
    var rightLines = [];
    var leftNum = 0;
    var rightNum = 0;

    d.rows.forEach(function(row) {{
        var lc = row.tag === "equal" ? "eq" : (row.tag === "delete" ? "del" : (row.tag === "insert" ? "add" : "replace"));
        var rc = row.tag === "equal" ? "eq" : (row.tag === "delete" ? "del" : (row.tag === "insert" ? "add" : "replace"));

        if (row.tag === "delete") {{
            leftNum++;
            leftLines.push('<div class="diff-line diff-line-' + lc + '" data-tag="' + row.tag + '"><span class="diff-line-num">' + leftNum + '</span><span class="diff-line-content">' + escHtml(row.left) + '</span></div>');
            rightLines.push('<div class="diff-line diff-line-empty" data-tag="' + row.tag + '"><span class="diff-line-num"></span><span class="diff-line-content"></span></div>');
        }} else if (row.tag === "insert") {{
            rightNum++;
            leftLines.push('<div class="diff-line diff-line-empty" data-tag="' + row.tag + '"><span class="diff-line-num"></span><span class="diff-line-content"></span></div>');
            rightLines.push('<div class="diff-line diff-line-' + rc + '" data-tag="' + row.tag + '"><span class="diff-line-num">' + rightNum + '</span><span class="diff-line-content">' + escHtml(row.right) + '</span></div>');
        }} else if (row.tag === "replace") {{
            leftNum++;
            rightNum++;
            leftLines.push('<div class="diff-line diff-line-' + lc + '" data-tag="' + row.tag + '"><span class="diff-line-num">' + leftNum + '</span><span class="diff-line-content">' + escHtml(row.left) + '</span></div>');
            rightLines.push('<div class="diff-line diff-line-' + rc + '" data-tag="' + row.tag + '"><span class="diff-line-num">' + rightNum + '</span><span class="diff-line-content">' + escHtml(row.right) + '</span></div>');
        }} else {{
            leftNum++;
            rightNum++;
            leftLines.push('<div class="diff-line diff-line-eq" data-tag="equal"><span class="diff-line-num">' + leftNum + '</span><span class="diff-line-content">' + escHtml(row.left) + '</span></div>');
            rightLines.push('<div class="diff-line diff-line-eq" data-tag="equal"><span class="diff-line-num">' + rightNum + '</span><span class="diff-line-content">' + escHtml(row.right) + '</span></div>');
        }}
    }});

    var truncNote = d.truncated ? '<span style="color:#f59e0b;">(已截断)</span> ' : "";

    return '<div class="diff-panel">' +
        '<div class="diff-toolbar">' +
        '<button class="filter-btn active" onclick="filterDiff(\'' + diffId + '\', \'all\', this)">全部</button>' +
        '<button class="filter-btn" onclick="filterDiff(\'' + diffId + '\', \'insert\', this)">仅新增</button>' +
        '<button class="filter-btn" onclick="filterDiff(\'' + diffId + '\', \'delete\', this)">仅删除</button>' +
        '<button class="filter-btn" onclick="filterDiff(\'' + diffId + '\', \'replace\', this)">仅修改</button>' +
        '<span class="diff-info">' + truncNote + d.total_diff_lines + ' 处差异</span>' +
        '</div>' +
        '<div class="diff-sidebyside ' + collapsedClass + '" id="' + diffId + '-panel">' +
        '<div class="diff-col"><div class="diff-col-header">左边</div><div id="' + diffId + '-left">' + leftLines.join("") + '</div></div>' +
        '<div class="diff-col"><div class="diff-col-header">右边</div><div id="' + diffId + '-right">' + rightLines.join("") + '</div></div>' +
        '</div>' +
        collapseBtn +
        '</div>';
}}

function buildDiffRows() {{
    var rows = [];
    REPORT.different.forEach(function(item) {{
        var diffId = "diff-" + Math.random().toString(36).substring(2, 10);
        var toggleBtn = "";
        var diffHtml = "";
        if (item.diff) {{
            toggleBtn = '<button class="btn-toggle-diff" onclick="toggleDiffPanel(\'' + diffId + '\')">查看差异</button>';
            diffHtml = '<tr id="' + diffId + '-row" style="display:none;"><td colspan="7">' + buildDiffDiffHTML(item, diffId) + '</td></tr>';
        }}
        rows.push(
            '<tr class="diff-header-row" data-diff-id="' + diffId + '">' +
            '<td>' + deltaBadge(item.delta) + '</td>' +
            '<td class="file-path">' + escHtml(item.path) + '</td>' +
            '<td>' + formatSize(item.left_size) + ' / ' + formatSize(item.right_size) + '</td>' +
            '<td>' + formatTime(item.left_mtime) + ' / ' + formatTime(item.right_mtime) + '</td>' +
            '<td>' + verifyBadge(item.left_verify_method) + ' ' + verifyBadge(item.right_verify_method) + '</td>' +
            '<td class="md5-cell" title="Left: ' + escHtml(item.left_md5 || "") + '&#10;Right: ' + escHtml(item.right_md5 || "") + '">' + escHtml((item.left_md5 || "").substring(0, 8)) + ' / ' + escHtml((item.right_md5 || "").substring(0, 8)) + '</td>' +
            '<td>' + toggleBtn + '</td>' +
            '</tr>' + diffHtml
        );
    }});
    document.getElementById("tbody-diff").innerHTML = rows.join("");
}}

function buildSameRows() {{
    var rows = [];
    REPORT.identical.forEach(function(item) {{
        rows.push(
            '<tr>' +
            '<td>' + deltaBadge(item.delta) + '</td>' +
            '<td class="file-path">' + escHtml(item.path) + '</td>' +
            '<td>' + formatSize(item.size) + '</td>' +
            '<td>' + verifyBadge(item.verify_method) + '</td>' +
            '<td class="md5-cell" title="' + escHtml(item.md5 || "") + '">' + escHtml((item.md5 || "").substring(0, 16)) + '...</td>' +
            '</tr>'
        );
    }});
    document.getElementById("tbody-same").innerHTML = rows.join("");
}}

buildLeftRows();
buildRightRows();
buildDiffRows();
buildSameRows();

function switchTab(tabId) {{
    document.querySelectorAll(".tab-btn").forEach(function(b) {{ b.classList.remove("active"); }});
    document.querySelectorAll(".tab-content").forEach(function(c) {{ c.classList.remove("active"); }});
    document.getElementById(tabId).classList.add("active");
    event.target.classList.add("active");
}}

function toggleSyncPanel() {{
    var panel = document.getElementById("syncPanel");
    panel.style.display = panel.style.display === "none" ? "block" : "none";
}}

function toggleDiffPanel(diffId) {{
    var row = document.getElementById(diffId + "-row");
    if (row) {{
        var visible = row.style.display !== "none";
        row.style.display = visible ? "none" : "table-row";
        var btns = document.querySelectorAll('[data-diff-id="' + diffId + '"] .btn-toggle-diff');
        btns.forEach(function(btn) {{
            btn.textContent = visible ? "查看差异" : "隐藏差异";
        }});
    }}
}}

function toggleCollapse(panelId) {{
    var panel = document.getElementById(panelId);
    if (panel) {{
        var collapsed = panel.classList.contains("collapsed");
        panel.classList.toggle("collapsed");
        var btn = panel.nextElementSibling;
        if (btn) {{
            btn.textContent = collapsed ? "折叠差异" : "展开全部差异";
        }}
    }}
}}

function filterDiff(diffId, filter, btn) {{
    var panel = document.getElementById(diffId + "-panel");
    if (!panel) return;
    var lines = panel.querySelectorAll(".diff-line");
    lines.forEach(function(line) {{
        var tag = line.getAttribute("data-tag");
        if (filter === "all" || tag === filter) {{
            line.style.display = "flex";
        }} else {{
            line.style.display = "none";
        }}
    }});
    var toolbar = btn.parentElement;
    toolbar.querySelectorAll(".filter-btn").forEach(function(b) {{ b.classList.remove("active"); }});
    btn.classList.add("active");
}}

function filterTable() {{
    var query = document.getElementById("searchInput").value.toLowerCase();
    document.querySelectorAll(".tab-content.active tbody tr").forEach(function(row) {{
        var text = row.textContent.toLowerCase();
        if (query === "" || text.includes(query)) {{
            row.style.display = "";
        }} else {{
            row.style.display = "none";
            if (row.classList.contains("diff-header-row")) {{
                var diffId = row.getAttribute("data-diff-id");
                var diffRow = document.getElementById(diffId + "-row");
                if (diffRow) diffRow.style.display = "none";
            }}
        }}
    }});
}}

function exportCSV() {{
    var csv = "\\uFEFF状态,文件路径,大小_左,大小_右,修改时间_左,修改时间_右,MD5_左,MD5_右,校验方式,错误\\n";
    REPORT.only_left.forEach(function(item) {{
        csv += "仅在左边," + escapeCSV(item.path) + "," + escapeCSV(item.size) + ",,,," + escapeCSV(item.md5) + ",," + escapeCSV(item.verify_method) + "," + escapeCSV(item.error || "") + "\\n";
    }});
    REPORT.only_right.forEach(function(item) {{
        csv += "仅在右边," + escapeCSV(item.path) + ",,,," + escapeCSV(item.size) + ",," + escapeCSV(item.md5) + "," + escapeCSV(item.verify_method) + "," + escapeCSV(item.error || "") + "\\n";
    }});
    REPORT.different.forEach(function(item) {{
        csv += "内容不同," + escapeCSV(item.path) + "," + escapeCSV(item.left_size) + "," + escapeCSV(item.right_size) + "," + escapeCSV(item.left_mtime) + "," + escapeCSV(item.right_mtime) + "," + escapeCSV(item.left_md5) + "," + escapeCSV(item.right_md5) + "," + escapeCSV(item.left_verify_method + "/" + item.right_verify_method) + "," + escapeCSV((item.left_error||"") + " | " + (item.right_error||"")) + "\\n";
    }});
    REPORT.identical.forEach(function(item) {{
        csv += "完全相同," + escapeCSV(item.path) + "," + escapeCSV(item.size) + "," + escapeCSV(item.size) + ",,,," + escapeCSV(item.md5) + "," + escapeCSV(item.md5) + "," + escapeCSV(item.verify_method) + ",\\n";
    }});
    downloadBlob(csv, "folder_diff_report.csv", "text/csv;charset=utf-8;");
}}

function exportJSON() {{
    var json = JSON.stringify(REPORT, null, 2);
    downloadBlob(json, "folder_diff_report.json", "application/json;charset=utf-8;");
}}

function downloadBlob(content, filename, mimeType) {{
    var blob = new Blob([content], {{type: mimeType}});
    var link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = filename;
    link.click();
}}
</script>
</body>
</html>'''


def export_csv(report: dict, csv_path: str) -> None:
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_ALL)
        writer.writerow(["状态", "文件路径", "大小_左", "大小_右", "修改时间_左", "修改时间_右",
                          "MD5_左", "MD5_右", "校验方式", "错误"])
        for item in report["only_left"]:
            writer.writerow(["仅在左边", item["path"], item["size"], "", item.get("mtime", ""), "",
                             item.get("md5", ""), "", item.get("verify_method", ""),
                             item.get("error", "")])
        for item in report["only_right"]:
            writer.writerow(["仅在右边", item["path"], "", "", "", item.get("mtime", ""),
                             "", item.get("md5", ""), item.get("verify_method", ""),
                             item.get("error", "")])
        for item in report["different"]:
            writer.writerow(["内容不同", item["path"], item["left_size"], item["right_size"],
                             item.get("left_mtime", ""), item.get("right_mtime", ""),
                             item.get("left_md5", ""), item.get("right_md5", ""),
                             f'{item.get("left_verify_method", "")}/{item.get("right_verify_method", "")}',
                             f'{item.get("left_error", "")} | {item.get("right_error", "")}'])
        for item in report["identical"]:
            writer.writerow(["完全相同", item["path"], item["size"], item["size"], "", "",
                             item.get("md5", ""), item.get("md5", ""),
                             item.get("verify_method", ""), ""])


def export_json(report: dict, json_path: str) -> None:
    output = {
        "generated_at": datetime.now().isoformat(),
        "summary": {
            "only_left": len(report["only_left"]),
            "only_right": len(report["only_right"]),
            "different": len(report["different"]),
            "identical": len(report["identical"]),
            "total_left": report["total_left"],
            "total_right": report["total_right"],
        },
        "only_left": [{k: v for k, v in item.items() if k != "diff"}
                       for item in report["only_left"]],
        "only_right": [{k: v for k, v in item.items() if k != "diff"}
                        for item in report["only_right"]],
        "different": [{k: v for k, v in item.items() if k != "diff"}
                       for item in report["different"]],
        "identical": [{k: v for k, v in item.items() if k != "diff"}
                       for item in report["identical"]],
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False, default=str)


def build_ignore_rules(args) -> list:
    rules = []

    if args.ignore_config:
        rules.extend(load_ignore_config(args.ignore_config))

    if args.ignore_hidden:
        rules.append(IgnoreRule(hidden=True))

    for ext in args.ignore_ext:
        ext = ext.lower()
        if not ext.startswith("."):
            ext = "." + ext
        rules.append(IgnoreRule(glob_pattern="*" + ext))

    for pattern in args.ignore:
        if pattern.startswith(".") and "/" not in pattern:
            rules.append(IgnoreRule(glob_pattern="*" + pattern))
        elif pattern.endswith("/") or pattern.endswith("\\"):
            rules.append(IgnoreRule(directory=pattern))
        else:
            rules.append(IgnoreRule(glob_pattern=pattern))

    return rules


def main():
    parser = argparse.ArgumentParser(
        description="文件夹对比工具 - 对比两个文件夹并生成HTML报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python folder_diff.py --left ./dir_a --right ./dir_b
  python folder_diff.py --left ./dir_a --right ./dir_b --ignore-ext .log .tmp --ignore-hidden
  python folder_diff.py --left ./dir_a --right ./dir_b --show-diff --verify-mode fast
  python folder_diff.py --left ./dir_a --right ./dir_b --ignore "*.log" "node_modules/" --ignore-config ignore.json
  python folder_diff.py --left ./dir_a --right ./dir_b --export-csv report.csv --export-json report.json
  python folder_diff.py --left ./dir_a --right ./dir_b --cache ./snapshot.json

忽略规则配置文件格式 (JSON):
  [
    "*.log",
    "node_modules/",
    {{"glob": "*.tmp", "min_size": 0, "max_size": 1024}},
    {{"directory": "build/", "hidden": true}}
  ]
        """,
    )
    parser.add_argument("--left", required=True, help="左边文件夹路径")
    parser.add_argument("--right", required=True, help="右边文件夹路径")
    parser.add_argument("--output", "-o", default="folder_diff_report.html",
                        help="HTML报告输出路径 (默认: folder_diff_report.html)")
    parser.add_argument("--ignore-ext", nargs="*", default=[],
                        help="忽略的扩展名，如 .log .tmp")
    parser.add_argument("--ignore", nargs="*", default=[],
                        help="忽略的通配符模式或目录，如 '*.log' 'node_modules/'")
    parser.add_argument("--ignore-config",
                        help="忽略规则配置文件路径 (JSON格式)")
    parser.add_argument("--ignore-hidden", action="store_true",
                        help="忽略隐藏文件（以.开头的文件/目录）")
    parser.add_argument("--verify-mode", choices=["reliable", "fast"], default="reliable",
                        help="校验模式: reliable=完整MD5(默认), fast=大文件采样MD5")
    parser.add_argument("--sample-threshold", type=int, default=1048576,
                        help="采样MD5阈值（字节），仅在fast模式下对超过此大小的文件使用采样 (默认: 1MB)")
    parser.add_argument("--show-diff", action="store_true",
                        help="在报告中展示文本文件的内容差异（左右并排）")
    parser.add_argument("--export-csv", help="导出CSV文件到指定路径")
    parser.add_argument("--export-json", help="导出JSON文件到指定路径")
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

    rules = build_ignore_rules(args)
    verify_mode = args.verify_mode

    print(f"正在扫描左边文件夹: {left_root}  [校验模式: {verify_mode}]")
    left_snapshot = scan_folder(left_root, rules, verify_mode, args.sample_threshold)
    print(f"  找到 {len(left_snapshot)} 个文件")

    print(f"正在扫描右边文件夹: {right_root}  [校验模式: {verify_mode}]")
    right_snapshot = scan_folder(right_root, rules, verify_mode, args.sample_threshold)
    print(f"  找到 {len(right_snapshot)} 个文件")

    prev_cache = {}
    if args.cache:
        prev_cache = load_cache(args.cache)
        if prev_cache:
            print(f"已加载缓存快照: {args.cache} ({prev_cache.get('timestamp', 'unknown')})")

    print("正在对比文件...")
    report = compare(
        left_snapshot, right_snapshot, prev_cache,
        args.sample_threshold, left_root, right_root, args.show_diff, verify_mode,
    )

    rsync_commands = generate_rsync_commands(
        report["only_left"], report["only_right"], report["different"],
        left_root, right_root,
    )

    print(f"正在生成HTML报告: {args.output}")
    html = generate_html(report, left_root, right_root, args.show_diff, rsync_commands, verify_mode)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html)

    if args.export_csv:
        print(f"正在导出CSV: {args.export_csv}")
        export_csv(report, args.export_csv)

    if args.export_json:
        print(f"正在导出JSON: {args.export_json}")
        export_json(report, args.export_json)

    if args.cache and not args.no_cache:
        save_cache(args.cache, left_snapshot, right_snapshot, report)
        print(f"已保存缓存快照: {args.cache}")

    delta_stats = {}
    for cat in ["only_left", "only_right", "different", "identical"]:
        for item in report[cat]:
            d = item.get("delta")
            if d:
                delta_stats[d] = delta_stats.get(d, 0) + 1

    print()
    print("=" * 60)
    print("  对比完成！")
    print(f"  仅在左边:  {len(report['only_left'])} 个文件")
    print(f"  仅在右边:  {len(report['only_right'])} 个文件")
    print(f"  内容不同:  {len(report['different'])} 个文件")
    print(f"  完全相同:  {len(report['identical'])} 个文件")
    if delta_stats:
        print("  --- 增量变化 ---")
        for d, count in sorted(delta_stats.items()):
            label, _ = DELTA_LABELS.get(d, (d, ""))
            if label:
                print(f"  {label}: {count}")
    print(f"  HTML报告:  {os.path.abspath(args.output)}")
    if args.export_csv:
        print(f"  CSV导出:   {os.path.abspath(args.export_csv)}")
    if args.export_json:
        print(f"  JSON导出:  {os.path.abspath(args.export_json)}")
    print("=" * 60)


if __name__ == "__main__":
    main()