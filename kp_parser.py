#!/usr/bin/env python3
"""
KP 文件解析器 — WinOLS Map Pack (.kp) 文件翻译支持
====================================================
WinOLS KP 文件是 ECU 调校图包，包含：
  - 图名称 (Map names)
  - 图描述 (Map descriptions)
  - 轴标签 (Axis labels)
  - 单位信息 (Units)

格式：二进制头 + 长度前缀字符串 + 图数据块
"""

import struct
import sys
from pathlib import Path


def parse_kp_header(data):
    """解析 KP 文件头，返回元信息和字符串列表"""
    if len(data) < 16:
        raise ValueError("文件太小，不是有效的 KP 文件")

    pos = 0
    strings = []

    # 验证魔数 "WinOLS File"
    str_len = struct.unpack_from('<I', data, pos)[0]
    pos += 4
    magic = data[pos:pos+str_len].decode('latin-1', errors='replace').rstrip('\x00')
    pos += str_len

    if magic != "WinOLS File":
        raise ValueError(f"不是 WinOLS KP 文件: {magic}")

    # 跳过一些字段读取文件大小和版本
    # WinOLS header structure (simplified):
    # [4] magic_len + magic
    # [4] unknown (version?)
    # [4] file_size
    # [4] unknown
    # ... padding zeros ...
    # [4] filename_len + filename
    # [4] version_len + version
    # [4] map_count
    # ... map entries ...

    # Read file size
    unknown1 = struct.unpack_from('<I', data, pos)[0]
    pos += 4
    file_size = struct.unpack_from('<I', data, pos)[0]
    pos += 4
    # Skip 44 bytes of zeros/padding
    pos += 44  # Based on hex analysis

    # Read filename
    fn_len = struct.unpack_from('<I', data, pos)[0]
    pos += 4
    filename = data[pos:pos+fn_len].decode('latin-1', errors='replace').rstrip('\x00')
    pos += fn_len
    strings.append(('filename', filename))

    # Read version
    ver_len = struct.unpack_from('<I', data, pos)[0]
    pos += 4
    version = data[pos:pos+ver_len].decode('latin-1', errors='replace').rstrip('\x00')
    pos += ver_len
    strings.append(('version', version))

    # Try to read map count
    pos += 4  # Skip potential padding

    # Extract all readable length-prefixed strings from the rest of the file
    remaining = data[pos:]

    map_strings = []
    i = 0
    while i < len(remaining) - 4:
        try:
            slen = struct.unpack_from('<I', remaining, i)[0]
            # Sanity check: string length
            if 2 <= slen <= 256:
                try:
                    s = remaining[i+4:i+4+slen].decode('utf-8', errors='replace')
                    # Filter: only keep printable strings
                    printable = sum(1 for c in s if c.isprintable() or c in '\n\r\t')
                    if printable / len(s) > 0.7 and len(s.strip()) > 2:
                        map_strings.append(s.strip())
                except:
                    pass
            i += 4 + max(0, slen)
        except:
            i += 1

    return {
        'magic': magic,
        'filename': filename,
        'version': version,
        'file_size': file_size,
        'strings': strings,
        'map_strings': list(set(map_strings)),  # dedup
    }


def extract_translatable(info):
    """从解析结果中提取可翻译文本"""
    items = []
    counter = 0

    # 文件名和版本
    for typ, text in info['strings']:
        counter += 1
        items.append({
            'id': counter,
            'type': typ.upper(),
            'name': typ,
            'original': text,
            'translated': '',
            'status': 'untranslated',
            'start': 0,
            'end': len(text),
        })

    # 图名字符串
    for text in info['map_strings']:
        if len(text) >= 3 and not text.startswith(('WISE', 'OLS', 'WinOLS')):
            counter += 1
            items.append({
                'id': counter,
                'type': 'MAP_NAME',
                'name': '',
                'original': text,
                'translated': '',
                'status': 'untranslated',
                'start': 0,
                'end': len(text),
            })

    return items


def rebuild_kp(data, translations):
    """将翻译写回 KP 文件（仅替换长度前缀的 ASCII 字符串）"""
    result = bytearray(data)
    # 对每个翻译，在二进制数据中搜索并替换
    for item in translations:
        original = item['original'].encode('latin-1', errors='replace')
        translated = item['translated'].encode('utf-8', errors='replace')
        if item['translated'] and translated != original:
            # 搜索原始字符串位置
            pos = 0
            while pos < len(result) - len(original):
                idx = result.find(original, pos)
                if idx == -1:
                    break
                # 检查前面4字节是否是长度前缀
                if idx >= 4:
                    stored_len = struct.unpack_from('<I', result, idx - 4)[0]
                    if stored_len == len(original) or abs(stored_len - len(original)) <= 3:
                        # 替换
                        result[idx:idx+len(original)] = translated
                        # 更新长度前缀
                        struct.pack_into('<I', result, idx - 4, len(translated))
                pos = idx + 1
    return bytes(result)


def main():
    if len(sys.argv) < 2:
        print("用法: python kp_parser.py <file.kp>")
        print("      python kp_parser.py <file.kp> --translate")
        return 1

    filepath = Path(sys.argv[1])
    if not filepath.exists():
        print(f"文件不存在: {filepath}")
        return 1

    with open(filepath, 'rb') as f:
        data = f.read()

    print(f"\n  KP 文件: {filepath.name} ({len(data)/1024:.1f} KB)")
    print(f"{'='*60}")

    info = parse_kp_header(data)
    print(f"  格式: {info['magic']}")
    print(f"  版本: {info['version']}")
    print(f"  文件名: {info['filename']}")
    print(f"  文件大小: {info['file_size']} bytes")

    items = extract_translatable(info)
    print(f"\n  可翻译文本: {len(items)} 条")
    print(f"{'='*60}")

    for item in items:
        print(f"  [{item['type']}] {item['original'][:80]}")

    if '--translate' in sys.argv:
        from a2l_translator import auto_translate
        from glossary_data import BUILTIN_GLOSSARY, GERMAN_GLOSSARY

        glossary = dict(BUILTIN_GLOSSARY)
        glossary.update(GERMAN_GLOSSARY)

        translated = auto_translate(items, glossary, 'auto', 'zh-CN')
        print(f"\n  翻译完成: {translated} 条")

        # 写回
        new_data = rebuild_kp(data, items)
        out_path = filepath.parent / f"{filepath.stem}_cn{filepath.suffix}"
        with open(out_path, 'wb') as f:
            f.write(new_data)
        print(f"  输出: {out_path}")

    return 0


if __name__ == '__main__':
    sys.exit(main())
