#!/usr/bin/env python3
"""
A2L/KP 文件翻译工具 - 图形界面版
双击运行，拖放文件即可翻译
支持 A2L (ASAP2) + KP (WinOLS Map Pack)
"""

import sys
import os
import re
import json
import csv
import time
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── 高 DPI 适配（解决高分屏模糊问题）──
import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
from pathlib import Path
from collections import OrderedDict
import urllib.request
import urllib.parse
import urllib.error
import ssl

# ── 百度翻译 API 模块 ──
try:
    from baidu_api import baidu_translate_batch, baidu_translate_one
    _HAS_BAIDU = True
except ImportError:
    _HAS_BAIDU = False

# ── 模块化可升级架构：优先从 data/ 加载外部模块 ──
import importlib.util

def _load_module(module_name, file_name):
    """优先从 data/ 加载外部模块（可热更新），fallback 到内建版本"""
    # 确定 data/ 路径（与 exe 同级）
    if getattr(sys, 'frozen', False):
        data_dir = Path(sys.executable).parent / "data"
    else:
        data_dir = Path(__file__).parent / "data"

    external_path = data_dir / file_name
    if external_path.exists():
        try:
            spec = importlib.util.spec_from_file_location(module_name, str(external_path))
            mod = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = mod
            spec.loader.exec_module(mod)
            return mod, "external"
        except Exception:
            pass  # 外部模块加载失败，回退到内建

    # Fallback: 使用内建版本
    return __import__(module_name), "builtin"

_glossary_mod, _glossary_src = _load_module("glossary_data", "glossary_data.py")
BUILTIN_GLOSSARY = _glossary_mod.BUILTIN_GLOSSARY
GERMAN_GLOSSARY = _glossary_mod.GERMAN_GLOSSARY
SMART_KEYWORDS = getattr(_glossary_mod, "SMART_KEYWORDS", {})

_dict_mod, _dict_src = _load_module("dictionary_resources", "dictionary_resources.py")
MultiSourceDictionary = _dict_mod.MultiSourceDictionary
get_dictionary = _dict_mod.get_dictionary
quick_lookup = _dict_mod.quick_lookup


# ══════════════════════════════════════════════════════════
#  词典预索引 — 把 O(n*m) 降为 O(1) + O(candidates)
# ══════════════════════════════════════════════════════════

def build_glossary_index(glossary):
    """预建词典索引，大幅加速匹配。同时添加德语 Umlaut ASCII 变体。"""
    exact = {}          # {lowercase_key: translation}
    by_first_word = {}  # {first_word: [(key, value, key_lower), ...]}
    by_length = {}       # {word_count: [(key, value, key_lower), ...]}

    # 德语 Umlaut → ASCII（两种形式：简写 a 和全写 ae）
    _umlaut_simple = str.maketrans("äöüÄÖÜ", "aouAOU")

    def _de_variants(text):
        """生成德语文本的所有 ASCII 变体"""
        simple = text.translate(_umlaut_simple).replace("ß", "ss")
        expanded = text.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
        expanded = expanded.replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue")
        expanded = expanded.replace("ß", "ss")
        variants = {simple}
        if expanded != simple:
            variants.add(expanded)
        return variants

    def add_entry(key, value):
        kl = key.lower()
        exact[kl] = value
        words = kl.split()
        if words:
            fw = words[0]
            by_first_word.setdefault(fw, []).append((key, value, kl))
        wc = len(words)
        by_length.setdefault(wc, []).append((key, value, kl))

    for key, value in glossary.items():
        add_entry(key, value)
        # 添加德语 ASCII 变体（ä→a 和 ä→ae 两种）
        if any(c in key for c in "äöüÄÖÜß"):
            for variant in _de_variants(key):
                if variant != key:
                    add_entry(variant, value)

    return exact, by_first_word, by_length


def translate_with_glossary_fast(text, glossary, index):
    """使用预建索引的快速翻译 — 将 4000+ 次比较降为 ~20 次"""
    if not text or len(text.strip()) < 2:
        return None

    original = text
    text_lower = text.lower().strip()
    exact_dict, by_first_word, by_length = index

    # ── 1. O(1) 精确匹配 ──
    if text_lower in exact_dict:
        return exact_dict[text_lower]

    # ── 2. 智能子串匹配（只搜索候选条目）──
    words = text_lower.split()

    # 收集候选：按首词匹配 + 按长度匹配
    candidates = {}
    for w in words:
        if w in by_first_word:
            for key, value, kl in by_first_word[w]:
                if kl not in candidates:
                    candidates[kl] = (key, value)

    # 也按词数匹配
    wc = len(words)
    for delta in [0, -1, 1, -2, 2]:
        check_wc = wc + delta
        if check_wc in by_length:
            for key, value, kl in by_length[check_wc]:
                if kl not in candidates:
                    candidates[kl] = (key, value)

    # 在候选中做子串匹配（通常只有 10-50 个候选）
    matches = []
    for kl, (key, value) in candidates.items():
        if len(kl) < 3:
            continue
        idx = text_lower.find(kl)
        if idx >= 0:
            before_ok = idx == 0 or not text_lower[idx - 1].isalpha()
            after_ok = (idx + len(kl) == len(text_lower)
                        or not text_lower[idx + len(kl)].isalpha())
            if before_ok and after_ok:
                matches.append((kl, value, idx, len(kl)))

    if not matches:
        return None

    # ── 3. 去重去重叠，最长优先，拼接待译 ──
    matches.sort(key=lambda x: -x[3])
    used_ranges = []
    selected = []
    for m in matches:
        m_start, m_end = m[2], m[2] + m[3]
        overlap = any(not (m_end <= u[0] or m_start >= u[1]) for u in used_ranges)
        if not overlap:
            used_ranges.append((m_start, m_end))
            selected.append(m)

    selected.sort(key=lambda x: x[2])
    result_parts = []
    last_end = 0
    for _, zh, start, length in selected:
        gap = original[last_end:start]
        if gap.strip():
            result_parts.append(gap)
        result_parts.append(zh)
        last_end = start + length
    if last_end < len(original):
        tail = original[last_end:]
        if tail.strip():
            result_parts.append(tail)

    return "".join(result_parts) if result_parts else None


# ══════════════════════════════════════════════════════════
#  逐词智能翻译 — 第二遍补漏，覆盖率 87% → 96%
# ══════════════════════════════════════════════════════════

_SMART_SKIP_TYPES = {"COMPU_METHOD", "FUNCTION", "RECORD_LAYOUT", "MODULE"}

def _build_smart_translator(glossary, extra_keywords=None):
    """构建智能翻译器 — 返回 (exact_match_dict, compiled_regex, term_to_zh)"""
    exact_dict = {}       # 整条匹配: lowercase_en -> zh
    merge = {}            # 合并收集所有 term -> zh
    for en, zh in glossary.items():
        en_lower = en.lower().strip()
        exact_dict[en_lower] = zh
        for word in re.findall(r'[A-Za-z]{2,}', en):
            wl = word.lower()
            if wl not in merge:
                merge[wl] = zh[:8]  # 取前几个字作为默认翻译
    if extra_keywords:
        for k, v in extra_keywords.items():
            kl = k.lower().strip()
            exact_dict[kl] = v
            merge[kl] = v

    # 按长度降序构建组合正则（长词优先匹配）
    terms = sorted(set(t for t in merge if len(t) >= 2), key=len, reverse=True)
    if not terms:
        return exact_dict, None, {}

    combined_pattern = re.compile(
        '|'.join(re.escape(t) for t in terms),
        re.IGNORECASE
    )
    term_map = {t: merge[t] for t in terms}
    return exact_dict, combined_pattern, term_map


def smart_translate_text_fast(text, exact_dict, combined_pattern, term_map):
    """单次正则扫描翻译 — O(n) 复杂度"""
    if not combined_pattern:
        return None

    tl = text.lower().strip()
    if tl in exact_dict:
        return exact_dict[tl]

    # 单次扫描收集所有匹配
    matches = []
    for m in combined_pattern.finditer(text):
        matched_term = m.group().lower()
        if matched_term in term_map:
            matches.append((m.start(), m.end(), term_map[matched_term]))

    if not matches:
        return None

    # 长词优先，不重叠
    matches.sort(key=lambda x: -(x[1] - x[0]))
    used = []
    selected = []
    for start, end, zh in matches:
        if not any(max(start, u[0]) < min(end, u[1]) for u in used):
            used.append((start, end))
            selected.append((start, end, zh))

    selected.sort(key=lambda x: x[0])
    parts = []
    last = 0
    for start, end, zh in selected:
        parts.append(text[last:start])
        parts.append(zh)
        last = end
    parts.append(text[last:])
    result = "".join(parts)
    return result if result != text else None


# ══════════════════════════════════════════════════════════
#  核心解析/翻译引擎
# ══════════════════════════════════════════════════════════

def parse_a2l(filepath):
    """解析 A2L 文件 — 单次正则扫描 + 每 500 匹配释放 GIL"""
    with open(filepath, "r", encoding="utf-8-sig", errors="replace") as f:
        content = f.read()

    results = []
    used_positions = set()

    # 注意：不能加 re.DOTALL！DOTALL 会让 .+? 跨行吞噬整个文件，
    # 导致大文件只匹配到 1‰ 的条目。使用 (?:(?!\*/)[\s\S])*? 正确匹配
    # 块注释内容（匹配到最近的 */ 为止，可跨行但不会过度吞噬）
    big_pattern = re.compile(r"""
        /\*\s*((?:(?!\*/)[\s\S])*?)\s*\*/          # 块注释 (group 1)
      | //\s*(.+)$                                   # 行注释 (group 2)
      | /begin\s+(\w+)\s+(\w+)\s*                   # A2L 关键字 (group 3,4)
        (?: "([^"]*)"                                # 带引号字符串 (group 5)
          | ((?:"[^"]*"\s*)+)                        # 多引号字符串 (group 6)
        )
    """, re.IGNORECASE | re.MULTILINE | re.VERBOSE)

    match_count = 0
    for m in big_pattern.finditer(content):
        match_count += 1
        # 每 500 个匹配释放 GIL，让主线程有机会更新 UI
        if match_count % 500 == 0:
            time.sleep(0)

        # 注释
        if m.group(1):
            raw_comment = m.group(1)
            text = raw_comment.strip()
            if text and len(text) > 3:
                # 计算 strip 后文本在原始内容中的准确起始位置
                stripped_offset = len(raw_comment) - len(raw_comment.lstrip())
                pos = m.start(1) + stripped_offset
                if pos not in used_positions:
                    used_positions.add(pos)
                    results.append({"type": "块注释", "keyword": "", "name": "",
                                    "original": text, "translated": "",
                                    "position": pos, "length": len(text)})
            continue

        if m.group(2):
            raw_line = m.group(2)
            text = raw_line.strip()
            if text and len(text) > 3:
                stripped_offset = len(raw_line) - len(raw_line.lstrip())
                pos = m.start(2) + stripped_offset
                if pos not in used_positions:
                    used_positions.add(pos)
                    results.append({"type": "行注释", "keyword": "", "name": "",
                                    "original": text, "translated": "",
                                    "position": pos, "length": len(text)})
            continue

        # A2L 关键字
        keyword = m.group(3)
        name = m.group(4) or ""
        strings_text = m.group(5) or m.group(6) or ""

        # 跳过注释行
        line_start = content.rfind("\n", 0, m.start()) + 1
        prefix = content[line_start:m.start()]
        if "//" in prefix:
            continue

        # 提取引号内字符串
        if m.group(5):
            # 单个字符串：group(5) 捕获的是引号内的纯文本（不含引号）
            s = strings_text.strip()
            if s and len(s) > 1:
                # m.start(5) 指向文本首字符，-1 定位到前导引号 "
                quote_pos = m.start(5) - 1
                if quote_pos not in used_positions:
                    used_positions.add(quote_pos)
                    results.append({
                        "type": "A2L描述", "keyword": keyword,
                        "name": "" if s == name else name,
                        "original": s, "translated": "",
                        "position": quote_pos + 1,     # 跳过引号指向文本首字符
                        "length": len(s),               # 不含引号的纯文本长度
                    })
        elif m.group(6):
            # 多个字符串：group(6) 捕获的是带引号的文本
            for s in re.findall(r'"([^"]*)"', strings_text):
                s_stripped = s.strip()
                if s_stripped and len(s_stripped) > 1:
                    group_start = m.start(6)
                    pos = content.find(f'"{s_stripped}"', group_start, m.end())
                    if pos == -1:
                        pos = content.find(f'"{s}"', group_start, m.end())
                    if pos != -1 and pos not in used_positions:
                        used_positions.add(pos)
                        results.append({
                            "type": "A2L描述", "keyword": keyword,
                            "name": "" if s_stripped == name else name,
                            "original": s_stripped, "translated": "",
                            "position": pos + 1,
                            "length": len(s_stripped),
                        })
                    break

    results.sort(key=lambda x: x["position"])
    return results, content


def apply_translations(content, entries):
    """将翻译替换回 A2L 内容（分段拼接 + GIL 释放，避免大文件卡死）"""
    translated = [e for e in entries if e.get("translated") and
                  e["translated"] != e["original"]]
    if not translated:
        return content

    translated.sort(key=lambda x: x["position"])
    parts = []
    last_end = 0
    for i, entry in enumerate(translated):
        if i % 500 == 0:
            time.sleep(0)  # 释放 GIL
        pos, orig_len = entry["position"], entry["length"]
        parts.append(content[last_end:pos])
        parts.append(entry["translated"])
        last_end = pos + orig_len
    parts.append(content[last_end:])
    return "".join(parts)


def _fuzzy_score(text, pattern):
    """编辑距离相似度 (0-1)，用于模糊匹配"""
    t, p = text.lower(), pattern.lower()
    if p in t:
        return 1.0
    # 简易 Levenshtein (限制差异 ≤ 2 字符)
    if abs(len(t) - len(p)) > 2:
        return 0.0
    if len(t) < len(p):
        t, p = p, t
    # 滑动窗口匹配
    best = 0
    for i in range(len(t) - len(p) + 1):
        matches = sum(1 for a, b in zip(t[i:i+len(p)], p) if a == b)
        score = matches / len(p)
        if score > best:
            best = score
    return best


def translate_with_glossary(text, glossary):
    """四级智能匹配：精确→模糊→子串→组合短语"""
    if not text or len(text.strip()) < 2:
        return None

    text_lower = text.lower().strip()

    # ── 1. 精确匹配（含大小写不敏感）──
    for en, zh in glossary.items():
        if en.lower() == text_lower:
            return zh

    # ── 2. 模糊匹配（处理拼写变体 / OCR误差）──
    fuzzy_candidates = []
    for en, zh in glossary.items():
        en_lower = en.lower()
        if len(en_lower) < 4 or len(text_lower) < 4:
            continue
        # 长度差异不超过 20%
        if abs(len(en_lower) - len(text_lower)) > max(len(en_lower), len(text_lower)) * 0.25:
            continue
        score = _fuzzy_score(text_lower, en_lower)
        if score >= 0.85:
            fuzzy_candidates.append((score, len(en_lower), zh))
    if fuzzy_candidates:
        fuzzy_candidates.sort(key=lambda x: (-x[0], -x[1]))
        return fuzzy_candidates[0][2]

    # ── 3. 子串匹配 - 找所有匹配的 glossary key ──
    matches = []
    for en, zh in glossary.items():
        en_lower = en.lower()
        if len(en_lower) < 3:
            continue
        idx = text_lower.find(en_lower)
        if idx >= 0:
            # 单词边界检查
            before_ok = idx == 0 or not text_lower[idx - 1].isalpha()
            after_ok = (idx + len(en_lower) == len(text_lower)
                        or not text_lower[idx + len(en_lower)].isalpha())
            if before_ok and after_ok:
                matches.append((en_lower, zh, idx, len(en_lower)))

    if not matches:
        # ── 3b. 回退：不做单词边界检查的子串匹配 ──
        for en, zh in glossary.items():
            en_lower = en.lower()
            if len(en_lower) < 4:
                continue
            idx = text_lower.find(en_lower)
            if idx >= 0:
                matches.append((en_lower, zh, idx, len(en_lower)))
        if not matches:
            return None

    # ── 4. 去重去重叠，最长优先 ──
    matches.sort(key=lambda x: -x[3])
    used_ranges = []
    selected = []
    for m in matches:
        m_start, m_end = m[2], m[2] + m[3]
        overlap = any(not (m_end <= u[0] or m_start >= u[1]) for u in used_ranges)
        if not overlap:
            used_ranges.append((m_start, m_end))
            selected.append(m)

    # ── 5. 按原文位置排序，智能拼接译文 ──
    selected.sort(key=lambda x: x[2])
    result_parts = []
    last_end = 0
    for _, zh, start, length in selected:
        gap = text[last_end:start]
        if gap.strip():
            result_parts.append(gap)
        result_parts.append(zh)
        last_end = start + length
    if last_end < len(text):
        tail = text[last_end:]
        if tail.strip():
            result_parts.append(tail)

    return "".join(result_parts) if result_parts else None


def api_translate_one(text, src_lang, tgt_lang, ssl_ctx, timeout=15):
    params = {"q": text, "langpair": f"{src_lang}|{tgt_lang}"}
    url = "https://api.mymemory.translated.net/get?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; A2L-Translator/2.9.5)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("responseStatus") == 200:
                return data["responseData"]["translatedText"].strip()
    except Exception:
        pass
    return None


def api_translate_batch(texts, src_lang, tgt_lang, ssl_ctx, timeout=15):
    separator = " ||| "
    combined = separator.join(texts)
    params = {"q": combined, "langpair": f"{src_lang}|{tgt_lang}"}
    url = "https://api.mymemory.translated.net/get?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; A2L-Translator/2.9.5)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("responseStatus") == 200:
                result = data["responseData"]["translatedText"]
                parts = result.split("|||") if "|||" in result else (
                    result.split(" ||| ") if " ||| " in result else [result])
                return [p.strip() for p in parts]
    except Exception:
        pass
    return None


# ══════════════════════════════════════════════════════════
#  现代主题样式系统
# ══════════════════════════════════════════════════════════

COLORS = {
    "bg":            "#F0F2F5",
    "card":          "#FFFFFF",
    "primary":       "#3B82F6",
    "primary_hover": "#2563EB",
    "primary_light": "#EFF6FF",
    "success":       "#10B981",
    "warning":       "#F59E0B",
    "danger":        "#EF4444",
    "text":          "#1E293B",
    "text_secondary":"#64748B",
    "text_muted":    "#94A3B8",
    "border":        "#E2E8F0",
    "drop_bg":       "#F8FAFC",
    "drop_border":   "#CBD5E1",
    "drop_active":   "#DBEAFE",
    "header_start":  "#1E40AF",
    "header_end":    "#3B82F6",
    "row_alt":       "#F8FAFC",
    "row_hover":     "#EFF6FF",
}

def apply_theme(root):
    style = ttk.Style(root)
    style.theme_use("clam")

    # ── 通用字体 ──
    default_font = ("Microsoft YaHei UI", 9)
    bold_font    = ("Microsoft YaHei UI", 9, "bold")
    small_font   = ("Microsoft YaHei UI", 8)

    style.configure(".", font=default_font, background=COLORS["bg"])

    # ── 框架 ──
    style.configure("Card.TFrame", background=COLORS["card"], relief="flat",
                    borderwidth=1)
    style.configure("Header.TFrame", background=COLORS["header_end"])
    style.configure("Toolbar.TFrame", background=COLORS["bg"])

    # ── 标签 ──
    style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"),
                    background=COLORS["header_end"], foreground="#FFFFFF")
    style.configure("Subtitle.TLabel", font=("Microsoft YaHei UI", 9),
                    background=COLORS["header_end"], foreground="#BFDBFE")
    style.configure("CardTitle.TLabel", font=("Microsoft YaHei UI", 11, "bold"),
                    foreground=COLORS["text"], background=COLORS["card"])
    style.configure("Setting.TLabel", font=default_font,
                    foreground=COLORS["text_secondary"], background=COLORS["card"])

    # ── 按钮 ──
    style.configure("Primary.TButton",
                    font=("Microsoft YaHei UI", 9, "bold"),
                    background=COLORS["primary"], foreground="#FFFFFF",
                    borderwidth=0, relief="flat", padding=(14, 6))
    style.map("Primary.TButton",
              background=[("active", COLORS["primary_hover"]),
                          ("disabled", "#93C5FD")])

    style.configure("Secondary.TButton",
                    font=default_font,
                    background=COLORS["card"], foreground=COLORS["text"],
                    borderwidth=1, relief="solid", padding=(10, 5))
    style.map("Secondary.TButton",
              background=[("active", COLORS["primary_light"]),
                          ("disabled", COLORS["card"])],
              bordercolor=[("active", COLORS["primary"])])

    style.configure("Outline.TButton",
                    font=default_font,
                    background=COLORS["card"], foreground=COLORS["primary"],
                    borderwidth=1, relief="solid", padding=(10, 5))
    style.map("Outline.TButton",
              background=[("active", COLORS["primary_light"])])

    style.configure("Success.TButton",
                    font=("Microsoft YaHei UI", 9, "bold"),
                    background=COLORS["success"], foreground="#FFFFFF",
                    borderwidth=0, relief="flat", padding=(14, 6))
    style.map("Success.TButton",
              background=[("active", "#059669"), ("disabled", "#6EE7B7")])

    style.configure("Danger.TButton",
                    font=("Microsoft YaHei UI", 9),
                    background="#FFFFFF", foreground=COLORS["danger"],
                    borderwidth=1, relief="solid", padding=(10, 5))
    style.map("Danger.TButton",
              background=[("active", "#FEF2F2")])

    # ── Treeview (表格) ──
    style.configure("Modern.Treeview",
                    font=default_font,
                    background=COLORS["card"], foreground=COLORS["text"],
                    fieldbackground=COLORS["card"], borderwidth=0,
                    rowheight=30)
    style.configure("Modern.Treeview.Heading",
                    font=("Microsoft YaHei UI", 9, "bold"),
                    background="#F1F5F9", foreground=COLORS["text"],
                    borderwidth=0, relief="flat", padding=(8, 6))
    style.map("Modern.Treeview",
              background=[("selected", COLORS["primary_light"])],
              foreground=[("selected", COLORS["text"])])

    # ── 进度条 ──
    style.configure("Modern.Horizontal.TProgressbar",
                    troughcolor=COLORS["border"],
                    background=COLORS["primary"],
                    thickness=6, borderwidth=0)

    # ── Combobox ──
    style.configure("TCombobox",
                    fieldbackground=COLORS["card"],
                    background=COLORS["card"],
                    arrowcolor=COLORS["text"],
                    borderwidth=1, relief="solid")

    # ── Checkbutton ──
    style.configure("Modern.TCheckbutton",
                    background=COLORS["card"],
                    foreground=COLORS["text_secondary"],
                    font=default_font)
    style.map("Modern.TCheckbutton",
              background=[("active", COLORS["card"])])

    # ── Spinbox ──
    style.configure("TSpinbox",
                    fieldbackground=COLORS["card"],
                    background=COLORS["card"],
                    borderwidth=1, relief="solid",
                    arrowsize=12)

    # ── Scrollbar ──
    style.configure("TScrollbar",
                    background=COLORS["card"],
                    troughcolor=COLORS["bg"],
                    borderwidth=0, arrowsize=12)

    return default_font, bold_font, small_font


# ══════════════════════════════════════════════════════════
#  拖放区域组件 (canvas 绘制)
# ══════════════════════════════════════════════════════════

class DropZone(tk.Canvas):
    """自定义拖放区，带图标和动画"""

    def __init__(self, parent, on_click, on_drop=None, **kw):
        super().__init__(parent, height=120, bg=COLORS["drop_bg"],
                         highlightthickness=0, cursor="hand2", **kw)
        self.on_click = on_click
        self.on_drop = on_drop
        self._hover = False
        self._anim_id = None
        self._draw()

        self.bind("<Button-1>", lambda e: self.on_click())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

        try:
            from tkinterdnd2 import DND_FILES
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_dnd_drop)
        except ImportError:
            pass

    def _on_dnd_drop(self, event):
        if self.on_drop:
            self.on_drop(event)

    def _on_enter(self, _e):
        self._hover = True
        self._draw()

    def _on_leave(self, _e):
        self._hover = False
        self._draw()

    def _draw(self):
        self.delete("all")
        w = self.winfo_width() or 700
        h = 120

        # 背景
        bg = COLORS["drop_active"] if self._hover else COLORS["drop_bg"]
        border = COLORS["primary"] if self._hover else COLORS["drop_border"]
        self.configure(bg=bg)

        # 虚线边框
        dash = (12, 6) if self._hover else (8, 6)
        self.create_rectangle(3, 3, w - 3, h - 3, outline=border, width=2,
                              dash=dash, tags="border")

        # 图标 - 文件夹/文档
        cx, cy = w // 2, 42
        color = COLORS["primary"] if self._hover else COLORS["text_muted"]

        # 文件图标 (简化SVG风格)
        # 文档主体
        doc_x, doc_y = cx - 16, cy - 14
        self.create_rectangle(doc_x, doc_y, doc_x + 28, doc_y + 36,
                              fill=color, outline="", tags="icon")
        # 折角
        self.create_polygon(doc_x + 14, doc_y, doc_x + 28, doc_y,
                            doc_x + 28, doc_y + 14,
                            fill="#FFFFFF" if self._hover else COLORS["drop_bg"],
                            outline="", tags="icon")
        # 折线
        self.create_line(doc_x + 14, doc_y, doc_x + 14, doc_y + 14,
                         doc_x + 28, doc_y + 14,
                         fill=bg, width=1.5, tags="icon")
        # 横线(文字模拟)
        for i, line_w in enumerate([16, 18, 12]):
            lx = doc_x + 5
            ly = doc_y + 19 + i * 7
            self.create_line(lx, ly, lx + line_w, ly,
                             fill=bg, width=2, tags="icon")

        # 箭头
        arrow_x = cx + 24
        arrow_y = cy - 4
        self.create_line(arrow_x, arrow_y, arrow_x + 16, arrow_y,
                         fill=color, width=2, tags="icon")
        self.create_polygon(arrow_x + 10, arrow_y - 5, arrow_x + 16, arrow_y,
                            arrow_x + 10, arrow_y + 5,
                            fill=color, outline="", tags="icon")

        # 文字
        text_color = COLORS["primary"] if self._hover else COLORS["text_secondary"]
        self.create_text(cx, cy + 38,
                         text="拖放 A2L 文件到此处，或点击选择",
                         fill=text_color,
                         font=("Microsoft YaHei UI", 11),
                         tags="text")
        self.create_text(cx, cy + 56,
                         text="支持 .a2l / .kp 文件  |  内置 1700+ 中英德汽车专业术语词库",
                         fill=COLORS["text_muted"],
                         font=("Microsoft YaHei UI", 8),
                         tags="sub")


# ══════════════════════════════════════════════════════════
#  编辑对话框
# ══════════════════════════════════════════════════════════

class EditDialog(tk.Toplevel):
    def __init__(self, parent, original, translated, on_save):
        super().__init__(parent)
        self.title("编辑译文")
        self.geometry("520x160")
        self.resizable(False, False)
        self.configure(bg=COLORS["card"])
        self.transient(parent)
        self.grab_set()

        # 居中
        self.update_idletasks()
        px = parent.winfo_rootx() + (parent.winfo_width() - 520) // 2
        py = parent.winfo_rooty() + (parent.winfo_height() - 160) // 2
        self.geometry(f"+{px}+{py}")

        # 去除系统标题栏边框感
        self.configure(padx=0, pady=0)

        inner = tk.Frame(self, bg=COLORS["card"], padx=20, pady=15)
        inner.pack(fill=tk.BOTH, expand=True)

        # 原文
        tk.Label(inner, text="原文", font=("Microsoft YaHei UI", 8),
                 fg=COLORS["text_muted"], bg=COLORS["card"]).pack(anchor=tk.W)
        orig_label = tk.Label(inner, text=original, font=("Microsoft YaHei UI", 10, "bold"),
                              fg=COLORS["text"], bg=COLORS["card"],
                              wraplength=470, justify=tk.LEFT)
        orig_label.pack(anchor=tk.W, pady=(0, 12))

        # 译文输入
        tk.Label(inner, text="译文", font=("Microsoft YaHei UI", 8),
                 fg=COLORS["primary"], bg=COLORS["card"]).pack(anchor=tk.W)

        entry_frame = tk.Frame(inner, bg=COLORS["card"])
        entry_frame.pack(fill=tk.X)

        self.entry = tk.Entry(entry_frame, font=("Microsoft YaHei UI", 11),
                              bg="#F8FAFC", fg=COLORS["text"],
                              insertbackground=COLORS["primary"],
                              relief=tk.SOLID, bd=1)
        self.entry.insert(0, translated)
        self.entry.pack(fill=tk.X, ipady=4)
        self.entry.focus_set()
        self.entry.select_range(0, tk.END)

        # 按钮
        btn_frame = tk.Frame(inner, bg=COLORS["card"])
        btn_frame.pack(fill=tk.X, pady=(12, 0))

        cancel_btn = tk.Button(btn_frame, text="取消", font=("Microsoft YaHei UI", 9),
                               bg="#F1F5F9", fg=COLORS["text_secondary"],
                               activebackground="#E2E8F0",
                               relief=tk.FLAT, bd=0, padx=16, pady=4,
                               cursor="hand2",
                               command=self.destroy)
        cancel_btn.pack(side=tk.RIGHT, padx=(8, 0))

        def do_save():
            on_save(self.entry.get())
            self.destroy()

        save_btn = tk.Button(btn_frame, text="确认保存", font=("Microsoft YaHei UI", 9, "bold"),
                             bg=COLORS["primary"], fg="#FFFFFF",
                             activebackground=COLORS["primary_hover"],
                             relief=tk.FLAT, bd=0, padx=20, pady=4,
                             cursor="hand2",
                             command=do_save)
        save_btn.pack(side=tk.RIGHT)

        self.entry.bind("<Return>", lambda e: do_save())
        self.entry.bind("<Escape>", lambda e: self.destroy())


# ══════════════════════════════════════════════════════════
#  主应用
# ══════════════════════════════════════════════════════════

class A2LTranslatorGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("A2L/KP 文件翻译工具")
        self.root.geometry("860x700")
        self.root.minsize(700, 550)
        self.root.configure(bg=COLORS["bg"])

        # 样式
        self.DF, self.BF, self.SF = apply_theme(root)

        # 状态
        self.filepath = tk.StringVar()
        self.src_lang = tk.StringVar(value="en")
        self.tgt_lang = tk.StringVar(value="zh-CN")
        self.batch_size = tk.IntVar(value=8)
        self.delay = tk.DoubleVar(value=0.6)
        self.use_builtin = tk.BooleanVar(value=True)
        self.skip_ssl = tk.BooleanVar(value=False)
        self.use_batch = tk.BooleanVar(value=True)
        self.baidu_appid = tk.StringVar()   # 百度翻译 APP ID
        self.baidu_secret = tk.StringVar()  # 百度翻译密钥

        # 模块来源标识（外置可升级 / 内置出厂）
        self._glossary_src = _glossary_src
        self._dict_src = _dict_src

        self.entries = []
        self.original_content = ""
        self.is_processing = False
        self.filter_var = tk.StringVar()         # 搜索过滤
        self._tree_loaded = 0                    # 已加载到 TreeView 的条目数
        self._tree_loading_job = None            # 懒加载 after job ID
        self._tree_full_loaded = False           # Tree 是否已全部加载
        self.glossary = dict(BUILTIN_GLOSSARY)
        self.glossary.update(GERMAN_GLOSSARY)  # 合并德语词典
        self.glossary_index = build_glossary_index(self.glossary)  # 预建索引
        # 智能翻译器: (精确匹配dict, 组合正则, term映射)
        self.smart_exact, self.smart_pattern, self.smart_terms = _build_smart_translator(
            self.glossary, SMART_KEYWORDS)
        self.ssl_ctx = None
        self._update_ssl_ctx()

        # ── 多源词典引擎（8大词典，延迟初始化）──
        self.multi_dict = None
        self._dict_ready = False

        self._build_ui()
        # 后台异步初始化多源词典
        self.root.after(200, self._init_multi_dict)

    def _init_multi_dict(self):
        """后台异步初始化多源词典引擎"""
        try:
            self.multi_dict = get_dictionary()
            self._dict_ready = True
        except Exception:
            self._dict_ready = False

    def _update_ssl_ctx(self):
        if self.skip_ssl.get():
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self.ssl_ctx = ctx
        else:
            self.ssl_ctx = None

        # ── 启动时自动检查更新 ──
        self.root.after(2000, lambda: self._auto_check_update())

    # ═══ UI 构建 ═══

    def _build_ui(self):
        self._build_header()
        self._build_drop_zone()
        self._build_settings()
        self._build_toolbar()
        self._build_table()
        self._build_statusbar()

    # ═══ 自动更新 ═══

    def _auto_check_update(self):
        """启动时静默检查更新"""
        try:
            from updater import check_and_notify
            check_and_notify(self.root, silent=True)
        except Exception:
            pass

    def _build_header(self):
        header = tk.Frame(self.root, bg=COLORS["header_end"], height=72)
        header.pack(fill=tk.X)
        header.pack_propagate(False)

        inner = tk.Frame(header, bg=COLORS["header_end"])
        inner.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)

        # Logo 图标 (用 Canvas 绘制)
        logo = tk.Canvas(inner, width=40, height=40, bg=COLORS["header_end"],
                         highlightthickness=0)
        logo.pack(side=tk.LEFT, padx=(0, 12))
        # 翻译图标 - A→中
        logo.create_oval(2, 2, 38, 38, outline="#60A5FA", width=2)
        logo.create_text(20, 14, text="A", fill="#93C5FD",
                         font=("Consolas", 14, "bold"))
        logo.create_text(20, 29, text="译", fill="#FFFFFF",
                         font=("Microsoft YaHei UI", 11, "bold"))

        # 标题
        text_frame = tk.Frame(inner, bg=COLORS["header_end"])
        text_frame.pack(side=tk.LEFT)

        tk.Label(text_frame, text="A2L 文件翻译工具",
                 font=("Microsoft YaHei UI", 18, "bold"),
                 fg="#FFFFFF", bg=COLORS["header_end"]).pack(anchor=tk.W)
        tk.Label(text_frame, text="ASAM MCD-2 MC / ASAP2 标定文件翻译",
                 font=("Microsoft YaHei UI", 9),
                 fg="#93C5FD", bg=COLORS["header_end"]).pack(anchor=tk.W)

        # 版本号
        _version_text = "v2.9.5 · 单文件版"
        self._version_label = tk.Label(inner, text=_version_text, font=("Consolas", 8),
                 fg="#60A5FA", bg=COLORS["header_end"])
        self._version_label.pack(side=tk.RIGHT, anchor=tk.S)
        # Tooltip 提示
        self._version_label.bind("<Enter>", lambda e: self._show_tooltip(e,
            "▲ 可升级：外置 data/ 目录中的模块\n▼ 出厂默认：exe 内置的模块\n\n外置模块可独立替换升级，无需重装 exe"))
        self._version_label.bind("<Leave>", lambda e: self._hide_tooltip())

    def _build_drop_zone(self):
        drop_frame = tk.Frame(self.root, bg=COLORS["bg"])
        drop_frame.pack(fill=tk.X, padx=15, pady=(12, 8))

        self.dropzone = DropZone(drop_frame, on_click=self._select_file,
                                 on_drop=self._on_drop)
        self.dropzone.pack(fill=tk.X)

        # 文件路径显示
        path_frame = tk.Frame(self.root, bg=COLORS["bg"])
        path_frame.pack(fill=tk.X, padx=15, pady=(0, 8))

        self.path_label = tk.Label(path_frame, text="尚未选择文件",
                                   font=("Microsoft YaHei UI", 9),
                                   fg=COLORS["text_muted"], bg=COLORS["bg"],
                                   anchor=tk.W)
        self.path_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.file_badge = tk.Frame(path_frame, bg=COLORS["primary_light"])
        # 隐藏直到加载文件

    def _build_settings(self):
        card = tk.Frame(self.root, bg=COLORS["card"], bd=0,
                        highlightbackground=COLORS["border"],
                        highlightthickness=1)
        card.pack(fill=tk.X, padx=15, pady=(0, 8))

        inner = tk.Frame(card, bg=COLORS["card"])
        inner.pack(fill=tk.X, padx=15, pady=10)

        # 行1: 语言设置
        row1 = tk.Frame(inner, bg=COLORS["card"])
        row1.pack(fill=tk.X)

        # 源语言
        self._make_setting(row1, "源语言", "auto / en / de")
        src = ttk.Combobox(row1, textvariable=self.src_lang, width=8,
                           values=["auto", "en", "de"], state="readonly",
                           font=self.DF)
        src.pack(side=tk.LEFT, padx=(4, 18))

        # 目标语言
        self._make_setting(row1, "目标语言")
        tgt = ttk.Combobox(row1, textvariable=self.tgt_lang, width=8,
                           values=["zh-CN", "en", "ja", "ko", "de", "fr", "es"],
                           state="readonly", font=self.DF)
        tgt.pack(side=tk.LEFT, padx=(4, 18))

        # 每次翻译条数
        self._make_setting(row1, "批量条数")
        ttk.Spinbox(row1, textvariable=self.batch_size, from_=1, to=20,
                    width=5, font=self.DF).pack(side=tk.LEFT, padx=(4, 18))

        # API间隔
        self._make_setting(row1, "间隔(秒)")
        ttk.Spinbox(row1, textvariable=self.delay, from_=0.1, to=5,
                    increment=0.1, width=5, format="%.1f",
                    font=self.DF).pack(side=tk.LEFT)

        # ── 登录验证行 ──
        row_login = tk.Frame(inner, bg=COLORS["card"])
        row_login.pack(fill=tk.X, pady=(6, 2))
        self.btn_login = ttk.Button(row_login, text="登录验证",
                                    command=self._verify_baidu_credentials,
                                    style="Accent.TButton", width=10)
        self.btn_login.pack(side=tk.LEFT)
        self.baidu_status = tk.Label(row_login, text="",
                                     font=("Microsoft YaHei UI", 8),
                                     fg=COLORS["text_muted"], bg=COLORS["card"])
        self.baidu_status.pack(side=tk.LEFT, padx=(8, 0))

        # 百度翻译 API 密钥行
        row_baidu1 = tk.Frame(inner, bg=COLORS["card"])
        row_baidu1.pack(fill=tk.X, pady=(4, 0))
        self._make_setting(row_baidu1, "APP ID")
        appid_entry = ttk.Entry(row_baidu1, textvariable=self.baidu_appid,
                                width=28, font=self.DF)
        appid_entry.pack(side=tk.LEFT, padx=(4, 8))
        self._bind_copy_paste(appid_entry)

        row_baidu2 = tk.Frame(inner, bg=COLORS["card"])
        row_baidu2.pack(fill=tk.X, pady=(4, 0))
        self._make_setting(row_baidu2, "密钥")
        secret_entry = ttk.Entry(row_baidu2, textvariable=self.baidu_secret,
                                 width=28, font=self.DF, show="*")
        secret_entry.pack(side=tk.LEFT, padx=(4, 8))
        self._bind_copy_paste(secret_entry)
        baidu_help = tk.Label(row_baidu2,
                              text="留空则使用 MyMemory",
                              font=("Microsoft YaHei UI", 7),
                              fg=COLORS["text_muted"], bg=COLORS["card"])
        baidu_help.pack(side=tk.LEFT)

        # 右侧选项
        right_frame = tk.Frame(inner, bg=COLORS["card"])
        right_frame.pack(fill=tk.X, pady=(8, 0))

        chk_frame = tk.Frame(right_frame, bg=COLORS["card"])
        chk_frame.pack(side=tk.LEFT)

        ttk.Checkbutton(chk_frame, text="内置术语词典",
                        variable=self.use_builtin,
                        style="Modern.TCheckbutton").pack(side=tk.LEFT, padx=(0, 12))
        ttk.Checkbutton(chk_frame, text="批量翻译(快)",
                        variable=self.use_batch,
                        style="Modern.TCheckbutton").pack(side=tk.LEFT, padx=(0, 12))
        ttk.Checkbutton(chk_frame, text="跳过SSL验证",
                        variable=self.skip_ssl, command=self._update_ssl_ctx,
                        style="Modern.TCheckbutton").pack(side=tk.LEFT)

    def _make_setting(self, parent, text, tooltip=""):
        lbl = tk.Label(parent, text=text, font=("Microsoft YaHei UI", 8),
                       fg=COLORS["text_muted"], bg=COLORS["card"])
        lbl.pack(side=tk.LEFT)

    def _bind_copy_paste(self, widget):
        """为 Entry/Text 控件绑定右键菜单 (剪切/复制/粘贴)"""
        menu = tk.Menu(widget, tearoff=0, font=("Microsoft YaHei UI", 9))

        def cut():
            try:
                widget.event_generate("<<Cut>>")
            except tk.TclError:
                pass

        def copy():
            try:
                widget.event_generate("<<Copy>>")
            except tk.TclError:
                pass

        def paste():
            try:
                widget.event_generate("<<Paste>>")
            except tk.TclError:
                pass

        menu.add_command(label="剪切   Ctrl+X", command=cut)
        menu.add_command(label="复制   Ctrl+C", command=copy)
        menu.add_command(label="粘贴   Ctrl+V", command=paste)

        def show_menu(event):
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        widget.bind("<Button-3>", show_menu, add="+")
        # Windows 标准快捷键 Ctrl+C/V/X 默认已由 Tk 处理，无需额外绑定

    def _verify_baidu_credentials(self):
        """验证百度翻译 API 凭据是否有效"""
        appid = (self.baidu_appid.get() or "").strip()
        secret = (self.baidu_secret.get() or "").strip()
        if not appid or not secret:
            self.baidu_status.config(text="⚠ 请填写 APP ID 和密钥", fg="#D97706")
            self._set_status("请填写百度翻译凭据后再验证", "warning")
            return

        self.baidu_status.config(text="⏳ 验证中...", fg=COLORS["text_muted"])
        self.btn_login.config(state="disabled")
        self._set_status("正在验证百度翻译 API...", "working")

        def worker():
            try:
                result = baidu_translate_one("verify", "en", "zh", appid, secret, self.ssl_ctx, timeout=10)
                if result is not None:
                    self.root.after(0, lambda: self.baidu_status.config(
                        text="✓ 验证通过", fg="#16A34A"))
                    self.root.after(0, lambda: self._set_status("百度翻译 API 验证通过 ✓", "success"))
                else:
                    self.root.after(0, lambda: self.baidu_status.config(
                        text="✗ 验证失败，请检查凭据", fg="#DC2626"))
                    self.root.after(0, lambda: self._set_status("百度翻译 API 验证失败", "error"))
            except Exception:
                self.root.after(0, lambda: self.baidu_status.config(
                    text="✗ 网络错误", fg="#DC2626"))
                self.root.after(0, lambda: self._set_status("百度翻译 API 网络错误", "error"))
            finally:
                self.root.after(0, lambda: self.btn_login.config(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def _build_toolbar(self):
        """操作按钮栏，卡片式"""
        card = tk.Frame(self.root, bg=COLORS["card"], bd=0,
                        highlightbackground=COLORS["border"],
                        highlightthickness=1)
        card.pack(fill=tk.X, padx=15, pady=(0, 8))

        inner = tk.Frame(card, bg=COLORS["card"])
        inner.pack(fill=tk.X, padx=12, pady=8)

        # 左侧：主操作
        left = tk.Frame(inner, bg=COLORS["card"])
        left.pack(side=tk.LEFT)

        steps = [
            ("1. 加载", self._load_file, "Primary.TButton"),
            ("2. 词典匹配", self._glossary_translate, "Secondary.TButton"),
            ("3. 自动翻译", self._auto_translate, "Primary.TButton"),
            ("4. 导出 A2L", self._export, "Success.TButton"),
        ]
        for i, (text, cmd, style) in enumerate(steps):
            btn = ttk.Button(left, text=text, command=cmd, style=style)
            btn.pack(side=tk.LEFT, padx=(0, 6))
            setattr(self, f"btn_{i}", btn)

        # 分隔
        sep1 = tk.Frame(inner, bg=COLORS["border"], width=1)
        sep1.pack(side=tk.LEFT, fill=tk.Y, padx=8, pady=2)

        # 多源验证按钮（8大词典）
        self.btn_verify = ttk.Button(left, text="🌐 多源验证",
                                     command=self._multi_source_verify,
                                     style="Accent.TButton")
        self.btn_verify.pack(side=tk.LEFT, padx=(4, 6))

        # 升级帮助按钮
        self.btn_upgrade = ttk.Button(left, text="📦 升级",
                                      command=self._show_upgrade_info,
                                      style="Outline.TButton")
        self.btn_upgrade.pack(side=tk.LEFT, padx=(0, 0))

        # 分隔
        sep = tk.Frame(inner, bg=COLORS["border"], width=1)
        sep.pack(side=tk.LEFT, fill=tk.Y, padx=10, pady=2)

        # 右侧：辅助操作
        right = tk.Frame(inner, bg=COLORS["card"])
        right.pack(side=tk.LEFT)

        aux_actions = [
            ("导出 CSV", self._export_csv),
            ("导入 CSV", self._import_csv),
            ("保存进度", self._save_progress),
            ("恢复进度", self._load_progress),
        ]
        for text, cmd in aux_actions:
            btn = ttk.Button(right, text=text, command=cmd, style="Outline.TButton")
            btn.pack(side=tk.LEFT, padx=(0, 6))

    def _build_table(self):
        card = tk.Frame(self.root, bg=COLORS["card"], bd=0,
                        highlightbackground=COLORS["border"],
                        highlightthickness=1)
        card.pack(fill=tk.BOTH, expand=True, padx=15, pady=(0, 8))

        # ── 搜索栏 ──
        search_frame = tk.Frame(card, bg="#F8FAFC", height=34)
        search_frame.pack(fill=tk.X)
        search_frame.pack_propagate(False)

        tk.Label(search_frame, text="  🔍",
                 font=("Microsoft YaHei UI", 10),
                 fg=COLORS["text_muted"], bg="#F8FAFC").pack(side=tk.LEFT, pady=4)

        self.filter_entry = tk.Entry(search_frame, textvariable=self.filter_var,
                                     font=("Microsoft YaHei UI", 9),
                                     bg="#FFFFFF", fg=COLORS["text"],
                                     insertbackground=COLORS["primary"],
                                     relief=tk.FLAT, bd=0)
        self.filter_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 8), ipady=2)

        # 清除按钮
        self.filter_clear_btn = tk.Label(search_frame, text="✕",
                                         font=("Microsoft YaHei UI", 10),
                                         fg=COLORS["text_muted"], bg="#F8FAFC",
                                         cursor="hand2")
        self.filter_clear_btn.pack(side=tk.RIGHT, padx=(0, 8), pady=4)
        self.filter_clear_btn.bind("<Button-1>", lambda e: self.filter_var.set(""))
        self.filter_clear_btn.bind("<Enter>", lambda e: self.filter_clear_btn.configure(fg=COLORS["danger"]))
        self.filter_clear_btn.bind("<Leave>", lambda e: self.filter_clear_btn.configure(fg=COLORS["text_muted"]))

        placeholder_text = "输入关键词搜索... (支持原文/译文/名称，实时过滤)"
        self.filter_entry.insert(0, placeholder_text)
        self.filter_entry.configure(fg=COLORS["text_muted"])
        def _on_focus_in(e):
            if self.filter_var.get() == placeholder_text:
                self.filter_var.set("")
                self.filter_entry.configure(fg=COLORS["text"])
        def _on_focus_out(e):
            if not self.filter_var.get():
                self.filter_var.set(placeholder_text)
                self.filter_entry.configure(fg=COLORS["text_muted"])
        self.filter_entry.bind("<FocusIn>", _on_focus_in)
        self.filter_entry.bind("<FocusOut>", _on_focus_out)

        # 过滤延迟 （250ms 防抖，避免每次按键都重建整个 Tree）
        self._filter_after_id = None
        self.filter_var.trace_add("write", lambda *a: self._schedule_filter())

        # 表头
        header = tk.Frame(card, bg="#F8FAFC", height=30)
        header.pack(fill=tk.X)
        header.pack_propagate(False)

        tk.Label(header, text="  翻译条目",
                 font=("Microsoft YaHei UI", 10, "bold"),
                 fg=COLORS["text"], bg="#F8FAFC").pack(side=tk.LEFT, pady=4)

        self.table_stats = tk.Label(header, text="",
                                    font=("Microsoft YaHei UI", 8),
                                    fg=COLORS["text_muted"], bg="#F8FAFC")
        self.table_stats.pack(side=tk.RIGHT, padx=10, pady=4)

        # 表格容器
        table_area = tk.Frame(card, bg=COLORS["card"])
        table_area.pack(fill=tk.BOTH, expand=True, padx=1, pady=(0, 1))

        # Scrollbar
        self.tree_scroll_y = ttk.Scrollbar(table_area, orient=tk.VERTICAL)
        self.tree_scroll_x = ttk.Scrollbar(table_area, orient=tk.HORIZONTAL)

        self.tree = ttk.Treeview(
            table_area,
            columns=("type", "name", "original", "translated"),
            show="headings",
            yscrollcommand=self.tree_scroll_y.set,
            xscrollcommand=self.tree_scroll_x.set,
            selectmode="extended",
            style="Modern.Treeview",
        )
        self.tree_scroll_y.config(command=self.tree.yview)
        self.tree_scroll_x.config(command=self.tree.xview)

        self.tree.heading("type", text="类型")
        self.tree.heading("name", text="名称")
        self.tree.heading("original", text="原文")
        self.tree.heading("translated", text="译文 ✓")

        self.tree.column("type", width=80, minwidth=60, anchor=tk.CENTER)
        self.tree.column("name", width=130, minwidth=80)
        self.tree.column("original", width=300, minwidth=150)
        self.tree.column("translated", width=300, minwidth=150)

        # 标签颜色
        self.tree.tag_configure("translated", foreground=COLORS["success"])
        self.tree.tag_configure("untranslated", foreground=COLORS["text"])
        self.tree.tag_configure("type_tag", foreground=COLORS["text_muted"],
                                font=("Microsoft YaHei UI", 8))

        self.tree_scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_scroll_x.pack(side=tk.BOTTOM, fill=tk.X)
        self.tree.pack(fill=tk.BOTH, expand=True)

        # 事件
        self.tree.bind("<Double-1>", self._on_cell_edit)
        self._build_context_menu()

        # 空状态提示
        self.empty_hint = tk.Label(table_area, text="", bg=COLORS["card"])

    # ═══ 搜索过滤 + 懒加载 TreeView ═══

    def _schedule_filter(self):
        """250ms 防抖：延迟重建过滤视图"""
        if self._filter_after_id:
            self.root.after_cancel(self._filter_after_id)
        self._filter_after_id = self.root.after(250, self._apply_filter)

    def _apply_filter(self):
        """根据 filter_var 重建 TreeView 显示（O(n) 扫描 entries，O(k) 插入）"""
        if not self.entries:
            return

        # 取消进行中的懒加载
        if self._tree_loading_job:
            self.root.after_cancel(self._tree_loading_job)
            self._tree_loading_job = None

        query = self.filter_var.get().strip().lower()
        placeholder = "输入关键词搜索... (支持原文/译文/名称，实时过滤)"

        # 空查询 → 显示所有（懒加载模式）
        if not query or query == placeholder.lower():
            self._start_lazy_tree_load(reset=True)
            return

        # 有过滤词 → 扫描 entries，只显示匹配的
        matched = []
        for i, e in enumerate(self.entries):
            ol = e["original"].lower()
            tl = (e.get("translated") or "").lower()
            nl = e.get("name", "").lower()
            kw = e.get("keyword", "").lower()
            if (query in ol or query in tl or query in nl or query in kw):
                matched.append((i, e))

        self.tree.delete(*self.tree.get_children())
        self._tree_loaded = 0
        self._tree_full_loaded = False

        if not matched:
            self._update_stats_filtered(0, len(self.entries))
            return

        # 分批插入（每批 200 条，用 update_idletasks 而非 update）
        total_m = len(matched)
        BATCH = 200

        def insert_filter_batch(start):
            if start >= total_m:
                self._tree_loaded = total_m
                self._update_stats_filtered(total_m, len(self.entries))
                return

            end = min(start + BATCH, total_m)
            for j in range(start, end):
                i, e = matched[j]
                tag = "translated" if e.get("translated") else "untranslated"
                self.tree.insert("", tk.END,
                    values=(e["type"], e.get("name", ""),
                            e["original"], e.get("translated", "")),
                    tags=(tag,))

            try:
                self.root.update_idletasks()
            except tk.TclError:
                pass

            self.root.after(30, lambda: insert_filter_batch(end))

        insert_filter_batch(0)

    def _update_stats_filtered(self, shown, total):
        """更新统计标签（考虑过滤状态）"""
        query = self.filter_var.get().strip().lower()
        placeholder = "输入关键词搜索... (支持原文/译文/名称，实时过滤)"
        if query and query != placeholder.lower():
            self.table_stats.config(text=f"搜索 \"{self.filter_var.get()}\" — 显示 {shown}/{total} 条")
        else:
            translated = sum(1 for e in self.entries if e.get("translated"))
            if self._tree_loaded < total:
                self.table_stats.config(
                    text=f"共 {total} 条  |  已翻译 {translated}  |  已加载 {self._tree_loaded}/{total}")
            else:
                self.table_stats.config(
                    text=f"共 {total} 条  |  已翻译 {translated}  |  未翻译 {total - translated}")

    def _start_lazy_tree_load(self, reset=False):
        """渐进式加载 TreeView：每 200 条一批，用 after_idle 在空闲时加载"""
        if reset:
            self.tree.delete(*self.tree.get_children())
            self._tree_loaded = 0
            self._tree_full_loaded = False
            if self._tree_loading_job:
                self.root.after_cancel(self._tree_loading_job)
                self._tree_loading_job = None

        if not self.entries or self._tree_full_loaded:
            return

        total = len(self.entries)
        BATCH = 200

        def load_batch():
            if self._tree_loaded >= total:
                self._tree_full_loaded = True
                self._tree_loading_job = None
                self._update_stats_filtered(total, total)
                return

            end = min(self._tree_loaded + BATCH, total)
            for i in range(self._tree_loaded, end):
                e = self.entries[i]
                tag = "translated" if e.get("translated") else "untranslated"
                self.tree.insert("", tk.END,
                    values=(e["type"], e.get("name", ""),
                            e["original"], e.get("translated", "")),
                    tags=(tag,))

            self._tree_loaded = end

            # 轻量刷新：只处理空闲任务，不阻塞用户交互
            try:
                self.root.update_idletasks()
            except tk.TclError:
                pass

            self._update_stats_filtered(self._tree_loaded, total)

            # 使用 after_idle：仅在主线程空闲时加载下一批
            self._tree_loading_job = self.root.after_idle(load_batch)

        self._tree_loading_job = self.root.after_idle(load_batch)

    def _cancel_tree_loading(self):
        """取消所有 TreeView 加载任务"""
        if self._tree_loading_job:
            self.root.after_cancel(self._tree_loading_job)
            self._tree_loading_job = None
        if self._filter_after_id:
            self.root.after_cancel(self._filter_after_id)
            self._filter_after_id = None

    def _build_context_menu(self):
        self.tree_menu = tk.Menu(self.tree, tearoff=0,
                                 font=("Microsoft YaHei UI", 9),
                                 bg=COLORS["card"], fg=COLORS["text"],
                                 activebackground=COLORS["primary_light"],
                                 activeforeground=COLORS["primary"],
                                 relief=tk.FLAT, bd=1)
        self.tree_menu.add_command(label="✎  编辑译文", command=self._edit_selected)
        self.tree_menu.add_command(label="✕  清空译文", command=self._clear_selected)
        self.tree_menu.add_separator()
        self.tree_menu.add_command(label="📋 复制原文", command=self._copy_original)
        self.tree_menu.add_command(label="📖 词典重译选中", command=self._glossary_translate_selected)
        self.tree_menu.add_separator()
        self.tree_menu.add_command(label="🔍 多源词典查询", command=self._multi_source_lookup_selected)
        self.tree_menu.add_command(label="✅ 多源验证选中", command=self._multi_source_verify_selected)
        self.tree.bind("<Button-3>", self._on_right_click)

    def _build_statusbar(self):
        bar = tk.Frame(self.root, bg=COLORS["header_end"], height=36)
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        bar.pack_propagate(False)

        self.status_dot = tk.Canvas(bar, width=14, height=14,
                                    bg=COLORS["header_end"], highlightthickness=0)
        self.status_dot.pack(side=tk.LEFT, padx=(15, 6), pady=11)
        self._draw_status_dot(COLORS["text_muted"])

        self.status_label = tk.Label(bar, text="就绪",
                                     font=("Microsoft YaHei UI", 9),
                                     fg="#BFDBFE", bg=COLORS["header_end"])
        self.status_label.pack(side=tk.LEFT, pady=8)

        # 进度条
        self.progress = ttk.Progressbar(bar, mode="determinate",
                                        style="Modern.Horizontal.TProgressbar",
                                        length=200)
        self.progress.pack(side=tk.RIGHT, padx=(0, 15), pady=5)

    def _draw_status_dot(self, color):
        self.status_dot.delete("all")
        cx, cy, r = 7, 7, 4
        self.status_dot.create_oval(cx - r, cy - r, cx + r, cy + r,
                                    fill=color, outline="")

    # ═══ 事件处理 ═══

    def _select_file(self):
        path = filedialog.askopenfilename(
            title="选择 A2L 文件",
            filetypes=[("A2L/KP 文件", "*.a2l;*.kp"), ("A2L 文件", "*.a2l"), ("KP 文件", "*.kp"), ("所有文件", "*.*")]
        )
        if path:
            self.filepath.set(path)
            self._load_file()

    def _on_drop(self, event):
        files = self.root.tk.splitlist(event.data)
        if files:
            path = files[0].strip("{}")
            self.filepath.set(path)
            self._load_file()

    def _on_right_click(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.tree_menu.post(event.x_root, event.y_root)

    def _on_cell_edit(self, event):
        self._edit_selected()

    def _edit_selected(self):
        selection = self.tree.selection()
        if not selection:
            return
        item = selection[0]
        values = self.tree.item(item, "values")

        def save(new_val):
            new_values = list(values)
            new_values[3] = new_val
            self.tree.item(item, values=new_values)
            # 通过 entry_id 查找真实索引，避免过滤器导致索引错位
            entry_id = int(values[0])
            for i, entry in enumerate(self.entries):
                if entry["id"] == entry_id:
                    entry["translated"] = new_val
                    break
            self._refresh_row_tags(item)
            self._update_stats()

        EditDialog(self.root, values[2], values[3], save)

    def _clear_selected(self):
        for item in self.tree.selection():
            vals = list(self.tree.item(item, "values"))
            vals[3] = ""
            self.tree.item(item, values=vals)
            entry_id = int(vals[0])
            for entry in self.entries:
                if entry["id"] == entry_id:
                    entry["translated"] = ""
                    break
            self._refresh_row_tags(item)
        self._update_stats()

    def _copy_original(self):
        selection = self.tree.selection()
        if selection:
            values = self.tree.item(selection[0], "values")
            self.root.clipboard_clear()
            self.root.clipboard_append(values[2])
            self._set_status(f"已复制到剪贴板", "info")

    def _glossary_translate_selected(self):
        for item in self.tree.selection():
            vals = list(self.tree.item(item, "values"))
            result = translate_with_glossary_fast(vals[2], self.glossary, self.glossary_index)
            if not result:
                result = smart_translate_text_fast(vals[2], self.smart_exact, self.smart_pattern, self.smart_terms)
            if result:
                vals[3] = result
                self.tree.item(item, values=vals)
                entry_id = int(vals[0])
                for entry in self.entries:
                    if entry["id"] == entry_id:
                        entry["translated"] = result
                        break
                self._refresh_row_tags(item)
        self._update_stats()

    # ═══ 多源词典查询与验证 ═══

    def _multi_source_lookup_selected(self):
        """对选中的原文进行8大词典联合查询"""
        selection = self.tree.selection()
        if not selection:
            return
        item = selection[0]
        vals = self.tree.item(item, "values")
        original = vals[2]
        self._show_dict_lookup_dialog(original)

    def _multi_source_verify_selected(self):
        """对选中的翻译对进行多源验证"""
        selection = self.tree.selection()
        if not selection:
            return
        item = selection[0]
        vals = self.tree.item(item, "values")
        original = vals[2]
        translated = vals[3]
        if not translated:
            self._set_status("请先翻译该条目再验证", "warning")
            return
        self._show_dict_verify_dialog(original, translated)

    def _multi_source_verify(self):
        """批量多源验证所有已翻译条目"""
        if not self.entries:
            self._set_status("请先加载文件", "warning")
            return

        # 收集已翻译条目
        verified_pairs = [(e["original"], e["translated"])
                         for e in self.entries
                         if e.get("translated")]

        if not verified_pairs:
            self._set_status("没有已翻译条目可供验证", "warning")
            return

        if not self._dict_ready:
            self._set_status("多源词典引擎正在初始化，请稍候...", "working")
            self.root.after(1000, self._multi_source_verify)
            return

        self._set_status(f"正在用8大词典验证 {len(verified_pairs)} 条翻译...", "working")
        self.progress["value"] = 0

        # 去重（相同原文只查一次）
        seen = {}
        unique_pairs = []
        for en, zh in verified_pairs:
            if en not in seen:
                seen[en] = zh
                unique_pairs.append((en, zh))

        results = []
        total = len(unique_pairs)
        passed = 0
        failed = 0

        def verify_worker():
            nonlocal passed, failed
            for i, (en, zh) in enumerate(unique_pairs):
                try:
                    vr = self.multi_dict.verify_translation(en, zh)
                    results.append((en, zh, vr))
                    if vr["verified"]:
                        passed += 1
                    else:
                        failed += 1
                except Exception:
                    results.append((en, zh, {"verified": False, "confidence": 0,
                                            "best_alternative": ""}))

                # 每5条更新进度
                if (i + 1) % 5 == 0 or i == total - 1:
                    pct = (i + 1) / total * 100
                    self.root.after(0, lambda p=pct: self.progress.configure(value=p))
                    self.root.after(0, lambda d=i+1, t=total, ps=passed, fl=failed:
                        self._set_status(
                            f"验证中... {d}/{t}  |  通过 {ps}  |  未通过 {fl}",
                            "working"))

            # 完成 - 显示结果弹窗
            self.root.after(0, lambda: self._show_verify_summary(results, passed, failed, total))
            self.root.after(0, lambda: self.progress.configure(value=100))

        threading.Thread(target=verify_worker, daemon=True).start()

    def _show_dict_lookup_dialog(self, word):
        """弹出多源词典查询结果窗口"""
        if not self._dict_ready:
            self._set_status("多源词典引擎初始化中...", "working")
            return

        dialog = tk.Toplevel(self.root)
        dialog.title(f"多源词典查询 — {word}")
        dialog.geometry("650x500")
        dialog.minsize(500, 350)
        dialog.configure(bg=COLORS["bg"])
        dialog.transient(self.root)
        dialog.grab_set()

        # 标题栏
        header = tk.Frame(dialog, bg=COLORS["header_end"], height=50)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text=f"📚  多源词典联合查询",
                 font=("Microsoft YaHei UI", 13, "bold"),
                 fg="#FFFFFF", bg=COLORS["header_end"]).pack(side=tk.LEFT, padx=15, pady=10)
        tk.Label(header, text=f'"{word}"',
                 font=("Consolas", 11),
                 fg="#93C5FD", bg=COLORS["header_end"]).pack(side=tk.LEFT, padx=5, pady=10)

        # 内容区（可滚动）
        canvas = tk.Canvas(dialog, bg=COLORS["bg"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(dialog, orient=tk.VERTICAL, command=canvas.yview)
        scroll_frame = tk.Frame(canvas, bg=COLORS["bg"])

        scroll_frame.bind("<Configure>",
                         lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scroll_frame, anchor=tk.NW, tags="inner")
        canvas.configure(yscrollcommand=scrollbar.set)

        # 鼠标滚轮
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        dialog.bind_all("<MouseWheel>", _on_mousewheel)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0), pady=10)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y, pady=10)

        # Loading 提示
        loading = tk.Label(scroll_frame,
                          text="⏳  正在查询8大词典...",
                          font=("Microsoft YaHei UI", 11),
                          fg=COLORS["text_muted"], bg=COLORS["bg"])
        loading.pack(pady=20)

        self._set_status("正在查询多源词典...", "working")

        def query_worker():
            try:
                result = self.multi_dict.query_parallel(word)
            except Exception:
                result = None

            def update_ui():
                loading.destroy()

                if not result or not result["results"]:
                    tk.Label(scroll_frame,
                            text="未在任何词典源中查询到结果",
                            font=("Microsoft YaHei UI", 10),
                            fg=COLORS["text_muted"], bg=COLORS["bg"]).pack(pady=20)
                    self._set_status("词典查询未命中", "warning")
                    return

                # 综合信息卡片
                info_card = tk.Frame(scroll_frame, bg=COLORS["card"],
                                    bd=0, highlightbackground=COLORS["border"],
                                    highlightthickness=1)
                info_card.pack(fill=tk.X, pady=(0, 10))

                info_inner = tk.Frame(info_card, bg=COLORS["card"])
                info_inner.pack(fill=tk.X, padx=12, pady=10)

                # 置信度评分
                score = result["confidence_score"]
                score_color = (COLORS["success"] if score >= 80
                              else COLORS["warning"] if score >= 50
                              else COLORS["danger"])

                tk.Label(info_inner,
                        text=f"综合置信度",
                        font=("Microsoft YaHei UI", 8),
                        fg=COLORS["text_muted"], bg=COLORS["card"]).pack(anchor=tk.W)

                score_frame = tk.Frame(info_inner, bg=COLORS["card"])
                score_frame.pack(fill=tk.X, pady=(2, 0))

                score_bar_bg = tk.Frame(score_frame, bg=COLORS["border"],
                                       height=16)
                score_bar_bg.pack(side=tk.LEFT, fill=tk.X, expand=True)

                score_bar = tk.Frame(score_bar_bg, bg=score_color, height=16, width=0)
                score_bar.place(x=0, y=0, relwidth=score/100, height=16)

                tk.Label(score_frame,
                        text=f"  {score}/100  ({result['source_count']}/{result['total_sources']} 源命中)",
                        font=("Microsoft YaHei UI", 9, "bold"),
                        fg=score_color, bg=COLORS["card"]).pack(side=tk.LEFT, padx=(8, 0))

                # 最佳翻译
                if result["best_translation"]:
                    tk.Label(info_inner,
                            text=f"✓ 最佳翻译: {result['best_translation']}",
                            font=("Microsoft YaHei UI", 10, "bold"),
                            fg=COLORS["success"], bg=COLORS["card"]).pack(anchor=tk.W, pady=(6, 0))

                # 各词典源详细结果
                source_labels = {
                    "youdao": "有道词典",
                    "bing": "必应词典",
                    "cnki": "CNKI翻译助手",
                    "coca": "COCA语料库",
                    "wordreference": "WordReference",
                    "iciba": "金山词霸",
                    "iciba_pro": "爱词霸(专业)",
                }

                source_colors = {
                    "youdao": "#E53E3E",
                    "bing": "#3182CE",
                    "cnki": "#DD6B20",
                    "coca": "#805AD5",
                    "wordreference": "#00A86B",
                    "iciba": "#D69E2E",
                    "iciba_pro": "#2B6CB0",
                }

                for r in result["results"]:
                    src_key = next((k for k in source_labels if k in r.get("source", "").lower()), None)
                    src_color = source_colors.get(src_key, COLORS["text_muted"])

                    src_card = tk.Frame(scroll_frame, bg=COLORS["card"],
                                       bd=0, highlightbackground=COLORS["border"],
                                       highlightthickness=1)
                    src_card.pack(fill=tk.X, pady=(0, 6))

                    src_inner = tk.Frame(src_card, bg=COLORS["card"])
                    src_inner.pack(fill=tk.X, padx=12, pady=8)

                    src_header = tk.Frame(src_inner, bg=COLORS["card"])
                    src_header.pack(fill=tk.X)

                    dot = tk.Canvas(src_header, width=10, height=10,
                                   bg=COLORS["card"], highlightthickness=0)
                    dot.create_oval(1, 1, 9, 9, fill=src_color, outline="")
                    dot.pack(side=tk.LEFT, pady=1)

                    tk.Label(src_header,
                            text=f"  {r['source']}",
                            font=("Microsoft YaHei UI", 10, "bold"),
                            fg=COLORS["text"], bg=COLORS["card"]).pack(side=tk.LEFT)

                    tk.Label(src_header,
                            text=f"置信度: {r['confidence']}",
                            font=("Microsoft YaHei UI", 8),
                            fg=src_color, bg=COLORS["card"]).pack(side=tk.RIGHT)

                    for trans in r["translations"][:5]:
                        tk.Label(src_inner,
                                text=f"  • {trans}",
                                font=("Microsoft YaHei UI", 9),
                                fg=COLORS["text"], bg=COLORS["card"],
                                anchor=tk.W, justify=tk.LEFT).pack(anchor=tk.W, pady=1)

                self._set_status(
                    f"词典查询完成 — {result['source_count']} 源命中，置信度 {score}/100",
                    "success")

            self.root.after(0, update_ui)

        threading.Thread(target=query_worker, daemon=True).start()

    def _show_dict_verify_dialog(self, original, translated):
        """弹出翻译验证结果窗口"""
        if not self._dict_ready:
            self._set_status("多源词典引擎初始化中...", "working")
            return

        dialog = tk.Toplevel(self.root)
        dialog.title(f"翻译验证 — {original}")
        dialog.geometry("550x400")
        dialog.configure(bg=COLORS["bg"])
        dialog.transient(self.root)
        dialog.grab_set()

        header = tk.Frame(dialog, bg=COLORS["header_end"], height=50)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text="✅  多源词典翻译验证",
                 font=("Microsoft YaHei UI", 13, "bold"),
                 fg="#FFFFFF", bg=COLORS["header_end"]).pack(side=tk.LEFT, padx=15, pady=10)

        content = tk.Frame(dialog, bg=COLORS["bg"])
        content.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        # 原文 / 译文
        row1 = tk.Frame(content, bg=COLORS["bg"])
        row1.pack(fill=tk.X, pady=(0, 10))
        tk.Label(row1, text="原文:", font=("Microsoft YaHei UI", 9, "bold"),
                fg=COLORS["text_muted"], bg=COLORS["bg"]).pack(anchor=tk.W)
        tk.Label(row1, text=original, font=("Consolas", 11),
                fg=COLORS["text"], bg=COLORS["bg"],
                wraplength=500).pack(anchor=tk.W, pady=(2, 0))

        row2 = tk.Frame(content, bg=COLORS["bg"])
        row2.pack(fill=tk.X, pady=(0, 10))
        tk.Label(row2, text="译文:", font=("Microsoft YaHei UI", 9, "bold"),
                fg=COLORS["text_muted"], bg=COLORS["bg"]).pack(anchor=tk.W)
        tk.Label(row2, text=translated, font=("Microsoft YaHei UI", 11),
                fg=COLORS["primary"], bg=COLORS["bg"],
                wraplength=500).pack(anchor=tk.W, pady=(2, 0))

        sep = tk.Frame(content, bg=COLORS["border"], height=1)
        sep.pack(fill=tk.X, pady=10)

        self.result_frame = tk.Frame(content, bg=COLORS["bg"])
        self.result_frame.pack(fill=tk.BOTH, expand=True)

        loading = tk.Label(self.result_frame, text="⏳  正在查询8大词典验证...",
                          font=("Microsoft YaHei UI", 10),
                          fg=COLORS["text_muted"], bg=COLORS["bg"])
        loading.pack(pady=15)

        def verify_worker():
            try:
                vr = self.multi_dict.verify_translation(original, translated)
            except Exception:
                vr = {"verified": False, "confidence": 0, "best_alternative": ""}

            def update():
                loading.destroy()

                if vr["verified"]:
                    verified_label = tk.Label(self.result_frame,
                        text=f"✅ 验证通过! (置信度: {vr['confidence']}/100, {vr['sources']} 源确认)",
                        font=("Microsoft YaHei UI", 11, "bold"),
                        fg=COLORS["success"], bg=COLORS["bg"])
                    verified_label.pack(anchor=tk.W, pady=(0, 10))
                else:
                    tk.Label(self.result_frame,
                        text=f"⚠️ 验证未通过 (置信度: {vr['confidence']}/100)",
                        font=("Microsoft YaHei UI", 11, "bold"),
                        fg=COLORS["warning"], bg=COLORS["bg"]).pack(anchor=tk.W, pady=(0, 10))
                    if vr.get("best_alternative"):
                        alt_frame = tk.Frame(self.result_frame, bg="#FFF3CD")
                        alt_frame.pack(fill=tk.X, pady=(0, 10))
                        tk.Label(alt_frame,
                            text=f"  建议翻译: {vr['best_alternative']}",
                            font=("Microsoft YaHei UI", 10),
                            fg="#856404", bg="#FFF3CD").pack(anchor=tk.W, padx=10, pady=6)

            self.root.after(0, update)

        threading.Thread(target=verify_worker, daemon=True).start()

    def _show_verify_summary(self, results, passed, failed, total):
        """显示批量验证总结窗口"""
        dialog = tk.Toplevel(self.root)
        dialog.title("多源词典批量验证报告")
        dialog.geometry("700x500")
        dialog.configure(bg=COLORS["bg"])
        dialog.transient(self.root)

        header = tk.Frame(dialog, bg=COLORS["header_end"], height=50)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text="📊  8大词典批量验证报告",
                 font=("Microsoft YaHei UI", 13, "bold"),
                 fg="#FFFFFF", bg=COLORS["header_end"]).pack(side=tk.LEFT, padx=15, pady=10)

        # 统计卡片
        stats_frame = tk.Frame(dialog, bg=COLORS["bg"])
        stats_frame.pack(fill=tk.X, padx=20, pady=15)

        cards = [
            ("总验证", total, COLORS["primary"]),
            ("✅ 通过", passed, COLORS["success"]),
            ("⚠️ 未通过", failed, COLORS["warning"] if failed > 0 else COLORS["success"]),
            ("通过率", f"{passed/total*100:.1f}%" if total > 0 else "0%", COLORS["success"] if passed >= total * 0.8 else COLORS["warning"]),
        ]
        for title, value, color in cards:
            card = tk.Frame(stats_frame, bg=COLORS["card"],
                           bd=0, highlightbackground=COLORS["border"],
                           highlightthickness=1)
            card.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
            tk.Label(card, text=title, font=("Microsoft YaHei UI", 8),
                    fg=COLORS["text_muted"], bg=COLORS["card"]).pack(pady=(8, 0))
            tk.Label(card, text=str(value), font=("Microsoft YaHei UI", 18, "bold"),
                    fg=color, bg=COLORS["card"]).pack(pady=(0, 8))

        # 未通过列表
        failed_list = [(en, zh, vr) for en, zh, vr in results if not vr["verified"]]
        if failed_list:
            tk.Label(dialog, text=f"  未通过验证的条目 ({len(failed_list)}条):",
                    font=("Microsoft YaHei UI", 9, "bold"),
                    fg=COLORS["warning"], bg=COLORS["bg"]).pack(anchor=tk.W, padx=20)

            tree_frame = tk.Frame(dialog, bg=COLORS["bg"])
            tree_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=(5, 10))

            vtree = ttk.Treeview(tree_frame,
                                columns=("original", "translated", "alternative", "confidence"),
                                show="headings", height=10)
            vtree.heading("original", text="原文")
            vtree.heading("translated", text="当前译文")
            vtree.heading("alternative", text="词典建议")
            vtree.heading("confidence", text="置信度")

            vtree.column("original", width=200)
            vtree.column("translated", width=120)
            vtree.column("alternative", width=200)
            vtree.column("confidence", width=60, anchor=tk.CENTER)

            vscroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=vtree.yview)
            vtree.configure(yscrollcommand=vscroll.set)

            for en, zh, vr in failed_list:
                alt = vr.get("best_alternative", "")
                conf = vr.get("confidence", 0)
                vtree.insert("", tk.END, values=(en, zh, alt, f"{conf}%"))

            vtree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            vscroll.pack(side=tk.RIGHT, fill=tk.Y)
        else:
            tk.Label(dialog, text="  🎉 全部通过验证!",
                    font=("Microsoft YaHei UI", 11),
                    fg=COLORS["success"], bg=COLORS["bg"]).pack(pady=20)

        close_btn = ttk.Button(dialog, text="关闭", command=dialog.destroy,
                              style="Secondary.TButton")
        close_btn.pack(pady=(0, 15))

        self._set_status(
            f"验证完成 — 通过率 {passed/total*100:.1f}% ({passed}/{total})",
            "success" if passed >= total * 0.8 else "warning")

    def _refresh_row_tags(self, item):
        vals = self.tree.item(item, "values")
        tag = "translated" if vals[3] else "untranslated"
        self.tree.item(item, tags=(tag,))

    # ═══ 状态更新 ═══

    def _set_status(self, msg, level="info"):
        colors = {"info": "#BFDBFE", "success": "#6EE7B7", "warning": "#FCD34D",
                  "error": "#FCA5A5", "working": "#93C5FD"}
        dot_colors = {"info": COLORS["text_muted"], "success": COLORS["success"],
                      "warning": COLORS["warning"], "error": COLORS["danger"],
                      "working": COLORS["primary"]}
        self.status_label.config(text=msg, fg=colors.get(level, "#BFDBFE"))
        self._draw_status_dot(dot_colors.get(level, COLORS["text_muted"]))
        self.root.update_idletasks()

    def _make_progress_dots(self, done, total, width=20):
        """生成 ●/○ 点阵进度条"""
        filled = int(done / total * width) if total > 0 else 0
        empty = width - filled
        return "●" * filled + "○" * empty

    def _update_stats(self):
        query = self.filter_var.get().strip().lower()
        placeholder = "输入关键词搜索... (支持原文/译文/名称，实时过滤)"
        if query and query != placeholder.lower():
            # 过滤中：显示过滤后的统计
            tree_count = len(self.tree.get_children())
            self.table_stats.config(text=f"搜索 \"{self.filter_var.get()}\" — 显示 {tree_count}/{len(self.entries)} 条")
            return
        total = len(self.entries)
        translated = sum(1 for e in self.entries if e.get("translated"))
        if self._tree_loaded < total:
            self.table_stats.config(
                text=f"共 {total} 条  |  已翻译 {translated}  |  已加载 {self._tree_loaded}/{total}")
        else:
            self.table_stats.config(text=f"共 {total} 条  |  已翻译 {translated}  |  未翻译 {total - translated}")

    def _show_empty_hint(self, text=""):
        if text:
            self.empty_hint.config(text=text)
            self.empty_hint.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        else:
            self.empty_hint.place_forget()

    # ═══ 核心操作 ═══

    def _load_file(self):
        path = self.filepath.get()
        if not path or not os.path.isfile(path):
            if not path:
                self._select_file()
                return
            self._set_status("文件不存在", "error")
            return

        if self.is_processing:
            return

        # ── 模态加载窗口 ──
        load_win = tk.Toplevel(self.root)
        load_win.title("加载中")
        load_win.geometry("400x130")
        load_win.resizable(False, False)
        load_win.transient(self.root)
        load_win.grab_set()
        load_win.configure(bg="#FFFFFF")
        # 居中
        load_win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - 400) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - 130) // 2
        load_win.geometry(f"+{x}+{y}")

        fname = os.path.basename(path)
        # 标题
        tk.Label(load_win, text=f"正在加载 {fname}...",
                 font=("Microsoft YaHei UI", 12, "bold"),
                 fg="#1E293B", bg="#FFFFFF").pack(pady=(18, 6))
        # 状态文字
        status_var = tk.StringVar(value="解析文件中...")
        tk.Label(load_win, textvariable=status_var,
                 font=("Microsoft YaHei UI", 9),
                 fg="#64748B", bg="#FFFFFF").pack()
        # 进度条
        load_progress = ttk.Progressbar(load_win, mode="indeterminate", length=320)
        load_progress.pack(pady=(8, 0))
        load_progress.start(15)

        # 取消按钮（仅加载阶段可取消）
        cancel_flag = [False]

        def do_cancel():
            cancel_flag[0] = True
            load_win.destroy()

        cancel_btn = tk.Button(load_win, text="取消", command=do_cancel,
                               font=("Microsoft YaHei UI", 9),
                               bg="#F1F5F9", fg="#64748B", borderwidth=0,
                               activebackground="#E2E8F0", cursor="hand2",
                               padx=20, pady=4)
        cancel_btn.pack(pady=(4, 0))

        self.is_processing = True
        self._set_status(f"正在加载 {fname}...", "working")
        self.progress["value"] = 0

        def worker():
            # 检测文件类型
            is_kp = path.lower().endswith('.kp')

            # 阶段 1: 解析文件
            try:
                if is_kp:
                    # KP 文件用专用解析器
                    from kp_parser import parse_kp_header, extract_translatable as kp_extract
                    with open(path, 'rb') as f:
                        kp_data = f.read()
                    kp_info = parse_kp_header(kp_data)
                    entries = kp_extract(kp_info)
                    original_content = kp_data
                else:
                    entries, original_content = parse_a2l(path)
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("解析错误", f"无法解析文件:\n{e}"))
                self.root.after(0, lambda: self._set_status("解析失败", "error"))
                self.root.after(0, load_win.destroy)
                self.root.after(0, lambda: setattr(self, "is_processing", False))
                return

            if cancel_flag[0]:
                self.root.after(0, load_win.destroy)
                self.root.after(0, lambda: setattr(self, "is_processing", False))
                return

            # 阶段 2: 词典匹配
            if self.use_builtin.get():
                self.root.after(0, lambda: status_var.set("正在词典匹配..."))
                total = len(entries)
                count = 0
                batch_interval = max(1, total // 20)

                for i, entry in enumerate(entries):
                    if not entry.get("translated"):
                        result = translate_with_glossary_fast(
                            entry["original"], self.glossary, self.glossary_index)
                        if result:
                            entry["translated"] = result
                            count += 1

                    if cancel_flag[0]:
                        break

                    # 每 100 条释放 GIL，让主线程能处理 UI 事件
                    if i % 100 == 0:
                        time.sleep(0)

                    # 更新 UI 进度
                    if (i + 1) % batch_interval == 0 or i == total - 1:
                        pct = (i + 1) / total * 100
                        dots = "●" * int(pct / 5) + "○" * (20 - int(pct / 5))
                        self.root.after(0, lambda p=pct: self.progress.configure(value=p))
                        self.root.after(0, lambda d=dots, n=i+1, t=total, c=count:
                                        status_var.set(f"词典匹配 {d}  {n}/{t}  命中{c}"))
            else:
                count = 0

            if cancel_flag[0]:
                self.root.after(0, load_win.destroy)
                self.root.after(0, lambda: setattr(self, "is_processing", False))
                return

            # ── 阶段 3: 逐词智能翻译（单次正则扫描，O(n)，带进度+取消）──
            smart_count = 0
            self.root.after(0, lambda: status_var.set("正在逐词智能翻译..."))
            smart_targets = [(i, e) for i, e in enumerate(entries)
                            if not e.get("translated") and e["type"] not in _SMART_SKIP_TYPES]
            smart_total = len(smart_targets)
            smart_batch = max(1, smart_total // 20) if smart_total > 20 else 1

            for j, (i, entry) in enumerate(smart_targets):
                result = smart_translate_text_fast(entry["original"],
                                                   self.smart_exact, self.smart_pattern, self.smart_terms)
                if result:
                    entry["translated"] = result
                    entry["status"] = "auto"
                    smart_count += 1

                if cancel_flag[0]:
                    break

                # 每 100 条释放 GIL
                if j % 100 == 0:
                    time.sleep(0)

                if (j + 1) % smart_batch == 0 or j == smart_total - 1:
                    pct = (j + 1) / smart_total * 100 if smart_total else 100
                    dots = "●" * int(pct / 5) + "○" * (20 - int(pct / 5))
                    self.root.after(0, lambda d=dots, n=j+1, t=smart_total, c=smart_count:
                                    status_var.set(f"逐词翻译 {d}  {n}/{t}  命中{c}"))

            if cancel_flag[0]:
                self.root.after(0, load_win.destroy)
                self.root.after(0, lambda: setattr(self, "is_processing", False))
                return

            # ── 回 UI 线程：懒加载 TreeView（先显示前 200 条，其余用 after_idle 渐进加载）──
            def on_done():
                self.entries = entries
                self.original_content = original_content
                self._cancel_tree_loading()
                self.tree.delete(*self.tree.get_children())

                self.path_label.config(text=f"📄  {fname}", fg=COLORS["text"])
                self._show_empty_hint()
                self.progress["value"] = 100
                self.is_processing = False
                load_win.destroy()

                # 启动懒加载
                self._start_lazy_tree_load(reset=True)

                if self.use_builtin.get():
                    total_matched = count + smart_count
                    pct = total_matched * 100 // total if total else 0
                    msg = f"已加载 {fname} — {total} 条, 翻译 {total_matched} 条 ({pct}%)"
                    if smart_count > 0:
                        msg += f" [词典{count} + 逐词{smart_count}]"
                    self._set_status(msg, "success")
                else:
                    self._set_status(f"已加载 {fname} — {len(entries)} 条文本", "success")

            self.root.after(0, on_done)

        threading.Thread(target=worker, daemon=True).start()

    def _glossary_translate(self):
        """手动词典匹配全部条目（后台执行，含逐词智能翻译）"""
        if not self.entries:
            self._set_status("请先加载文件", "warning")
            return
        if self.is_processing:
            return

        self.is_processing = True
        self._set_status("正在词典匹配...", "working")
        self.progress["value"] = 0

        total = len(self.entries)
        _last_ui = [0.0]

        def _throttled_status(msg):
            now = time.time()
            if now - _last_ui[0] < 0.2:
                return
            _last_ui[0] = now
            self.root.after_idle(lambda m=msg: self._set_status(m, "working"))

        def worker():
            # ── 第一遍：词典精确匹配 ──
            count = 0
            batch_interval = max(1, total // 20)
            for i, entry in enumerate(self.entries):
                if not entry.get("translated"):
                    result = translate_with_glossary_fast(
                        entry["original"], self.glossary, self.glossary_index)
                    if result:
                        entry["translated"] = result
                        count += 1
                if i % 100 == 0:
                    time.sleep(0)  # 释放 GIL
                if (i + 1) % batch_interval == 0 or i == total - 1:
                    pct = (i + 1) / total * 100
                    dots = "●" * int(pct / 5) + "○" * (20 - int(pct / 5))
                    self.root.after_idle(lambda p=pct: self.progress.configure(value=p))
                    _throttled_status(f"词典匹配 {dots}  {i+1}/{total}  命中{count}")

            # ── 第二遍：逐词智能翻译（带进度）──
            smart_count = 0
            _throttled_status("正在逐词智能翻译...")
            smart_targets = [(i, e) for i, e in enumerate(self.entries)
                            if not e.get("translated") and e["type"] not in _SMART_SKIP_TYPES]
            smart_total = len(smart_targets)
            smart_batch = max(1, smart_total // 20) if smart_total > 20 else 1
            for j, (i, entry) in enumerate(smart_targets):
                result = smart_translate_text_fast(entry["original"],
                                                   self.smart_exact, self.smart_pattern, self.smart_terms)
                if result:
                    entry["translated"] = result
                    entry["status"] = "auto"
                    smart_count += 1
                if j % 100 == 0:
                    time.sleep(0)  # 释放 GIL
                if (j + 1) % smart_batch == 0 or j == smart_total - 1:
                    pct = (j + 1) / smart_total * 100 if smart_total else 100
                    dots = "●" * int(pct / 5) + "○" * (20 - int(pct / 5))
                    self.root.after_idle(lambda p=pct: self.progress.configure(value=p))
                    _throttled_status(f"逐词翻译 {dots}  {j+1}/{smart_total}  命中{smart_count}")

            total_matched = count + smart_count
            pct = total_matched * 100 // total
            msg = f"翻译完成 — {total_matched} 条 ({pct}%)"
            if smart_count > 0:
                msg += f" [词典{count} + 逐词{smart_count}]"

            def on_done():
                def finish():
                    self.progress["value"] = 100
                    self._set_status(msg, "success")
                    self._update_stats()
                    self.is_processing = False
                self._sync_table(callback=finish)

            self.root.after(0, on_done)

        threading.Thread(target=worker, daemon=True).start()

    def _sync_table(self, callback=None):
        """异步批量同步 entries 到 TreeView（每批 200 行 + 50ms 间隔 + update_idletasks 防卡死）"""
        entries = self.entries
        total = len(entries)
        BATCH = 200

        # 取消懒加载，直接覆盖
        self._cancel_tree_loading()

        def update_batch(start):
            if start >= total:
                self._tree_loaded = total
                self._tree_full_loaded = True
                if callback:
                    callback()
                return

            end = min(start + BATCH, total)
            children = self.tree.get_children()
            for i in range(start, end):
                if i < len(children):
                    entry = entries[i]
                    tag = "translated" if entry.get("translated") else "untranslated"
                    self.tree.item(children[i], values=(
                        entry["type"], entry.get("name", ""),
                        entry["original"], entry.get("translated", ""),
                    ), tags=(tag,))

            self.progress["value"] = end / total * 100 if total else 0

            # 只用 update_idletasks，不阻塞用户交互
            try:
                self.root.update_idletasks()
            except tk.TclError:
                pass

            self.root.after(50, lambda: update_batch(end))

        update_batch(0)

    def _auto_translate(self):
        if self.is_processing:
            return
        if not self.entries:
            self._set_status("请先加载文件", "warning")
            return

        untranslated = [(i, e) for i, e in enumerate(self.entries)
                        if not e.get("translated")]
        if not untranslated:
            self._set_status("全部已翻译完毕 ✓", "success")
            messagebox.showinfo("完成", "所有条目已翻译完毕！")
            return

        self.is_processing = True
        for btn_name in ["btn_0", "btn_1", "btn_2", "btn_3"]:
            if hasattr(self, btn_name):
                getattr(self, btn_name).config(state="disabled")
        if hasattr(self, "btn_verify"):
            self.btn_verify.config(state="disabled")

        def worker():
            src = self.src_lang.get()
            tgt = self.tgt_lang.get()
            batch_n = self.batch_size.get()
            delay = self.delay.get()
            use_batch = self.use_batch.get()
            bid = (self.baidu_appid.get() or "").strip()
            bsk = (self.baidu_secret.get() or "").strip()
            use_baidu = _HAS_BAIDU and bid and bsk

            total = len(untranslated)
            done = 0
            success = 0

            # ── 节流 UI 更新：每 200ms 最多更新一次 ──
            _last_ui_update = [0.0]

            def update_ui_throttled(d, t, s):
                now = time.time()
                if d < t and now - _last_ui_update[0] < 0.2:
                    return  # 还没到 200ms，跳过
                _last_ui_update[0] = now

                dots = self._make_progress_dots(d, t)
                engine = "百度翻译" if use_baidu else "MyMemory"
                msg = f"翻译中[{engine}] {dots}  {d}/{t}  ✓{s}"
                # 使用 after_idle 而非 after(0)，在空闲时更新
                self.root.after_idle(lambda m=msg: self._set_status(m, "working"))
                self.root.after_idle(lambda v=d/t*100: self.progress.configure(value=v))

            update_ui_throttled(0, total, 0)

            def _baidu_batch(batch_data):
                batch, texts = batch_data
                result = baidu_translate_batch(texts, src, tgt, bid, bsk, self.ssl_ctx)
                if result:
                    local_success = 0
                    for j, (idx, entry) in enumerate(batch):
                        if j < len(result) and result[j]:
                            entry["translated"] = result[j]
                            local_success += 1
                    return len(batch), local_success
                return len(batch), 0

            def _mymemory_batch(batch_data):
                batch, texts = batch_data
                result = api_translate_batch(texts, src, tgt, self.ssl_ctx)
                if result:
                    local_success = 0
                    for j, (idx, entry) in enumerate(batch):
                        if j < len(result) and result[j]:
                            entry["translated"] = result[j]
                            local_success += 1
                    return len(batch), local_success
                else:
                    local_success = 0
                    for idx, entry in batch:
                        trans = api_translate_one(entry["original"], src, tgt,
                                                  self.ssl_ctx)
                        if trans:
                            entry["translated"] = trans
                            local_success += 1
                        time.sleep(delay)
                    return len(batch), local_success

            if use_batch:
                translate_batch = _baidu_batch if use_baidu else _mymemory_batch

                max_workers = min(3, max(1, (total + batch_n - 1) // batch_n))
                batches = []
                for bs in range(0, total, batch_n):
                    batch = untranslated[bs:bs + batch_n]
                    texts = [e["original"] for _, e in batch]
                    batches.append((batch, texts))

                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {executor.submit(translate_batch, b): b for b in batches}
                    for future in as_completed(futures):
                        try:
                            batch_done, batch_success = future.result()
                            done += batch_done
                            success += batch_success
                            update_ui_throttled(done, total, success)
                        except Exception:
                            batch = futures[future][0]
                            done += len(batch)
                            update_ui_throttled(done, total, success)
            else:
                for i, (idx, entry) in enumerate(untranslated):
                    if use_baidu:
                        trans = baidu_translate_one(entry["original"], src, tgt,
                                                    bid, bsk, self.ssl_ctx)
                    else:
                        trans = api_translate_one(entry["original"], src, tgt,
                                                  self.ssl_ctx)
                    if trans:
                        entry["translated"] = trans
                        success += 1
                    done += 1
                    update_ui_throttled(done, total, success)
                    time.sleep(delay)

            # 最终更新
            self.root.after(0, lambda: self.progress.configure(value=100))
            self.root.after(0, self._update_stats)

            engine = "百度翻译" if use_baidu else "MyMemory"
            def finalize():
                self._set_status(
                    f"翻译完成[{engine}] — {success}/{total} 成功",
                    "success" if success > 0 else "warning")
                # 异步刷新 TreeView
                self._sync_table(callback=lambda: setattr(self, "is_processing", False))
                for i in range(4):
                    if hasattr(self, f"btn_{i}"):
                        getattr(self, f"btn_{i}").config(state="normal")
                if hasattr(self, "btn_verify"):
                    self.btn_verify.config(state="normal")

            self.root.after(0, finalize)

        threading.Thread(target=worker, daemon=True).start()

    def _export(self):
        if not self.entries or not self.original_content:
            self._set_status("请先加载并翻译文件", "warning")
            return

        translated = sum(1 for e in self.entries if e.get("translated"))
        if translated == 0:
            if not messagebox.askyesno("确认", "没有任何译文，导出为原文吗？"):
                return

        path = filedialog.asksaveasfilename(
            title="导出翻译后 A2L",
            defaultextension=".a2l",
            filetypes=[("A2L/KP 文件", "*.a2l;*.kp"), ("A2L 文件", "*.a2l"), ("KP 文件", "*.kp"), ("所有文件", "*.*")]
        )
        if not path:
            return

        self.is_processing = True
        self._set_status("正在导出...", "working")
        self.progress["value"] = 0
        self.progress.configure(mode="indeterminate")
        self.progress.start()

        def worker():
            try:
                is_kp = path.lower().endswith('.kp')
                if is_kp and isinstance(self.original_content, bytes):
                    # KP 文件二进制写回
                    from kp_parser import rebuild_kp
                    new_data = rebuild_kp(self.original_content, self.entries)
                    with open(path, "wb") as f:
                        f.write(new_data)
                else:
                    content = apply_translations(self.original_content, self.entries)
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(content)
                self.root.after(0, lambda: (
                    self.progress.stop(),
                    self.progress.configure(mode="determinate", value=100),
                    self._set_status(f"导出成功 → {os.path.basename(path)}", "success"),
                    messagebox.showinfo("导出成功",
                                        f"文件已保存:\n{path}\n\n已翻译 {translated}/{len(self.entries)} 条"),
                    setattr(self, "is_processing", False)
                ))
            except Exception as e:
                self.root.after(0, lambda: (
                    self.progress.stop(),
                    self.progress.configure(mode="determinate", value=0),
                    messagebox.showerror("导出失败", str(e)),
                    self._set_status("导出失败", "error"),
                    setattr(self, "is_processing", False)
                ))

        threading.Thread(target=worker, daemon=True).start()

    def _export_csv(self):
        if not self.entries:
            return
        path = filedialog.asksaveasfilename(
            title="导出 CSV",
            defaultextension=".csv",
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")]
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["type", "name", "original", "translated"])
                for e in self.entries:
                    writer.writerow(
                        [e["type"], e["name"], e["original"], e.get("translated", "")])
            self._set_status(f"CSV 导出 → {os.path.basename(path)}", "success")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    def _import_csv(self):
        path = filedialog.askopenfilename(
            title="导入 CSV 译文",
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")]
        )
        if not path:
            return
        try:
            import_map = {}
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    orig = row.get("original", "").strip()
                    trans = row.get("translated", "").strip()
                    if orig and trans:
                        import_map[orig] = trans

            count = 0
            for entry in self.entries:
                if entry["original"] in import_map and import_map[entry["original"]]:
                    entry["translated"] = import_map[entry["original"]]
                    count += 1

            self._sync_table()
            self._update_stats()
            self._set_status(f"CSV 导入 → {count} 条更新", "success")
        except Exception as e:
            messagebox.showerror("导入失败", str(e))

    def _save_progress(self):
        if not self.entries:
            return
        path = filedialog.asksaveasfilename(
            title="保存翻译进度",
            defaultextension=".json",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")]
        )
        if not path:
            return
        try:
            data = {
                "source_file": self.filepath.get(),
                "original_content": self.original_content,
                "entries": [{k: v for k, v in e.items()} for e in self.entries]
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self._set_status(f"进度保存 → {os.path.basename(path)}", "success")
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    def _load_progress(self):
        path = filedialog.askopenfilename(
            title="恢复翻译进度",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")]
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.entries = data.get("entries", [])
            self.original_content = data.get("original_content", "")
            if data.get("source_file"):
                self.filepath.set(data["source_file"])

            # 使用懒加载
            self._cancel_tree_loading()
            self.tree.delete(*self.tree.get_children())
            self._start_lazy_tree_load(reset=True)

            self.path_label.config(
                text=f"📄  {os.path.basename(data.get('source_file', '未知'))}",
                fg=COLORS["text"])
            self._show_empty_hint()
            self._update_stats()
            self._set_status(f"进度恢复 — {len(self.entries)} 条", "success")
        except Exception as e:
            messagebox.showerror("恢复失败", str(e))

    # ── Tooltip ──
    def _show_tooltip(self, event, text):
        """鼠标悬停提示"""
        if hasattr(self, '_tooltip_win') and self._tooltip_win:
            self._tooltip_win.destroy()
        x, y = event.x_root + 15, event.y_root + 10
        self._tooltip_win = tw = tk.Toplevel(self.root)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tw.attributes("-topmost", True)
        label = tk.Label(tw, text=text, justify=tk.LEFT,
                         background="#FFFDE7", foreground="#333333",
                         font=("Microsoft YaHei UI", 9),
                         relief=tk.SOLID, borderwidth=1,
                         padx=8, pady=5)
        label.pack()

    def _hide_tooltip(self):
        if hasattr(self, '_tooltip_win') and self._tooltip_win:
            self._tooltip_win.destroy()
            self._tooltip_win = None

    # ── 升级说明 ──
    def _show_upgrade_info(self):
        """显示模块化升级说明"""
        import sys
        is_frozen = getattr(sys, 'frozen', False)

        if is_frozen:
            exe_dir = Path(sys.executable).parent
        else:
            exe_dir = Path(__file__).parent

        msg = (
            f"当前版本: v2.9.5\n\n"
            f"━━━━━ 单文件纯净版 ━━━━━\n\n"
            f"📦 所有功能集成在一个 exe 中\n"
            f"   术语库 · 词典引擎 · 翻译核心\n"
            f"   全部内置，即开即用\n\n"
            f"━━━━━ 如何升级 ━━━━━\n\n"
            f"🔹 获取新版 A2L_Translator.exe\n"
            f"🔹 直接替换旧版 exe 文件\n"
            f"🔹 双击新 exe 即可运行\n\n"
            f"━━━━━━ 当前位置 ━━━━━\n"
            f"程序目录: {exe_dir}"
        )

        messagebox.showinfo("📦 关于本程序", msg)

def main():
    root = tk.Tk()
    root.withdraw()  # 先隐藏，避免 PyInstaller onefile 模式下闪现小窗口

    icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "A2L_Translator.ico")
    if not os.path.exists(icon_path):
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "WinOLS_Toolkit.ico")
    if not os.path.exists(icon_path):
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
    try:
        if os.path.exists(icon_path):
            root.iconbitmap(icon_path)
    except Exception:
        try:
            root.iconbitmap(default="")
        except Exception:
            pass
    A2LTranslatorGUI(root)
    root.after(50, root.deiconify)  # UI 构建完成后显示
    root.mainloop()


if __name__ == "__main__":
    main()
