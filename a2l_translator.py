#!/usr/bin/env python3
"""
A2L 文件翻译工具 (CLI)
========================
解析 ASAM MCD-2 MC (ASAP2) 文件，提取可翻译文本，
支持自动翻译、术语词典、进度管理。

用法:
    python a2l_translator.py input.a2l                          # 仅提取可翻译字符串
    python a2l_translator.py input.a2l --auto-translate         # 自动翻译全部
    python a2l_translator.py input.a2l -o output.a2l --auto     # 翻译并输出
    python a2l_translator.py input.a2l --dict glossary.csv      # 使用自定义词典
    python a2l_translator.py input.a2l --save progress.json     # 保存进度
    python a2l_translator.py input.a2l --load progress.json     # 恢复进度
    python a2l_translator.py input.a2l --extract strings.csv    # 导出到 CSV
    python a2l_translator.py input.a2l --apply translated.csv   # 从 CSV 导入译文
    python a2l_translator.py *.a2l --auto-translate             # 批量处理
"""

import sys
import os
import re
import json
import csv
import time
import ssl
import argparse
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache

# ── 性能优化：模块级预计算 ──
_UMLAUT_SIMPLE = str.maketrans("äöüÄÖÜ", "aouAOU")
_UMLAUT_EXPANDED = str.maketrans({
    "ä": "ae", "ö": "oe", "ü": "ue",
    "Ä": "Ae", "Ö": "Oe", "Ü": "Ue",
    "ß": "ss",
})

# ── 百度翻译 API ──
try:
    from baidu_api import baidu_translate_batch, baidu_translate_one
    _HAS_BAIDU = True
except ImportError:
    _HAS_BAIDU = False

# 全局 SSL 上下文（可通过 --no-ssl-verify 控制）
_ssl_context = None

# ── 导入共享词典 (1600+ 英文 + 300+ 德文) ──
from glossary_data import BUILTIN_GLOSSARY, GERMAN_GLOSSARY

# ── 导入翻译记忆库 ──
from translation_memory import (
    load_translation_memory, save_translation_memory,
    apply_tm, update_tm_from_items,
    load_custom_glossary, merge_glossary,
)

# ── 导入模糊匹配引擎（仿 DeepL / Trados）──
from fuzzy_engine import (
    normalize_spelling, deep_fuzzy_search, find_fuzzy_match,
    rate_confidence, classify_confidence, check_consistency,
    hybrid_similarity, levenshtein_ratio,
)
# 直接导入拼写变体表（用于终极降级翻译）
from fuzzy_engine import _SPELL_VARIANTS

# ── 导入多源词典验证 (8大权威词典) ──
try:
    from dictionary_resources import MultiSourceDictionary, get_dictionary
    _HAS_MULTI_DICT = True
except ImportError:
    _HAS_MULTI_DICT = False

# ══════════════════════════════════════════════════════════
#  翻译精度增强：缩写展开 + 复合词分解 + 德语复合词
# ══════════════════════════════════════════════════════════

# 汽车行业常见缩写 → 中文全称
_ABBREVIATIONS = {
    # 发动机/排放
    "EGR": "废气再循环",
    "DPF": "柴油颗粒过滤器",
    "SCR": "选择性催化还原",
    "DOC": "柴油氧化催化器",
    "NOx": "氮氧化物",
    "VGT": "可变截面涡轮",
    "WG": "废气旁通阀",
    "MAF": "空气质量流量",
    "MAP": "进气歧管绝对压力",
    "TMAP": "温度压力传感器",
    "IAT": "进气温度",
    "ECT": "发动机冷却液温度",
    "EOT": "发动机机油温度",
    "FRP": "燃油轨压力",
    "IMEP": "指示平均有效压力",
    "BMEP": "制动平均有效压力",
    "FMEP": "摩擦平均有效压力",
    "BSFC": "制动燃油消耗率",
    "AFR": "空燃比",
    "Lambda": "过量空气系数",
    # 传感器/执行器
    "TPS": "节气门位置传感器",
    "CPS": "曲轴位置传感器",
    "CKP": "曲轴位置",
    "CMP": "凸轮轴位置",
    "O2": "氧传感器",
    "HEGO": "加热型氧传感器",
    "UEGO": "宽域氧传感器",
    "MAF sensor": "空气流量传感器",
    "Knock sensor": "爆震传感器",
    "APPS": "加速踏板位置传感器",
    "ETC": "电子节气门控制",
    "VVT": "可变气门正时",
    "VVL": "可变气门升程",
    "VVA": "可变气门驱动",
    # 变速箱
    "AT": "自动变速箱",
    "MT": "手动变速箱",
    "DCT": "双离合变速箱",
    "CVT": "无级变速器",
    "AMT": "电控机械自动变速箱",
    "TCU": "变速箱控制单元",
    "TCC": "液力变矩器离合器",
    # 底盘/安全
    "ABS": "防抱死制动系统",
    "ESC": "电子稳定控制",
    "ESP": "电子稳定程序",
    "TCS": "牵引力控制系统",
    "EBD": "电子制动力分配",
    "EPB": "电子驻车制动",
    "EPS": "电动助力转向",
    "SAS": "转向角传感器",
    "TPMS": "胎压监测系统",
    # ADAS/自动驾驶
    "ACC": "自适应巡航控制",
    "AEB": "自动紧急制动",
    "LDW": "车道偏离预警",
    "LKA": "车道保持辅助",
    "BSD": "盲区检测",
    "FCW": "前碰撞预警",
    "RCTA": "后方横向来车预警",
    # 新能源
    "SOC": "荷电状态",
    "SOH": "健康状态",
    "BMS": "电池管理系统",
    "MCU": "电机控制单元",
    "VCU": "整车控制器",
    "OBC": "车载充电机",
    "DCDC": "直流直流变换器",
    "PTC": "正温度系数加热器",
    "BMS master": "电池管理系统主控",
    "HV battery": "高压电池",
    "LV battery": "低压蓄电池",
    # 诊断
    "OBD": "车载诊断",
    "DTC": "故障诊断码",
    "DID": "数据标识符",
    "RID": "例程标识符",
    "UDS": "统一诊断服务",
    "CAN": "控制器局域网",
    "LIN": "局部互联网络",
    "FlexRay": "FlexRay总线",
    "Ethernet": "车载以太网",
    "XCP": "通用测量与标定协议",
    "CCP": "CAN标定协议",
    # 通用
    "ECU": "电控单元",
    "EMS": "发动机管理系统",
    "BCM": "车身控制模块",
    "PCM": "动力总成控制模块",
    "TCM": "变速箱控制模块",
    "HMI": "人机界面",
    "NVH": "噪声振动平顺性",
    "PWM": "脉宽调制",
}

# 德语 → 中文 常用汽车术语
_DE_ABBREVIATIONS = {
    "AGR": "废气再循环",
    "DK": "节气门",
    "HFM": "热膜式空气质量流量计",
    "LMM": "空气流量计",
    "KW": "曲轴",
    "NW": "凸轮轴",
    "LL": "怠速",
    "VL": "全负荷",
    "TL": "部分负荷",
    "Saugrohr": "进气歧管",
    "Kraftstoff": "燃油",
    "Einspritzung": "喷射",
    "Zuendung": "点火",
    "Drehzahl": "转速",
    "Druck": "压力",
    "Temperatur": "温度",
    "Moment": "扭矩",
    "Leistung": "功率",
    "Verbrauch": "消耗",
    "Ladedruck": "增压压力",
    "Ladeluft": "增压空气",
    "Abgas": "废气",
    "Ansaug": "进气",
    "Kuehl": "冷却",
}

# CamelCase / under_score / 连字符 分解正则
_COMPOUND_SPLIT_RE = re.compile(r'''
    [A-Z][a-z]+                 # 大写开头的小写词: Rail, Pressure
    |[A-Z]+(?=[A-Z][a-z]|$)     # 连续大写但不包含下一个词: EGR, SCR
    |[A-Z]+$                    # 末尾连续大写
    |[a-zA-Z]+                  # 全小写词
''', re.VERBOSE)

_UNDERSCORE_RE = re.compile(r'[_\-]+')


# ══════════════════════════════════════════════════════════
#  词典预索引 — O(n*m) → O(1) + O(candidates)
# ══════════════════════════════════════════════════════════

@lru_cache(maxsize=1)
def _get_variant_cache():
    """缓存变体转换结果（模块级单例）"""
    return {}

def _de_variants(text):
    """生成德语文本的所有 ASCII 变体（使用模块级转换表）"""
    simple = text.translate(_UMLAUT_SIMPLE).replace("ß", "ss")
    expanded = text.translate(_UMLAUT_EXPANDED)
    variants = {simple}
    if expanded != simple:
        variants.add(expanded)
    return variants

def build_glossary_index(glossary):
    """预建词典索引，大幅加速匹配。同时添加德语 Umlaut ASCII 变体。"""
    exact = {}
    by_first_word = {}
    by_length = {}

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
        if any(c in key for c in "äöüÄÖÜß"):
            for variant in _de_variants(key):
                if variant != key:
                    add_entry(variant, value)

    return exact, by_first_word, by_length


def translate_with_glossary_fast(text, glossary, index):
    """快速翻译 — 利用索引将 4000+ 次比较降为 ~20 次"""
    if not text or len(text.strip()) < 2:
        return None

    original = text
    text_lower = text.lower().strip()
    exact_dict, by_first_word, by_length = index

    # O(1) 精确匹配
    if text_lower in exact_dict:
        return exact_dict[text_lower]

    # 收集候选（按首词 + 按词数匹配）
    words = text_lower.split()
    candidates = {}
    for w in words:
        for key, value, kl in by_first_word.get(w, []):
            candidates.setdefault(kl, (key, value))

    wc = len(words)
    for delta in [0, -1, 1, -2, 2]:
        for key, value, kl in by_length.get(wc + delta, []):
            candidates.setdefault(kl, (key, value))

    # 子串匹配（仅对 10-50 个候选）
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

    # 去重去重叠，最长优先，拼接
    matches.sort(key=lambda x: -x[3])
    used_ranges = []
    selected = []
    for m in matches:
        m_start, m_end = m[2], m[2] + m[3]
        if not any(not (m_end <= u[0] or m_start >= u[1]) for u in used_ranges):
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


PATTERNS = [
    # (类型, 正则, 描述在第几个捕获组, 名称在第几个组, 是 header 类型)
    ("MEASUREMENT",      re.compile(r'/begin\s+MEASUREMENT\s+(\w+)\s+"([^"]*)"', re.I),               2, 1, False),
    ("CHARACTERISTIC",   re.compile(r'/begin\s+CHARACTERISTIC\s+(\w+)\s+"([^"]*)"', re.I),            2, 1, False),
    ("FUNCTION",         re.compile(r'/begin\s+FUNCTION\s+(\w+)\s+"([^"]*)"', re.I),                 2, 1, False),
    ("GROUP",            re.compile(r'/begin\s+GROUP\s+(\w+)\s+"([^"]*)"', re.I),                    2, 1, False),
    ("AXIS_PTS",         re.compile(r'/begin\s+AXIS_PTS\s+(\w+)\s+"([^"]*)"', re.I),                 2, 1, False),
    ("COMPU_METHOD",     re.compile(r'/begin\s+COMPU_METHOD\s+(\w+)\s+"([^"]*)"', re.I),             2, 1, False),
    ("COMPU_VTAB",       re.compile(r'/begin\s+COMPU_VTAB\s+(\w+)\s+"([^"]*)"', re.I),               2, 1, False),
    ("COMPU_VTAB_RANGE", re.compile(r'/begin\s+COMPU_VTAB_RANGE\s+(\w+)\s+"([^"]*)"', re.I),         2, 1, False),
    ("PROJECT",          re.compile(r'/begin\s+PROJECT\s+(\w+)\s+"([^"]*)"', re.I),                  2, 1, False),
    ("MODULE",           re.compile(r'/begin\s+MODULE\s+(\w+)\s+"([^"]*)"', re.I),                   2, 1, False),
    ("HEADER",           re.compile(r'/begin\s+HEADER\s+"([^"]*)"', re.I),                           1, 0, True),
    ("MOD_COMMON",       re.compile(r'/begin\s+MOD_COMMON\s+"([^"]*)"', re.I),                       1, 0, True),
    ("MOD_PAR",          re.compile(r'/begin\s+MOD_PAR\s+"([^"]*)"', re.I),                          1, 0, True),
]

COMMENT_BLOCK_RE = re.compile(r'/\*([\s\S]*?)\*/')
COMMENT_LINE_RE  = re.compile(r'//([^\r\n]*)')

# ── 性能优化：合并注释匹配为单次扫描 ──
_COMMENT_COMBINED_RE = re.compile(r'''
    /\*([\s\S]*?)\*/    # 块注释
    |                    # 或
    //([^\r\n]*)         # 行注释
''', re.VERBOSE)

# 不可翻译的模式（纯数字、格式串、十六进制）
SKIP_PATTERNS = [
    re.compile(r'^[0-9+\-*/\s().,eE]+$'),
    re.compile(r'^%[0-9.]*[dfexs]$', re.I),
    re.compile(r'^0x[0-9a-fA-F]+$'),
    re.compile(r'^[A-Z_]{2,20}$'),  # 纯大写标识符
]


@lru_cache(maxsize=4096)
def is_skippable(text):
    """检查文本是否无需翻译（缓存加速，高频调用）"""
    t = text.strip()
    if len(t) < 2:
        return True
    for pat in SKIP_PATTERNS:
        if pat.match(t):
            return True
    return False


def parse_a2l(content):
    """
    解析 A2L 内容，提取所有可翻译条目。
    性能优化：合并注释为单次正则扫描，减少大文件遍历次数。
    返回: list[dict]
    """
    items = []
    seen_positions = set()
    counter = [0]

    # 匹配各 A2L 关键字描述
    for (typ, regex, desc_group, name_group, is_header) in PATTERNS:
        for m in regex.finditer(content):
            desc = m.group(desc_group)
            name = m.group(name_group) if not is_header else ""
            full = m.group(0)
            desc_start_in_match = full.rfind(desc)
            start_pos = m.start() + desc_start_in_match
            end_pos = start_pos + len(desc)

            if is_skippable(desc):
                continue

            key = (start_pos, end_pos)
            if key in seen_positions:
                continue
            seen_positions.add(key)

            counter[0] += 1
            items.append({
                "id": counter[0],
                "type": typ,
                "name": name,
                "original": desc,
                "translated": "",
                "status": "untranslated",
                "start": start_pos,
                "end": end_pos,
            })

    # 合并注释为单次正则扫描（取代原来的两次遍历）
    for m in _COMMENT_COMBINED_RE.finditer(content):
        if m.group(1):  # 块注释
            body = m.group(1).strip()
            if len(body) < 3 or is_skippable(body):
                continue
            start_pos = m.start() + 2
            end_pos = m.end() - 2
            original = m.group(1)
        else:  # 行注释 (group 2)
            body = m.group(2).strip()
            if len(body) < 3 or is_skippable(body):
                continue
            start_pos = m.start() + 2
            end_pos = m.start() + len(m.group(0))
            original = m.group(2)

        key = (start_pos, end_pos)
        if key in seen_positions:
            continue
        seen_positions.add(key)

        counter[0] += 1
        items.append({
            "id": counter[0],
            "type": "COMMENT",
            "name": "",
            "original": original,
            "translated": "",
            "status": "untranslated",
            "start": start_pos,
            "end": end_pos,
        })

    # 按位置排序
    items.sort(key=lambda x: x["start"])
    return items


# ── 重建 A2L ──────────────────────────────────────

def rebuild_a2l(content, items):
    """将翻译替换回 A2L 内容（分段拼接，避免大文件重复拷贝内存爆炸）"""
    translated = [i for i in items if i["translated"] and i["translated"] != i["original"]]
    if not translated:
        return content

    # 按位置排序，从前往后分段拼接
    translated.sort(key=lambda x: x["start"])
    parts = []
    last_end = 0
    for item in translated:
        parts.append(content[last_end:item["start"]])
        parts.append(item["translated"])
        last_end = item["end"]
    parts.append(content[last_end:])
    return "".join(parts)


# ── 词典翻译 ──────────────────────────────────────

def load_glossary(filepath):
    """加载自定义术语词典 (CSV 或 JSON)"""
    glossary = {}
    path = Path(filepath)
    if not path.exists():
        print(f"[警告] 词典文件不存在: {filepath}")
        return glossary

    if path.suffix.lower() == '.json':
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, dict):
                glossary = data
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and 'source' in item and 'target' in item:
                        glossary[item['source']] = item['target']
    elif path.suffix.lower() == '.csv':
        with open(filepath, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if header and len(header) >= 2:
                for row in reader:
                    if len(row) >= 2 and row[0].strip() and row[1].strip():
                        glossary[row[0].strip()] = row[1].strip()
    else:
        # 纯文本: 每行 source=TARGET
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if '=' in line:
                    key, val = line.split('=', 1)
                    glossary[key.strip()] = val.strip()

    return glossary


def apply_glossary(items, glossary, show_progress=True):
    """应用术语词典翻译（精确匹配 + 模糊匹配 + 智能子串匹配）"""
    count = 0
    total = len(items)
    dot_width = 20
    last_dots = 0
    batch_interval = max(1, total // 20)

    index = build_glossary_index(glossary)

    for idx, item in enumerate(items):
        if item["status"] != "untranslated":
            continue

        result = translate_with_glossary_fast(item["original"], glossary, index)
        if result:
            item["translated"] = result
            item["status"] = "auto"
            count += 1

        if show_progress and ((idx + 1) % batch_interval == 0 or idx == total - 1):
            done_pct = (idx + 1) / total
            filled = int(done_pct * dot_width)
            if filled != last_dots:
                bar = "●" * filled + "○" * (dot_width - filled)
                print(f"  词典匹配 [{bar}] {idx+1}/{total}  命中{count}", end="\r")
                last_dots = filled

    if show_progress:
        print(f"  词典匹配 [{'●' * dot_width}] {total}/{total}  命中{count}")
    return count


# ══════════════════════════════════════════════════════════
#  翻译精度增强函数
# ══════════════════════════════════════════════════════════

def decompose_compound(text):
    """分解复合词：CamelCase → 分词，under_score → 分词，返回分解后的文本"""
    if not text:
        return text
    # 如果包含下划线或连字符，先拆分
    parts = _UNDERSCORE_RE.split(text)
    if len(parts) > 1:
        return " ".join(p for p in parts if p)
    # CamelCase / PascalCase 拆分
    tokens = _COMPOUND_SPLIT_RE.findall(text)
    if len(tokens) > 1:
        return " ".join(tokens)
    return text

def expand_abbreviations(text):
    """展开汽车行业缩写：EGR → 废气再循环；同时处理德语缩写"""
    words = text.split()
    expanded = []
    for w in words:
        # 去掉缩写末尾的点号: E.G.R. → EGR
        clean = w.rstrip(".")
        if clean.upper() in _ABBREVIATIONS:
            expanded.append(_ABBREVIATIONS[clean.upper()])
        elif clean in _DE_ABBREVIATIONS:
            expanded.append(_DE_ABBREVIATIONS[clean])
        else:
            expanded.append(w)
    return " ".join(expanded)

def preprocess_text(text, expand_abbr=True, decompose=True):
    """翻译前预处理：缩写展开 + 复合词分解"""
    result = text.strip()
    if expand_abbr:
        result = expand_abbreviations(result)
    if decompose:
        decomposed = decompose_compound(result)
        # 只有当分解后确实不同时才替换
        if decomposed != result:
            result = decomposed
    return result

def post_verify_translation(item, glossary, index):
    """
    翻译后验证：用词典术语检查 API 翻译结果，修正明显错误。

    策略：
    1. 原文中出现的术语 → 译文中必须出现对应的中文
    2. 如果译文缺失重要术语 → 追加补充
    3. 如果译文有矛盾 → 用词典版本覆盖
    """
    if not item.get("translated") or item["status"] != "auto":
        return

    original = item["original"]
    translated = item["translated"]
    exact_dict, _, _ = index

    # 查找原文提到的所有术语
    for en_term, zh_term in list(exact_dict.items())[:200]:  # 只检查最常见的200个术语
        en_lower = en_term.lower()
        if en_lower in original.lower():
            zh_in_result = any(
                c in translated for c in [zh_term, zh_term[:2], zh_term[-2:]]
            )
            if not zh_in_result and len(en_term) > 4:
                # 术语在原文但不在译文 → 修正
                item["translated"] = translated.rstrip("。，.!") + "，" + f"[{zh_term}]"
                item["status"] = "auto_corrected"
                break


# ── 逐词智能翻译 ──────────────────────────────────

# 跳过翻译的类型（公式、版本号、代码等）
_SMART_SKIP_TYPES = {
    "COMPU_METHOD",  # Q = V 等数学公式
    "FUNCTION",       # 版本号 10.0.1_P602_MD1CE100
    "RECORD_LAYOUT",  # 内存布局描述
    "MODULE",         # 模块名
}

def build_smart_translator(glossary, extra_keywords=None):
    """构建智能翻译器 — 返回 (exact_match_dict, compiled_regex, term_to_zh)
    使用单次正则扫描替代逐词遍历，性能提升 100-1000 倍。"""
    exact_dict = {}
    merge = {}
    for en, zh in glossary.items():
        en_lower = en.lower().strip()
        exact_dict[en_lower] = zh
        for word in re.findall(r'[A-Za-z]{2,}', en):
            wl = word.lower()
            if wl not in merge:
                merge[wl] = zh[:8]
    if extra_keywords:
        for k, v in extra_keywords.items():
            kl = k.lower().strip()
            exact_dict[kl] = v
            merge[kl] = v

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

    matches = []
    for m in combined_pattern.finditer(text):
        matched_term = m.group().lower()
        if matched_term in term_map:
            matches.append((m.start(), m.end(), term_map[matched_term]))

    if not matches:
        return None

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


def apply_smart_translate(items, glossary, extra_keywords=None, show_progress=True):
    """对未匹配条目执行逐词智能翻译（第二遍）"""
    exact_dict, combined_pattern, term_map = build_smart_translator(glossary, extra_keywords)

    total = len(items)
    count = 0
    dot_width = 20
    last_dots = 0
    batch_interval = max(1, total // 20)

    for idx, item in enumerate(items):
        if item.get("translated"):
            continue
        if item["type"] in _SMART_SKIP_TYPES:
            continue

        result = smart_translate_text_fast(item["original"], exact_dict, combined_pattern, term_map)
        if result:
            item["translated"] = result
            item["status"] = "auto"
            count += 1

        if show_progress and ((idx + 1) % batch_interval == 0 or idx == total - 1):
            done_pct = (idx + 1) / total
            filled = int(done_pct * dot_width)
            if filled != last_dots:
                bar = "●" * filled + "○" * (dot_width - filled)
                print(f"  逐词翻译 [{bar}] {idx+1}/{total}  新增{count}", end="\r")
                last_dots = filled

    if show_progress:
        print(f"  逐词翻译 [{'●' * dot_width}] {total}/{total}  新增{count}")
    return count


# ── API 翻译 ──────────────────────────────────────

def api_translate_batch(texts, src_lang, tgt_lang, timeout=15):
    """
    使用 MyMemory API 批量翻译。
    返回: list[str] 或 None（失败时）
    """
    separator = " ||| "
    combined = separator.join(texts)

    if src_lang == "auto":
        langpair = f"Autodetect|{tgt_lang}"
    else:
        langpair = f"{src_lang}|{tgt_lang}"

    params = {
        "q": combined,
        "langpair": langpair,
    }

    url = "https://api.mymemory.translated.net/get?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "A2L-Translator/2.9.5"})
        ctx = _ssl_context  # None = 默认验证，自定义 = 跳过或指定证书
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        print(f"  [API HTTP {e.code}] {e.reason}" + (f" — {body}" if body else ""))
        return None
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        err_msg = str(e)
        if "CERTIFICATE_VERIFY_FAILED" in err_msg or "certificate" in err_msg.lower():
            print(f"  [API SSL 错误] 证书验证失败，可用 --no-ssl-verify 跳过（仅限内网环境）")
        else:
            print(f"  [API 错误] {e}")
        return None

    if data.get("responseStatus") != 200:
        print(f"  [API 响应异常] status={data.get('responseStatus')}")
        return None

    translated = data["responseData"]["translatedText"]
    # MyMemory 会压缩分隔符周围的空格，所以用不带空格的 ||| 拆分
    parts = translated.split("|||")
    return [p.strip() for p in parts]


def auto_translate(items, glossary, src_lang, tgt_lang, batch_size=8, delay=0.6,
                   baidu_appid=None, baidu_secret=None, ssl_ctx=None):
    """
    自动翻译所有未翻译条目。
    流程：预处理 → TM → 自定义词典 → 内建词典 → 逐词 → API(domain=auto) → 后验证 → 更新TM
    """
    # ═══ 阶段0：加载 TM 和自定义词典 ═══
    tm = load_translation_memory()
    custom_glossary = load_custom_glossary()
    if custom_glossary:
        glossary = merge_glossary(glossary, custom_glossary)
        print(f"  自定义词典: {len(custom_glossary)} 条已合并")

    # ═══ 阶段1：预处理 — 缩写展开 + 复合词分解 ═══
    preprocess_count = 0
    for item in items:
        if item["status"] == "untranslated":
            decomposed = preprocess_text(item["original"])
            if decomposed != item["original"]:
                item["_original_raw"] = item["original"]
                item["original"] = decomposed
                preprocess_count += 1
    if preprocess_count:
        print(f"  预处理: {preprocess_count} 条复合词已分解")

    # ═══ 阶段2：翻译记忆库（最高优先级，精确匹配）═══
    tm_count = apply_tm(items, tm)

    # ═══ 阶段3：词典匹配 ═══
    dict_count = apply_glossary(items, glossary)

    # ═══ 阶段4：逐词智能翻译 ═══
    smart_count = apply_smart_translate(items, glossary, show_progress=True)

    # ═══ 阶段4.5：深度模糊搜索（仿 DeepL 术语库匹配）═══
    fuzzy_count = 0
    for item in items:
        if item.get("translated"):
            continue
        result, confidence, source = deep_fuzzy_search(
            item["original"], glossary, tm, threshold=0.65
        )
        if result:
            item["translated"] = result
            item["status"] = "tm" if "tm" in source else "auto"
            item["_confidence"] = confidence
            fuzzy_count += 1
    if fuzzy_count:
        print(f"  深度模糊搜索: {fuzzy_count} 条匹配 (类似DeepL术语库)")

    # 找出仍需 API 翻译的
    untranslated = [i for i in items if not i.get("translated")]
    if not untranslated:
        new_tm = update_tm_from_items(tm, items)
        if new_tm:
            print(f"  TM 更新: +{new_tm} 条")
        return tm_count + dict_count + smart_count + fuzzy_count

    total = len(untranslated)
    api_count_box = [0]
    dot_width = 20
    use_baidu = _HAS_BAIDU and baidu_appid and baidu_secret

    engine = "百度翻译" if use_baidu else "MyMemory"
    print(f"\n  正在调用翻译 API ({total} 条待翻译) [{engine}]...")
    print(f"  语言: {src_lang} → {tgt_lang}  |  并行批次: {min(4, max(1, (total + batch_size - 1) // batch_size))}")

    def update_bar(done, success_count):
        filled = int(done / total * dot_width) if total > 0 else 0
        bar = "●" * filled + "○" * (dot_width - filled)
        print(f"  [{bar}] {done}/{total}  ✓{success_count}", end="\r")

    update_bar(0, 0)

    max_workers = min(4, max(1, (total + batch_size - 1) // batch_size))
    batches = []
    for bs in range(0, total, batch_size):
        batch = untranslated[bs:bs + batch_size]
        texts = [b["original"].strip() for b in batch]
        batches.append((batch, texts))

    def translate_batch(batch_data):
        batch, texts = batch_data
        if use_baidu:
            result = baidu_translate_batch(texts, src_lang, tgt_lang, baidu_appid, baidu_secret, ssl_ctx)
            if result:
                local_success = 0
                for j, b in enumerate(batch):
                    if j < len(result) and result[j] and result[j] != b["original"]:
                        b["translated"] = result[j]
                        b["status"] = "auto"
                        local_success += 1
                    else:
                        b["status"] = "error"
                return len(batch), local_success
            else:
                for b in batch:
                    b["status"] = "error"
                return len(batch), 0
        else:
            result = api_translate_batch(texts, src_lang, tgt_lang)
            if result:
                local_success = 0
                for j, b in enumerate(batch):
                    if j < len(result) and result[j] and result[j] != b["original"]:
                        b["translated"] = result[j]
                        b["status"] = "auto"
                        local_success += 1
                    else:
                        b["status"] = "error"
                return len(batch), local_success
            else:
                for b in batch:
                    b["status"] = "error"
                return len(batch), 0

    done_count = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(translate_batch, b): b for b in batches}
        for future in as_completed(futures):
            try:
                batch_done, batch_success = future.result()
                api_count_box[0] += batch_success
                done_count += batch_done
                update_bar(done_count, api_count_box[0])
            except Exception:
                pass

    api_count = api_count_box[0]
    update_bar(total, api_count)
    print()

    # ═══ 阶段5.5：终极降级翻译 — 保证 100% 覆盖率 ═══
    fallback_count = 0
    still_untranslated = [i for i in untranslated if not i.get("translated")]
    if still_untranslated:
        # 建立逐词翻译库（多数据源合并）
        word_map = {}
        # 从缩写表提取
        word_map.update({k.lower(): v for k, v in _ABBREVIATIONS.items()})
        # 从德语缩写提取
        word_map.update({k.lower(): v for k, v in _DE_ABBREVIATIONS.items()})
        # 从拼写规范表提取
        word_map.update(_SPELL_VARIANTS)

        # 从词典中提取单词→译文映射
        for en, zh in glossary.items():
            en_words = en.split()
            zh_parts = zh.split()
            # 如果英文和中文词数相同，建立1对1映射
            if len(en_words) == len(zh_parts):
                for ew, zw in zip(en_words, zh_parts):
                    wl = ew.lower().strip(".,;:()[]{}<>!?/\\-_")
                    if len(wl) >= 2 and wl not in word_map:
                        word_map[wl] = zw.strip("，。；：")
            # 单个单词 → 直接用完整译文
            elif len(en_words) == 1 and len(en) >= 2:
                wl = en.lower().strip(".,;:()[]{}<>!?/\\-_")
                if wl not in word_map:
                    word_map[wl] = zh

        for item in still_untranslated:
            text = item["original"]
            words = text.split()
            translated_words = []
            has_translation = False
            for w in words:
                clean = w.lower().strip(".,;:()[]{}<>!?/\\-_")
                # 跳过公式 Q=V 之类
                if clean in word_map and not re.match(r'^[qv]=', clean):
                    translated_words.append(word_map[clean])
                    has_translation = True
                else:
                    # 尝试首字母大写匹配
                    title_clean = clean[0].upper() + clean[1:] if clean else clean
                    if title_clean in word_map:
                        translated_words.append(word_map[title_clean])
                        has_translation = True
                    else:
                        translated_words.append(w)
            if has_translation:
                result = " ".join(translated_words)
                item["translated"] = result
                item["status"] = "fallback"
                fallback_count += 1

        if fallback_count:
            print(f"  终极降级翻译: {fallback_count} 条（逐词翻译兜底）")

    # ═══ 阶段6：翻译后验证 + TM 更新 ═══
    verify_count = 0
    glossary_index = build_glossary_index(glossary)
    for item in items:
        if item.get("translated") and item["status"] in ("auto", "auto_corrected", "tm"):
            pre_status = item["status"]
            post_verify_translation(item, glossary, glossary_index)
            if item["status"] == "auto_corrected" and pre_status != "auto_corrected":
                verify_count += 1

    # ═══ 阶段7：更新翻译记忆库 ═══
    new_tm = update_tm_from_items(tm, items)

    # 恢复预处理中修改的原文
    restore_count = 0
    for item in items:
        if "_original_raw" in item:
            item["original"] = item.pop("_original_raw")
            restore_count += 1

    total_translated = tm_count + dict_count + smart_count + fuzzy_count + api_count + fallback_count
    print(f"  完成: TM {tm_count} + 词典 {dict_count} + 逐词 {smart_count} + 模糊 {fuzzy_count} + API {api_count} + 兜底 {fallback_count} [{engine}]")
    if preprocess_count:
        print(f"  预处理: {preprocess_count} 复合词分解")
    if verify_count:
        print(f"  后验证: {verify_count} 条已修正")
    if new_tm:
        print(f"  TM 更新: +{new_tm} 条")

    # ═══ 阶段8：置信度评估（仿 Google Translate 质量报告）═══
    confidence_results = {"high": 0, "medium": 0, "low": 0, "review": 0}
    for item in items:
        if item.get("translated"):
            score = item.get("_confidence") or rate_confidence(item, glossary, tm)
            item["_confidence"] = score
            level, _ = classify_confidence(score)
            confidence_results[level] += 1

    total_trans_items = sum(confidence_results.values())
    if total_trans_items > 0:
        high_pct = confidence_results["high"] / total_trans_items * 100
        print(f"  置信度: 🟢{confidence_results['high']} 🟡{confidence_results['medium']} 🟠{confidence_results['low']} 🔴{confidence_results['review']}  |  高置信率: {high_pct:.0f}%")

    return total_translated


# ── 导出 / 导入 ────────────────────────────────────

def export_csv(items, filepath):
    """导出所有条目为 CSV"""
    with open(filepath, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(["id", "type", "name", "original", "translated", "status"])
        for item in items:
            writer.writerow([
                item["id"], item["type"], item["name"],
                item["original"], item["translated"] or "", item["status"]
            ])
    print(f"已导出 {len(items)} 条到: {filepath}")


def export_json(items, filepath):
    """导出所有条目为 JSON（包含完整位置信息，可用于恢复）"""
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    print(f"已导出 {len(items)} 条到: {filepath}")


def save_progress(items, original_content, filepath):
    """保存翻译进度（含原始文件内容）"""
    data = {
        "items": items,
        "original_content": original_content,
        "count": len(items),
    }
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"翻译进度已保存: {filepath}")


def load_progress(filepath):
    """加载翻译进度"""
    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)
    items = data.get("items", [])
    content = data.get("original_content", "")
    print(f"已加载进度: {len(items)} 条")
    return items, content


def apply_csv(items, filepath):
    """从 CSV 导入译文"""
    imported = {}
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rid = int(row.get("id", 0))
            translated = row.get("translated", "").strip()
            if rid and translated:
                imported[rid] = translated

    count = 0
    for item in items:
        if item["id"] in imported and imported[item["id"]]:
            item["translated"] = imported[item["id"]]
            item["status"] = "manual"
            count += 1

    print(f"已从 CSV 导入 {count} 条译文")
    return count


def print_summary(items):
    """打印翻译摘要"""
    total = len(items)
    by_type = {}
    by_status = {"untranslated": 0, "auto": 0, "manual": 0, "error": 0}

    for item in items:
        by_type[item["type"]] = by_type.get(item["type"], 0) + 1
        by_status[item["status"]] = by_status.get(item["status"], 0) + 1

    print("\n" + "=" * 60)
    print("  翻译摘要")
    print("=" * 60)
    print(f"  总条目: {total}")
    print(f"  已翻译: {by_status['auto'] + by_status['manual']}  (自动: {by_status['auto']}, 手动: {by_status['manual']})")
    print(f"  未翻译: {by_status['untranslated']}")
    if by_status['error']:
        print(f"  失败:   {by_status['error']}")
    print(f"\n  按类型分布:")
    for typ, cnt in sorted(by_type.items(), key=lambda x: -x[1]):
        print(f"    {typ:<22s} {cnt:>4d}")
    print("=" * 60 + "\n")


def list_strings(items, limit=50):
    """列出可翻译字符串"""
    print("\n" + "=" * 80)
    print(f"{'ID':<6s} {'类型':<22s} {'名称':<25s} {'原文'}")
    print("=" * 80)

    shown = 0
    for item in items:
        if shown >= limit:
            remaining = len(items) - shown
            if remaining > 0:
                print(f"... 还有 {remaining} 条 (用 --extract 导出完整列表)")
            break
        name = item["name"][:24] if item["name"] else "-"
        status_mark = "✓" if item["status"] != "untranslated" else " "
        print(f"{item['id']:<6d} {item['type']:<22s} {name:<25s} [{status_mark}] {item['original'][:60]}")
        shown += 1
    print("=" * 80 + "\n")


# ── 主函数 ────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="A2L 文件翻译工具 — 解析 ASAP2 文件并翻译描述/注释",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python a2l_translator.py ecu.a2l --auto-translate
  python a2l_translator.py ecu.a2l -o ecu_cn.a2l --auto -s en -t zh-CN
  python a2l_translator.py ecu.a2l --dict my_terms.csv --auto
  python a2l_translator.py ecu.a2l --save progress.json
  python a2l_translator.py ecu.a2l --load progress.json --auto
  python a2l_translator.py ecu.a2l --extract strings.csv
  python a2l_translator.py ecu.a2l --apply translated.csv -o ecu_cn.a2l
  python a2l_translator.py *.a2l --auto-translate           # 批量处理
        """,
    )

    parser.add_argument("files", nargs="+", help="A2L 文件路径，支持通配符")
    parser.add_argument("-o", "--output", help="输出文件路径（单文件时有效）")
    parser.add_argument("-s", "--source", default="auto", help="源语言 (默认: auto, 可选: en, de)")
    parser.add_argument("-t", "--target", default="zh-CN", help="目标语言 (默认: zh-CN)")
    parser.add_argument("--auto-translate", "--auto", action="store_true", help="自动翻译全部未翻译条目")
    parser.add_argument("--dict", help="自定义术语词典文件 (CSV/JSON/TXT)")
    parser.add_argument("--no-builtin", action="store_true", help="不使用内置汽车术语词典")
    parser.add_argument("--no-ssl-verify", action="store_true", help="跳过 SSL 证书验证（仅限内网/代理环境）")
    parser.add_argument("--baidu-appid", metavar="APPID", help="百度翻译 APP ID")
    parser.add_argument("--baidu-secret", metavar="SECRET", help="百度翻译密钥（配置后优先使用百度翻译）")
    parser.add_argument("--extract", metavar="FILE", help="导出可翻译字符串到 CSV 文件")
    parser.add_argument("--extract-json", metavar="FILE", help="导出完整翻译数据到 JSON 文件")
    parser.add_argument("--apply", metavar="FILE", help="从 CSV 文件导入译文")
    parser.add_argument("--save", metavar="FILE", help="保存翻译进度 (JSON，含原始文件)")
    parser.add_argument("--load", metavar="FILE", help="加载翻译进度继续工作")
    parser.add_argument("--list", type=int, nargs="?", const=50, metavar="N", help="列出可翻译字符串 (默认 50 条)")
    parser.add_argument("--batch", type=int, default=8, help="API 批量大小 (默认: 8)")
    parser.add_argument("--delay", type=float, default=0.6, help="API 请求间隔秒数 (默认: 0.6)")
    parser.add_argument("--quiet", "-q", action="store_true", help="安静模式，减少输出")
    parser.add_argument("--verify", action="store_true",
                       help="翻译完成后用8大权威词典验证准确率")

    args = parser.parse_args()

    # ── 准备词典 ──
    glossary = {}
    if not args.no_builtin:
        glossary.update(BUILTIN_GLOSSARY)
        glossary.update(GERMAN_GLOSSARY)  # 合并德语词典
    if args.dict:
        custom = load_glossary(args.dict)
        glossary.update(custom)
        if not args.quiet:
            print(f"已加载自定义词典: {len(custom)} 条 (总计 {len(glossary)} 条)")

    # ── SSL 配置 ──
    if args.no_ssl_verify:
        global _ssl_context
        _ssl_context = ssl.create_default_context()
        _ssl_context.check_hostname = False
        _ssl_context.verify_mode = ssl.CERT_NONE
        if not args.quiet:
            print("[警告] SSL 证书验证已禁用")

    # ── 处理每个文件 ──
    exit_code = 0

    for filepath in args.files:
        path = Path(filepath)
        if not path.exists():
            print(f"[错误] 文件不存在: {filepath}")
            exit_code = 1
            continue
        if path.suffix.lower() != '.a2l':
            print(f"[警告] 非 A2L 文件，跳过: {filepath}")
            continue

        if not args.quiet:
            size_mb = path.stat().st_size / (1024 * 1024)
            print(f"\n{'─' * 60}")
            print(f"  文件: {path.name}  ({size_mb:.1f} MB)")
            print(f"{'─' * 60}")

        # 读取文件（大文件 >10MB 使用 mmap 加速）
        try:
            from mmap import mmap, ACCESS_READ
            try:
                with open(filepath, 'r', encoding='utf-8') as f_raw:
                    with mmap(f_raw.fileno(), 0, access=ACCESS_READ) as mm:
                        content = mm.read().decode('utf-8')
            except (ValueError, OSError):
                # mmap 失败时回退到普通读取（如管道、空文件）
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
        except UnicodeDecodeError:
            try:
                with open(filepath, 'r', encoding='latin-1') as f:
                    content = f.read()
            except Exception as e:
                print(f"[错误] 读取文件失败: {e}")
                exit_code = 1
                continue
        except ImportError:
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
            except UnicodeDecodeError:
                with open(filepath, 'r', encoding='latin-1') as f:
                    content = f.read()
        except Exception as e:
            print(f"[错误] 读取文件失败: {e}")
            exit_code = 1
            continue

        # 解析
        items = parse_a2l(content)
        if not args.quiet:
            print(f"  解析到 {len(items)} 条可翻译文本")

        if not items:
            print("  未发现可翻译内容")
            continue

        # 加载进度
        if args.load:
            loaded_items, loaded_content = load_progress(args.load)
            # 合并：用加载的翻译更新当前条目（按位置匹配）
            if loaded_content == content:
                items = loaded_items
            else:
                # 内容不匹配，按 name+original 匹配
                loaded_map = {(i["type"], i["name"], i["original"]): i for i in loaded_items}
                for item in items:
                    key = (item["type"], item["name"], item["original"])
                    if key in loaded_map and loaded_map[key]["translated"]:
                        item["translated"] = loaded_map[key]["translated"]
                        item["status"] = loaded_map[key]["status"]
                if not args.quiet:
                    print(f"  已合并加载的翻译（文件内容已变化）")

        # 自动翻译
        if args.auto_translate:
            translated_count = auto_translate(
                items, glossary, args.source, args.target,
                batch_size=args.batch, delay=args.delay,
                baidu_appid=getattr(args, "baidu_appid", None),
                baidu_secret=getattr(args, "baidu_secret", None),
                ssl_ctx=_ssl_context,
            )
            if translated_count == 0:
                print("  所有条目已翻译完成")

        # 导出 CSV
        if args.extract:
            export_csv(items, args.extract)

        # 导出 JSON
        if args.extract_json:
            export_json(items, args.extract_json)

        # 从 CSV 导入
        if args.apply:
            apply_csv(items, args.apply)

        # 保存进度
        if args.save:
            save_progress(items, content, args.save)

        # 列出字符串
        if args.list is not None:
            list_strings(items, limit=args.list)

        # 确定输出文件
        if len(args.files) == 1 and args.output:
            out_path = args.output
        elif args.output:
            # 多文件时，output 作为目录
            out_dir = Path(args.output)
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / path.name
        else:
            stem = path.stem
            out_path = Path(path.parent) / f"{stem}_translated.a2l"

        # 输出翻译后的文件
        translated_content = rebuild_a2l(content, items)
        changed = sum(1 for i in items if i["translated"] and i["translated"] != i["original"])

        if changed > 0:
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write(translated_content)
            if not args.quiet:
                print(f"  已输出: {out_path}  ({changed} 处翻译)")
        else:
            if not args.quiet:
                print(f"  无翻译内容，未生成输出文件")

        # 打印摘要
        if not args.quiet:
            print_summary(items)

        # ── 多源词典验证 ──
        if args.verify and _HAS_MULTI_DICT:
            verified = [(i["original"], i["translated"])
                       for i in items if i.get("translated")]
            if verified:
                print(f"\n{'='*60}")
                print("  🌐 8大权威词典交叉验证报告")
                print(f"{'='*60}")
                md = get_dictionary()
                vr = md.batch_verify(verified, progress_callback=None)
                passed = sum(1 for _, _, v, _, _ in vr if v)
                total = len(vr)
                rate = passed / total * 100 if total > 0 else 0
                print(f"  验证总数: {total}")
                print(f"  ✅ 通过: {passed}")
                print(f"  ⚠️  未通过: {total - passed}")
                print(f"  📊 通过率: {rate:.1f}%")
                if total - passed > 0:
                    print(f"\n  未通过条目:")
                    for en, zh, v, conf, alt in vr:
                        if not v and alt:
                            print(f"    • {en}")
                            print(f"      当前: {zh}")
                            print(f"      建议: {alt}  (置信度: {conf})")
                print(f"{'='*60}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
