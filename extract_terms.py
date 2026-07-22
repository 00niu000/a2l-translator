#!/usr/bin/env python3
"""
A2L 术语提取器 — 从 A2L 文件中提取未收录的专业术语
=====================================================
不导出文件内容，只提取术语列表，保护数据隐私。

用法:
  python extract_terms.py --scan D:\A2L_Files
  python extract_terms.py --scan D:\A2L_Files --output new_terms.json
  python extract_terms.py --scan D:\A2L_Files --merge  # 直接合并到 custom_glossary.json
"""

import sys
import re
import json
import argparse
from pathlib import Path
from collections import Counter

# ── 引入扫描器 ──
from auto_scanner import scan_a2l_files

# ── 引入词典 ──
from glossary_data import BUILTIN_GLOSSARY, GERMAN_GLOSSARY
from translation_memory import load_custom_glossary, save_custom_glossary

# ── 术语提取正则 ──
# 匹配双引号内的描述文本（MEASUREMENT / CHARACTERISTIC 的 name 和 description）
_QUOTED_TEXT_RE = re.compile(r'/begin\s+\w+\s+\w+\s+"([^"]+)"', re.I)
_HEADER_TEXT_RE = re.compile(r'/begin\s+(?:HEADER|MOD_COMMON|MOD_PAR)\s+"([^"]+)"', re.I)

# 提取有意义的英文短语（2-6 个单词，含常见 ECU 术语模式）
_TERM_PATTERN = re.compile(
    r'\b([A-Z][a-z]+(?:\s+(?:[A-Z][a-z]+|[a-z]+)){1,5})'
    r'(?:\s+(sensor|valve|pressure|temperature|speed|position|control|'
    r'module|unit|system|signal|voltage|current|ratio|angle|'
    r'torque|power|flow|level|status|mode|limit|filter|'
    r'correction|adaptation|detection|monitoring|protection|'
    r'compensation|estimation|prediction|calculation|'
    r'Sensor|Valve|Pressure|Temperature|Speed|Position|Control))?',
    re.IGNORECASE
)

# 要过滤的泛用词
_STOP_WORDS = {
    "the", "and", "for", "from", "with", "that", "this", "are", "was",
    "has", "have", "not", "but", "all", "can", "had", "her", "his",
    "its", "one", "our", "out", "she", "some", "than", "that", "their",
    "them", "then", "there", "these", "they", "this", "was", "were",
    "will", "with", "your", "about", "after", "also", "been", "being",
    "between", "both", "does", "during", "each", "every", "first",
    "into", "just", "like", "made", "make", "many", "more", "most",
    "much", "must", "only", "other", "over", "same", "said", "should",
    "since", "such", "take", "than", "through", "under", "until",
    "very", "well", "when", "where", "which", "while", "would",
}


def extract_terms_from_file(filepath):
    """从单个 A2L 文件中提取候选术语"""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except UnicodeDecodeError:
        with open(filepath, 'r', encoding='latin-1') as f:
            content = f.read()
    except Exception:
        return []

    terms = []

    # 1. 提取双引号内的完整描述
    for m in _QUOTED_TEXT_RE.finditer(content):
        text = m.group(1).strip()
        if 5 < len(text) < 120 and not text.startswith(("0x", "0X")):
            terms.append(("description", text))

    for m in _HEADER_TEXT_RE.finditer(content):
        text = m.group(1).strip()
        if 5 < len(text) < 120:
            terms.append(("header", text))

    # 2. 提取符合术语模式的短语
    for m in _TERM_PATTERN.finditer(content):
        phrase = m.group(0).strip()
        words = phrase.lower().split()
        # 过滤：有意义的术语 (2-6词，不含停用词为主)
        content_words = [w for w in words if w not in _STOP_WORDS]
        if 2 <= len(content_words) <= 6 and len(phrase) > 6:
            terms.append(("phrase", phrase))

    return terms


def extract_new_terms(filepaths, existing_glossary, min_freq=2, top_n=200):
    """
    从多个 A2L 文件提取新术语，排除已有词典中的。

    Returns:
        dict: {term: count} 按出现频率排序的新术语
    """
    existing_keys = set(k.lower() for k in existing_glossary.keys())
    counter = Counter()

    for i, fp in enumerate(filepaths, 1):
        print(f"  [{i}/{len(filepaths)}] 提取: {fp.name} ...", end=" ")
        terms = extract_terms_from_file(fp)
        for typ, text in terms:
            if text.lower() not in existing_keys:
                counter[text] += 1
        print(f"{len(terms)} 候选")

    # 按频率筛选
    new_terms = {}
    for term, count in counter.most_common(top_n):
        if count >= min_freq:
            new_terms[term] = count

    return new_terms


def main():
    parser = argparse.ArgumentParser(
        description="A2L 术语提取器 — 从文件中提取未收录的专业术语",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python extract_terms.py --scan D:\\A2L_Files
  python extract_terms.py --scan D:\\A2L_Files --output new_terms.json
  python extract_terms.py --scan D:\\A2L_Files --merge  # 直接合并到 custom_glossary.json
        """,
    )
    parser.add_argument("--scan", nargs="+", metavar="DIR", required=True,
                       help="扫描目录查找 A2L 文件")
    parser.add_argument("--output", "-o", metavar="FILE",
                       help="输出新术语到 JSON 文件")
    parser.add_argument("--merge", action="store_true",
                       help="直接合并到 custom_glossary.json（需要手动填写中文翻译）")
    parser.add_argument("--min-freq", type=int, default=2,
                       help="术语最低出现次数（默认2次）")
    parser.add_argument("--top", type=int, default=200,
                       help="最多提取条数（默认200）")
    parser.add_argument("--no-skip", action="store_true",
                       help="不跳过已翻译文件")

    args = parser.parse_args()

    print(f"\n  🔍 扫描 A2L 文件...")
    files = scan_a2l_files(args.scan, recursive=True,
                          skip_translated=not args.no_skip,
                          check_content=True)

    if not files:
        print("  ✗ 未发现 A2L 文件")
        return 1

    print(f"\n  📄 发现 {len(files)} 个 A2L 文件\n")

    # 合并所有现有词典
    all_glossary = {}
    all_glossary.update(BUILTIN_GLOSSARY)
    all_glossary.update(GERMAN_GLOSSARY)
    custom = load_custom_glossary()
    all_glossary.update(custom)

    # 提取新术语
    print(f"  📊 提取新术语（最低频率: {args.min_freq}次, 最多: {args.top}条）...\n")
    new_terms = extract_new_terms(files, all_glossary,
                                 min_freq=args.min_freq,
                                 top_n=args.top)

    if not new_terms:
        print("  ✓ 未发现新术语，现有词典已覆盖所有常见术语")
        return 0

    print(f"\n  {'='*60}")
    print(f"  📋 发现 {len(new_terms)} 个新术语（按频率排序）")
    print(f"  {'='*60}")
    print(f"\n  {'术语':<50s} {'频率':>6s}")
    print(f"  {'─'*50} {'─'*6}")
    for term, count in new_terms.items():
        print(f"  {term[:48]:<50s} {count:>6d}")

    # 输出为新术语模板（带空翻译）
    template = {}
    for term in new_terms:
        template[term] = ""

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump(template, f, ensure_ascii=False, indent=2)
        print(f"\n  ✓ 已导出到: {args.output}")
        print(f"  请用文本编辑器打开，填写中文翻译后保存")

    if args.merge:
        # 合并到 custom_glossary（待翻译项标记为 ""）
        custom.update(template)
        if save_custom_glossary(custom):
            print(f"\n  ✓ 已合并到 custom_glossary.json")
            untranslated = sum(1 for v in template.values() if not v)
            print(f"  共 {len(custom)} 条，其中 {untranslated} 条待翻译")
            print(f"  💡 打开 custom_glossary.json，为每个 \"\" 填写中文翻译即可")

    return 0


if __name__ == "__main__":
    sys.exit(main())
