#!/usr/bin/env python3
"""
A2L 翻译工具 — 自动扫描 & 批量翻译模块
==========================================
功能：
  1. 递归扫描指定目录，自动发现所有 .a2l 文件
  2. 文件监控模式 — 检测到新文件自动翻译
  3. 智能跳过已翻译文件（检查 *_translated.a2l 或 _cn.a2l）
  4. 批量翻译进度汇总

用法：
  python auto_scanner.py --scan D:\A2L_Files --auto-translate
  python auto_scanner.py --watch D:\incoming --auto-translate
  python auto_scanner.py --scan . --auto --baidu-appid xxx --baidu-secret xxx
"""

import sys
import os
import time
import json
import argparse
from pathlib import Path
from datetime import datetime

# ── 内嵌轻量 a2l 判断（避免大文件导入整个翻译引擎）──
_A2L_HEADER_MARKERS = [
    b"ASAP2_VERSION",
    b"/begin PROJECT",
    b"/begin MODULE",
    b"A2ML_VERSION",
]

def is_a2l_file(filepath, check_header=True):
    """判断是否为 A2L 文件（扩展名 + 可选内容检查）"""
    path = Path(filepath)
    if path.suffix.lower() != '.a2l':
        return False
    if not check_header:
        return True
    # 快速检查文件头
    try:
        with open(filepath, 'rb') as f:
            head = f.read(512)
        for marker in _A2L_HEADER_MARKERS:
            if marker in head:
                return True
        return False
    except Exception:
        return False


def is_already_translated(filepath):
    """检查是否已有翻译过的输出文件"""
    path = Path(filepath)
    parent = path.parent
    stem = path.stem
    # 检查常见输出命名
    candidates = [
        parent / f"{stem}_translated.a2l",
        parent / f"{stem}_cn.a2l",
        parent / f"{stem}_zh.a2l",
        parent / f"{stem}_中文.a2l",
        parent / "translated" / f"{stem}.a2l",
    ]
    for c in candidates:
        if c.exists():
            return True, str(c)
    return False, None


def scan_a2l_files(roots, recursive=True, skip_translated=True, check_content=True):
    """
    扫描目录，返回所有 A2L 文件列表。

    Args:
        roots: 根目录列表
        recursive: 是否递归子目录
        skip_translated: 是否跳过已翻译文件
        check_content: 是否检查文件头（true=只识别真 A2L，false=信任扩展名）

    Returns:
        list[Path]: A2L 文件路径列表
    """
    found = []
    skipped = 0

    for root in roots:
        root_path = Path(root)
        if not root_path.exists():
            print(f"  [跳过] 目录不存在: {root}")
            continue

        pattern = "**/*.a2l" if recursive else "*.a2l"
        for a2l_file in root_path.glob(pattern):
            if not is_a2l_file(a2l_file, check_header=check_content):
                continue

            if skip_translated:
                translated, out_path = is_already_translated(a2l_file)
                if translated:
                    skipped += 1
                    continue

            found.append(a2l_file)

    if skipped:
        print(f"  已跳过 {skipped} 个已翻译文件")
    return found


def watch_directory(dirs, callback, interval=5):
    """
    监控目录变化 — 检测到新 A2L 文件自动触发翻译。

    Args:
        dirs: 监控目录列表
        callback: 发现新文件时的回调函数 callback(filepath)
        interval: 扫描间隔秒数
    """
    print(f"\n👁  文件监控模式 — 每 {interval}s 扫描一次")
    print(f"  监控目录: {', '.join(str(d) for d in dirs)}")
    print(f"  按 Ctrl+C 停止\n")

    known_files = set()
    # 首次扫描 — 记录已存在的文件
    for d in dirs:
        for a2l in Path(d).glob("**/*.a2l"):
            if is_a2l_file(a2l):
                known_files.add(str(a2l.resolve()))

    print(f"  已记录 {len(known_files)} 个现有文件，等待新文件...")

    try:
        while True:
            time.sleep(interval)
            for d in dirs:
                if not Path(d).exists():
                    continue
                for a2l in Path(d).glob("**/*.a2l"):
                    a2l_path = str(a2l.resolve())
                    if a2l_path not in known_files and is_a2l_file(a2l):
                        known_files.add(a2l_path)
                        print(f"\n{'='*60}")
                        print(f"  🆕 发现新文件: {a2l.name}")
                        print(f"  📁 {a2l_path}")
                        print(f"  🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                        print(f"{'='*60}")
                        callback(a2l)
    except KeyboardInterrupt:
        print("\n  监控已停止")


def build_report(results):
    """生成批量翻译报告"""
    total = len(results)
    success = sum(1 for r in results if r.get("translated", 0) > 0)
    total_translated = sum(r.get("translated", 0) for r in results)
    total_entries = sum(r.get("entries", 0) for r in results)

    report = {
        "timestamp": datetime.now().isoformat(),
        "files_total": total,
        "files_translated": success,
        "files_unchanged": total - success,
        "total_entries": total_entries,
        "total_translated": total_translated,
        "files": results,
    }
    return report


def save_report(report, output_path):
    """保存批量翻译报告为 JSON"""
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n  报告已保存: {output_path}")


# ══════════════════════════════════════════════════════════
#  CLI 入口
# ══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="A2L 自动扫描 & 批量翻译工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python auto_scanner.py --scan D:\\ECU_Files
  python auto_scanner.py --scan D:\\ECU --auto-translate
  python auto_scanner.py --watch D:\\incoming --auto-translate
  python auto_scanner.py --scan . --auto --report scan_report.json
  python auto_scanner.py --scan C:\\ --skip-existing --quiet
        """,
    )
    parser.add_argument("--scan", nargs="+", metavar="DIR",
                       help="扫描指定目录，列出所有 A2L 文件")
    parser.add_argument("--watch", nargs="+", metavar="DIR",
                       help="监控目录，发现新 A2L 文件自动翻译")
    parser.add_argument("--auto-translate", "--auto", action="store_true",
                       help="扫描后自动翻译")
    parser.add_argument("--recursive", "-r", action="store_true", default=True,
                       help="递归扫描子目录（默认开启）")
    parser.add_argument("--no-recursive", action="store_true",
                       help="不递归扫描子目录")
    parser.add_argument("--skip-translated", action="store_true", default=True,
                       help="跳过已翻译文件（默认）")
    parser.add_argument("--no-skip", action="store_true",
                       help="不跳过已翻译文件，全部重新处理")
    parser.add_argument("--report", metavar="FILE",
                       help="保存批量翻译报告为 JSON")
    parser.add_argument("--output-dir", "-o", metavar="DIR",
                       help="输出目录（默认与源文件同目录）")
    parser.add_argument("--quiet", "-q", action="store_true",
                       help="安静模式")
    parser.add_argument("--baidu-appid", metavar="APPID",
                       help="百度翻译 APP ID")
    parser.add_argument("--baidu-secret", metavar="SECRET",
                       help="百度翻译密钥")
    parser.add_argument("--interval", type=int, default=5,
                       help="监控扫描间隔秒数（默认5秒）")

    args = parser.parse_args()

    recursive = not args.no_recursive
    skip_existing = not args.no_skip

    # ── 扫描模式 ──
    if args.scan:
        roots = args.scan
        print(f"\n  🔍 扫描 A2L 文件...")
        if not args.quiet:
            print(f"  目录: {', '.join(roots)}")
            print(f"  递归: {'是' if recursive else '否'}")
            print(f"  跳过已翻译: {'是' if skip_existing else '否'}")

        files = scan_a2l_files(roots, recursive=recursive,
                               skip_translated=skip_existing,
                               check_content=True)

        if not files:
            print(f"\n  ✗ 未发现 A2L 文件")
            return 0

        print(f"\n  ✓ 发现 {len(files)} 个 A2L 文件:\n")
        total_size = 0
        for i, f in enumerate(files, 1):
            size = f.stat().st_size / (1024 * 1024)
            total_size += size
            print(f"  {i:>3d}. {f.name} ({size:.1f} MB)")
            if not args.quiet:
                print(f"       {f}")

        print(f"\n  总计: {len(files)} 个文件, {total_size:.1f} MB")

        # 自动翻译
        if args.auto_translate:
            from a2l_translator import parse_a2l, auto_translate, rebuild_a2l
            from a2l_translator import BUILTIN_GLOSSARY, GERMAN_GLOSSARY
            from translation_memory import (
                load_translation_memory, update_tm_from_items,
                load_custom_glossary, merge_glossary,
            )
            import ssl

            glossary = dict(BUILTIN_GLOSSARY)
            glossary.update(GERMAN_GLOSSARY)
            custom = load_custom_glossary()
            if custom:
                glossary = merge_glossary(glossary, custom)

            results = []

            for i, a2l_file in enumerate(files, 1):
                print(f"\n{'─'*60}")
                print(f"  [{i}/{len(files)}] {a2l_file.name}")
                print(f"{'─'*60}")

                try:
                    with open(a2l_file, 'r', encoding='utf-8') as f:
                        content = f.read()
                except UnicodeDecodeError:
                    with open(a2l_file, 'r', encoding='latin-1') as f:
                        content = f.read()

                items = parse_a2l(content)
                total_entries = len(items)

                if not args.quiet:
                    print(f"  解析到 {total_entries} 条可翻译文本")

                translated_count = auto_translate(
                    items, glossary,
                    "auto", "zh-CN",
                    baidu_appid=args.baidu_appid,
                    baidu_secret=args.baidu_secret,
                    ssl_ctx=None,
                )

                # 输出文件
                if args.output_dir:
                    out_dir = Path(args.output_dir)
                    out_dir.mkdir(parents=True, exist_ok=True)
                    out_path = out_dir / f"{a2l_file.stem}_translated.a2l"
                else:
                    out_path = a2l_file.parent / f"{a2l_file.stem}_translated.a2l"

                if translated_count > 0:
                    translated_content = rebuild_a2l(content, items)
                    with open(out_path, 'w', encoding='utf-8') as f:
                        f.write(translated_content)
                    if not args.quiet:
                        print(f"  ✓ 已输出: {out_path}")

                results.append({
                    "file": str(a2l_file),
                    "output": str(out_path),
                    "entries": total_entries,
                    "translated": translated_count,
                    "size_mb": round(a2l_file.stat().st_size / (1024 * 1024), 2),
                })

            # 汇总
            if results:
                report = build_report(results)
                total_trans = report["total_translated"]
                total_ent = report["total_entries"]
                print(f"\n{'='*60}")
                print(f"  📊 批量翻译完成!")
                print(f"  文件: {report['files_translated']}/{report['files_total']} 已翻译")
                print(f"  条目: {total_trans}/{total_ent} 已翻译")
                print(f"{'='*60}")

                if args.report:
                    save_report(report, args.report)

        return 0

    # ── 监控模式 ──
    elif args.watch:
        if not args.auto_translate:
            print("  监控模式需要配合 --auto-translate 使用")
            return 1

        def on_new_file(filepath):
            """发现新文件时的回调"""
            from a2l_translator import parse_a2l, auto_translate, rebuild_a2l
            from a2l_translator import BUILTIN_GLOSSARY, GERMAN_GLOSSARY

            glossary = dict(BUILTIN_GLOSSARY)
            glossary.update(GERMAN_GLOSSARY)

            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
            except UnicodeDecodeError:
                with open(filepath, 'r', encoding='latin-1') as f:
                    content = f.read()

            items = parse_a2l(content)
            translated = auto_translate(items, glossary, "auto", "zh-CN",
                                        baidu_appid=args.baidu_appid,
                                        baidu_secret=args.baidu_secret)

            if translated > 0:
                out_path = filepath.parent / f"{filepath.stem}_translated.a2l"
                translated_content = rebuild_a2l(content, items)
                with open(out_path, 'w', encoding='utf-8') as f:
                    f.write(translated_content)
                print(f"  ✅ 自动翻译完成 → {out_path.name}")
            else:
                print(f"  ⓘ  无需翻译")

        watch_directory(args.watch, on_new_file, interval=args.interval)
        return 0

    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
