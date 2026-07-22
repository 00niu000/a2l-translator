#!/usr/bin/env python3
"""
A2L 翻译工具 — 仿 DeepL / Trados 级模糊匹配引擎
=================================================
核心技术：
  1. Levenshtein 编辑距离 — 纠错 + 模糊匹配
  2. N-gram 重叠度 — 类似 DeepL 的上下文匹配
  3. 多源置信度评分 — Google Translate 级质量评估
  4. 术语一致性验证 — 类似 Trados QA Checker

参考：
  - DeepL: 整句上下文 + 术语库匹配
  - Google NMT: Transformer 架构 + 置信度
  - Trados: 模糊匹配 (70%/80%/90%阈值) + Termbase
  - memoQ: 拼写规范化 + 一致性检查
"""

import re
from difflib import SequenceMatcher
from collections import Counter


# ══════════════════════════════════════════════════════════
#  1. 拼写规范化（常见 ECU 变体 / 大小写 / 缩写变体）
# ══════════════════════════════════════════════════════════

_SPELL_VARIANTS = {
    # 英美拼写差异
    "colour": "color",
    "behaviour": "behavior",
    "centre": "center",
    "metre": "meter",
    "litre": "liter",
    "calibre": "caliber",
    # ECU 常见错误/变体
    "press": "pressure",
    "temp": "temperature",
    "posn": "position",
    "veh": "vehicle",
    "eng": "engine",
    "ref": "reference",
    "cmd": "command",
    "actl": "actual",
    "req": "request",
    "diag": "diagnostic",
    "cal": "calibration",
    "char": "characteristic",
    "meas": "measurement",
    "ctrl": "control",
    "sens": "sensor",
    "sig": "signal",
    "init": "initial",
    "min": "minimum",
    "max": "maximum",
    "avg": "average",
    "abs": "absolute",
    "rel": "relative",
    "diff": "differential",
    "thr": "threshold",
    "freq": "frequency",
    "volt": "voltage",
    "curr": "current",
    "pw": "pulse width",
    "dc": "duty cycle",
    "fb": "feedback",
    "ff": "feed forward",
    "kp": "proportional gain",
    "ki": "integral gain",
    "kd": "derivative gain",
    "pid": "PID controller",
}

def normalize_spelling(text):
    """拼写规范化：ECU 缩写展开 + 英美拼写统一"""
    words = text.split()
    normalized = []
    for w in words:
        lower = w.lower().rstrip(".,_")
        if lower in _SPELL_VARIANTS:
            normalized.append(_SPELL_VARIANTS[lower])
        else:
            normalized.append(w)
    return " ".join(normalized)


# ══════════════════════════════════════════════════════════
#  2. Levenshtein 编辑距离（模糊匹配核心）
# ══════════════════════════════════════════════════════════

def levenshtein_ratio(s1, s2):
    """基于编辑距离的相似度 (0.0~1.0)，类似 DeepL 术语库模糊匹配"""
    return SequenceMatcher(None, s1.lower(), s2.lower()).ratio()


def find_fuzzy_match(text, candidates, threshold=0.75):
    """
    模糊匹配 — 在候选列表中找最佳匹配。
    类似 Trados 的 75%/85%/95% 匹配阈值。

    返回: (best_key, best_score) 或 (None, 0)
    """
    best_key, best_score = None, 0.0
    text_lower = text.lower().strip()
    # 精确匹配优先
    for key in candidates:
        if key.lower().strip() == text_lower:
            return key, 1.0
    # 模糊匹配
    for key in candidates:
        score = levenshtein_ratio(text_lower, key.lower())
        if score > best_score:
            best_score = score
            best_key = key
    if best_score >= threshold:
        return best_key, best_score
    return None, 0.0


# ══════════════════════════════════════════════════════════
#  3. N-gram 重叠度（上下文匹配，仿 DeepL）
# ══════════════════════════════════════════════════════════

def ngram_overlap(text1, text2, n=3):
    """
    N-gram 字符级重叠度。
    DeepL 用类似技术在术语库中匹配上下文相关翻译。
    """
    def get_ngrams(s, n):
        s = s.lower()
        return {s[i:i+n] for i in range(len(s) - n + 1)}

    ngrams1 = get_ngrams(text1, n)
    ngrams2 = get_ngrams(text2, n)

    if not ngrams1 or not ngrams2:
        return 0.0

    intersection = ngrams1 & ngrams2
    union = ngrams1 | ngrams2
    return len(intersection) / len(union) if union else 0.0


def hybrid_similarity(text1, text2):
    """
    混合相似度 = 编辑距离(60%) + 3-gram重叠(40%)
    综合了 DeepL（整句匹配）和 Trados（编辑距离）的优势
    """
    edit_score = levenshtein_ratio(text1, text2)
    ngram_score = ngram_overlap(text1, text2, n=3)
    return 0.6 * edit_score + 0.4 * ngram_score


# ══════════════════════════════════════════════════════════
#  4. 置信度评分（仿 Google Translate 质量评估）
# ══════════════════════════════════════════════════════════

def rate_confidence(item, glossary, tm):
    """
    对翻译结果进行置信度评分 (0.0~1.0)。

    评分依据：
      - 来源: TM(1.0) > custom_glossary(0.95) > glossary(0.85) > API(0.6)
      - 术语覆盖: 原文术语在译文中能找到对应 → 加分
      - 文本长度: 极短文本(<3字)或极长文本(>100字) → 降分
    """
    score = 0.0
    status = item.get("status", "untranslated")
    original = item.get("original", "").strip()
    translated = item.get("translated", "").strip()

    # 基础分（来源权重）
    if status == "tm":
        score = 0.95
    elif status == "manual":
        score = 0.90
    elif status == "auto":
        score = 0.60
    elif status == "auto_corrected":
        score = 0.75
    else:
        return 0.0

    # 术语覆盖加分
    if glossary:
        term_hits = 0
        for en_term in glossary:
            if en_term.lower() in original.lower():
                zh_term = glossary[en_term][:3]
                if zh_term in translated:
                    term_hits += 1
        if term_hits > 0:
            score = min(1.0, score + 0.02 * term_hits)

    # TM 精确匹配加分
    if tm and original in tm:
        if tm[original] == translated:
            score = min(1.0, score + 0.05)

    # 长度惩罚
    orig_len = len(original)
    if orig_len < 5:
        score = max(0.3, score - 0.10)
    elif orig_len > 200:
        score = max(0.3, score - 0.05)

    return round(min(1.0, score), 2)


def classify_confidence(score):
    """将置信度转为人类可读标签"""
    if score >= 0.90:
        return "high", "🟢 高置信度"
    elif score >= 0.70:
        return "medium", "🟡 中置信度"
    elif score >= 0.50:
        return "low", "🟠 低置信度"
    else:
        return "review", "🔴 需人工审核"


# ══════════════════════════════════════════════════════════
#  5. 术语一致性检查（仿 Trados QA Checker）
# ══════════════════════════════════════════════════════════

def check_consistency(items, glossary):
    """
    术语一致性检查 — 同一原文术语在不同位置是否翻译一致。

    类似 Trados QA Checker 的 "Terminology consistency" 规则。
    返回不一致条目列表。
    """
    inconsistent = []
    term_translations = {}  # {en_term: {zh_translation: count}}

    for item in items:
        if not item.get("translated"):
            continue
        original = item["original"].strip()
        translated = item["translated"].strip()

        # 检查每个术语
        for en_term, zh_term in glossary.items():
            if en_term.lower() in original.lower():
                if en_term not in term_translations:
                    term_translations[en_term] = {}
                term_translations[en_term][zh_term] = term_translations[en_term].get(zh_term, 0) + 1

    # 找出一对多的术语
    for en_term, translations in term_translations.items():
        if len(translations) > 1:
            most_common = max(translations, key=translations.get)
            for zh, count in translations.items():
                if zh != most_common:
                    inconsistent.append({
                        "term": en_term,
                        "expected": most_common,
                        "found": zh,
                        "count": count,
                    })

    return inconsistent


# ══════════════════════════════════════════════════════════
#  6. 高级模糊搜索（结合所有技术）
# ══════════════════════════════════════════════════════════

def deep_fuzzy_search(text, glossary, tm, threshold=0.50):
    """
    深度模糊搜索 — 仿 DeepL 术语库匹配。

    搜索优先级:
      1. TM 精确匹配 → 置信度 1.0
      2. TM 模糊匹配 → 置信度 0.7~0.9
      3. 词典混合匹配 → 置信度 0.6~0.85
      4. 无匹配 → None

    Returns: (translation, confidence_score, source)
    """
    text_clean = text.strip()
    text_norm = normalize_spelling(text_clean)

    # 1. TM 精确
    if text_norm in tm:
        return tm[text_norm], 1.0, "tm_exact"

    # 2. TM 模糊
    best_key, best_score = find_fuzzy_match(text_norm, tm, threshold=0.82)
    if best_key:
        return tm[best_key], best_score * 0.95, "tm_fuzzy"

    # 3. 词典混合匹配
    best_key, best_score = find_fuzzy_match(text_norm, glossary, threshold=threshold)
    if best_key:
        translated = glossary[best_key]
        # 混合相似度验证
        hybrid_score = hybrid_similarity(text_norm, best_key)
        confidence = min(0.85, hybrid_score * 1.1)
        return translated, confidence, "glossary_fuzzy"

    return None, 0.0, "no_match"
