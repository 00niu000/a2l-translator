#!/usr/bin/env python3
"""
A2L 翻译工具 — 翻译记忆库 & 自定义词典模块
============================================
- 翻译记忆库（TM）: 确保同一原文总得到同一译文
- 自定义词典: 用户手动添加术语，最高优先级
- 自动学习: 翻译完成后自动更新 TM
"""

import json
from pathlib import Path

_SRC = Path(__file__).parent


def _get_tm_path():
    return _SRC / "translation_memory.json"


def _get_custom_glossary_path():
    return _SRC / "custom_glossary.json"


# ══════════════════════════════════════════════════════════
#  翻译记忆库 (TM)
# ══════════════════════════════════════════════════════════

def load_translation_memory():
    """加载翻译记忆库（JSON: {原文: 译文, ...}）"""
    tm_path = _get_tm_path()
    if tm_path.exists():
        try:
            with open(tm_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_translation_memory(tm):
    """保存翻译记忆库"""
    try:
        with open(_get_tm_path(), 'w', encoding='utf-8') as f:
            json.dump(tm, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def apply_tm(items, tm, show_progress=True):
    """
    应用翻译记忆库（精确匹配，最高优先级）。
    返回命中条数。
    """
    if not tm:
        return 0
    count = 0
    for item in items:
        if item.get("translated"):
            continue
        key = item["original"].strip()
        if key in tm and tm[key]:
            item["translated"] = tm[key]
            item["status"] = "tm"
            count += 1
    if show_progress and count:
        print(f"  翻译记忆: {count} 条命中")
    return count


def update_tm_from_items(tm, items):
    """
    从已翻译条目更新记忆库（只添加不覆盖）。
    返回新增条数。
    """
    updated = 0
    for item in items:
        key = item["original"].strip()
        trans = (item.get("translated") or "").strip()
        if trans and key not in tm:
            tm[key] = trans
            updated += 1
    if updated:
        save_translation_memory(tm)
    return updated


# ══════════════════════════════════════════════════════════
#  自定义词典
# ══════════════════════════════════════════════════════════

def load_custom_glossary():
    """
    加载用户自定义术语词典。
    格式: {"英文术语": "中文翻译", ...}
    优先级高于内建词典。
    """
    cust_path = _get_custom_glossary_path()
    if cust_path.exists():
        try:
            with open(cust_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_custom_glossary(glossary):
    """保存用户自定义词典"""
    try:
        with open(_get_custom_glossary_path(), 'w', encoding='utf-8') as f:
            json.dump(glossary, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def merge_glossary(base, custom):
    """合并词典：自定义覆盖内建"""
    merged = dict(base)
    merged.update(custom)
    return merged
