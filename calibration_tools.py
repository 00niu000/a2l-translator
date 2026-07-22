#!/usr/bin/env python3
"""
ECU 标定工具集
==============
1. HEX/BIN 数据对比 — 比较两份标定数据差异
2. 校验和计算 (CVN/Checksum)
3. 文件格式转换 (HEX ↔ S19 ↔ BIN)

用法:
  python calibration_tools.py compare file1.bin file2.bin
  python calibration_tools.py checksum file.bin --algo crc32
  python calibration_tools.py convert input.hex output.s19
"""

import sys
import struct
import zlib
import argparse
from pathlib import Path
from collections import defaultdict


# ══════════════════════════════════════════════════════════
#  1. HEX/BIN 数据对比
# ══════════════════════════════════════════════════════════

def parse_hex_line(line):
    """解析 Intel HEX 格式的一行，返回 (address, data_bytes)"""
    line = line.strip()
    if not line.startswith(':'):
        return None, None
    try:
        byte_count = int(line[1:3], 16)
        address = int(line[3:7], 16)
        record_type = int(line[7:9], 16)
        if record_type != 0:  # 跳过非数据记录
            return None, None
        data = bytes.fromhex(line[9:9 + byte_count * 2])
        return address, data
    except Exception:
        return None, None


def parse_s19_line(line):
    """解析 Motorola S-Record (S19) 格式"""
    line = line.strip()
    if not line.startswith('S'):
        return None, None
    try:
        rec_type = line[0:2]
        if rec_type not in ('S1', 'S2', 'S3'):
            return None, None
        byte_count = int(line[2:4], 16)
        if rec_type == 'S1':
            addr_len = 2
        elif rec_type == 'S2':
            addr_len = 3
        else:
            addr_len = 4
        address = int(line[4:4 + addr_len * 2], 16)
        data_start = 4 + addr_len * 2
        data_end = 2 + byte_count * 2 - 2  # 减去校验和
        data = bytes.fromhex(line[data_start:data_end])
        return address, data
    except Exception:
        return None, None


def load_firmware(filepath):
    """加载固件文件 (支持 BIN/HEX/S19)"""
    path = Path(filepath)
    suffix = path.suffix.lower()

    if suffix == '.bin':
        with open(filepath, 'rb') as f:
            return f.read(), 'bin'

    # HEX/S19: 构建地址→数据映射
    data_map = {}
    with open(filepath, 'r') as f:
        for line in f:
            if suffix in ('.hex', '.ihex'):
                addr, data = parse_hex_line(line)
            elif suffix in ('.s19', '.srec', '.mot'):
                addr, data = parse_s19_line(line)
            else:
                # 尝试自动检测
                if line.strip().startswith(':'):
                    addr, data = parse_hex_line(line)
                elif line.strip().startswith('S'):
                    addr, data = parse_s19_line(line)
                else:
                    continue
            if addr is not None and data:
                data_map[addr] = data

    if not data_map:
        return None, 'unknown'

    # 展平为连续二进制
    min_addr = min(data_map.keys())
    max_addr = max(data_map.keys()) + max(len(d) for d in data_map.values())
    result = bytearray(max_addr - min_addr)
    for addr, data in data_map.items():
        result[addr - min_addr:addr - min_addr + len(data)] = data
    return bytes(result), f'{suffix} (0x{min_addr:X}-0x{max_addr:X})'


def compare_firmware(file1, file2, show_all=False):
    """比较两份固件/标定数据"""
    data1, fmt1 = load_firmware(file1)
    data2, fmt2 = load_firmware(file2)

    if data1 is None or data2 is None:
        print("错误: 无法加载文件")
        return

    print(f"\n  {'='*55}")
    print(f"  标定数据对比")
    print(f"  {'='*55}")
    print(f"  文件1: {Path(file1).name} ({len(data1):,} bytes, {fmt1})")
    print(f"  文件2: {Path(file2).name} ({len(data2):,} bytes, {fmt2})")

    min_len = min(len(data1), len(data2))
    diff_count = 0
    diff_regions = []
    region_start = None
    total_diff_bytes = 0

    for i in range(min_len):
        if data1[i] != data2[i]:
            if region_start is None:
                region_start = i
            diff_count += 1
        else:
            if region_start is not None:
                diff_regions.append((region_start, i - 1))
                total_diff_bytes += i - region_start
                region_start = None

    if region_start is not None:
        diff_regions.append((region_start, min_len - 1))
        total_diff_bytes += min_len - region_start

    diff_pct = diff_count / min_len * 100 if min_len > 0 else 0

    print(f"\n  差异字节: {diff_count:,} / {min_len:,} ({diff_pct:.2f}%)")
    print(f"  差异区域: {len(diff_regions)} 处")

    if len(data1) != len(data2):
        size_diff = len(data2) - len(data1)
        print(f"  文件大小差异: {'+' if size_diff > 0 else ''}{size_diff:,} bytes")

    # 显示差异区域
    show_count = len(diff_regions) if show_all else min(20, len(diff_regions))
    for i, (start, end) in enumerate(diff_regions[:show_count]):
        length = end - start + 1
        print(f"\n  [{i+1}] 0x{start:06X} - 0x{end:06X} ({length} bytes)")
        # 显示前 16 字节对照
        d1 = data1[start:start+16]
        d2 = data2[start:start+16]
        print(f"    1: {' '.join(f'{b:02X}' for b in d1)}")
        print(f"    2: {' '.join(f'{b:02X}' for b in d2)}")

    if len(diff_regions) > show_count:
        print(f"\n  ... 还有 {len(diff_regions) - show_count} 处差异")

    # 整体校验和
    print(f"\n  {'─'*55}")
    print(f"  CRC32:  {zlib.crc32(data1):08X}  →  {zlib.crc32(data2):08X}")
    print(f"  MD5:    {hashlib_md5_hex(data1)}  →  {hashlib_md5_hex(data2)}")
    print(f"  {'─'*55}")


# ══════════════════════════════════════════════════════════
#  2. 校验和计算
# ══════════════════════════════════════════════════════════

def hashlib_md5_hex(data):
    import hashlib
    return hashlib.md5(data).hexdigest().upper()


def calc_crc16(data, poly=0x1021, init=0xFFFF):
    """CRC-16-CCITT"""
    crc = init
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ poly
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


def calc_crc32_custom(data, poly=0xEDB88320, init=0xFFFFFFFF):
    """CRC32 (自定义多项式，默认标准)"""
    crc = init
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ poly
            else:
                crc >>= 1
    return crc ^ 0xFFFFFFFF


def calc_checksum16(data):
    """16位累加和校验"""
    total = sum(data)
    while total > 0xFFFF:
        total = (total & 0xFFFF) + (total >> 16)
    return total & 0xFFFF


def calc_checksum32(data):
    """32位累加和校验"""
    total = sum(data)
    return total & 0xFFFFFFFF


def checksum_file(filepath, algorithms=None):
    """计算文件的各种校验和"""
    data, fmt = load_firmware(filepath)

    if data is None:
        print("错误: 无法加载文件")
        return

    algs = algorithms or ['crc16', 'crc32', 'sum16', 'sum32', 'md5']

    print(f"\n  {'='*50}")
    print(f"  校验和计算")
    print(f"  {'='*50}")
    print(f"  文件: {Path(filepath).name} ({len(data):,} bytes, {fmt})")
    print(f"  {'─'*50}")

    results = {}
    if 'crc16' in algs:
        results['CRC16-CCITT'] = f"{calc_crc16(data):04X}"
    if 'crc32' in algs:
        results['CRC32(zlib)'] = f"{zlib.crc32(data):08X}"
        results['CRC32(custom)'] = f"{calc_crc32_custom(data):08X}"
    if 'sum16' in algs:
        results['Checksum16'] = f"{calc_checksum16(data):04X}"
    if 'sum32' in algs:
        results['Checksum32'] = f"{calc_checksum32(data):08X}"
    if 'md5' in algs:
        results['MD5'] = hashlib_md5_hex(data)

    for name, value in results.items():
        print(f"  {name:<20s}  0x{value}")


# ══════════════════════════════════════════════════════════
#  3. 文件格式转换
# ══════════════════════════════════════════════════════════

def hex_to_bin(input_path, output_path):
    """Intel HEX → 原始二进制"""
    data, _ = load_firmware(input_path)
    if data:
        with open(output_path, 'wb') as f:
            f.write(data)
        print(f"  转换完成: {Path(output_path).name} ({len(data):,} bytes)")


def bin_to_hex(input_path, output_path, base_addr=0, line_size=32):
    """原始二进制 → Intel HEX"""
    with open(input_path, 'rb') as f:
        data = f.read()

    with open(output_path, 'w') as f:
        for i in range(0, len(data), line_size):
            chunk = data[i:i+line_size]
            addr = base_addr + i
            f.write(f":{len(chunk):02X}{addr:04X}00")
            f.write(chunk.hex().upper())
            # 校验和
            cks = (len(chunk) + (addr >> 8) + (addr & 0xFF) + sum(chunk)) & 0xFF
            cks = (-cks) & 0xFF
            f.write(f"{cks:02X}\n")
        f.write(":00000001FF\n")  # EOF
    print(f"  转换完成: {Path(output_path).name}")


def bin_to_s19(input_path, output_path, base_addr=0):
    """原始二进制 → Motorola S19"""
    with open(input_path, 'rb') as f:
        data = f.read()

    with open(output_path, 'w') as f:
        f.write("S0030000FC\n")  # Header
        for i in range(0, len(data), 32):
            chunk = data[i:i+32]
            addr = base_addr + i
            # S3 record: count(2) + addr(4) + data + cks(1)
            count = len(chunk) + 5  # addr(4) + data + cks(1) = 5
            line = f"S3{count:02X}{addr:08X}{chunk.hex().upper()}"
            cks = sum(bytes.fromhex(line[2:])) & 0xFF
            cks = (-cks) & 0xFF
            f.write(f"{line}{cks:02X}\n")
        f.write("S70500000000FA\n")  # EOF
    print(f"  转换完成: {Path(output_path).name}")


def convert_file(input_path, output_path):
    """自动检测格式并转换"""
    in_suffix = Path(input_path).suffix.lower()
    out_suffix = Path(output_path).suffix.lower()

    if in_suffix == out_suffix:
        print("  输入输出格式相同，无需转换")
        return

    if in_suffix in ('.hex', '.ihex') and out_suffix == '.bin':
        hex_to_bin(input_path, output_path)
    elif in_suffix == '.bin' and out_suffix in ('.hex', '.ihex'):
        bin_to_hex(input_path, output_path)
    elif in_suffix == '.bin' and out_suffix in ('.s19', '.srec', '.mot'):
        bin_to_s19(input_path, output_path)
    elif in_suffix in ('.s19', '.srec', '.mot') and out_suffix == '.bin':
        data, _ = load_firmware(input_path)
        if data:
            with open(output_path, 'wb') as f:
                f.write(data)
            print(f"  转换完成: {Path(output_path).name} ({len(data):,} bytes)")
    else:
        print(f"  不支持的转换: {in_suffix} → {out_suffix}")


# ══════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="ECU 标定工具集")
    sub = parser.add_subparsers(dest='cmd', help='子命令')

    # compare
    cmp_parser = sub.add_parser('compare', help='比较两份标定数据')
    cmp_parser.add_argument('file1')
    cmp_parser.add_argument('file2')
    cmp_parser.add_argument('--all', '-a', action='store_true', help='显示所有差异区域')

    # checksum
    cks_parser = sub.add_parser('checksum', help='计算校验和')
    cks_parser.add_argument('file')
    cks_parser.add_argument('--algo', nargs='+', choices=['crc16','crc32','sum16','sum32','md5','all'],
                           default=['all'], help='算法 (默认: all)')

    # convert
    cnv_parser = sub.add_parser('convert', help='文件格式转换')
    cnv_parser.add_argument('input')
    cnv_parser.add_argument('output')

    args = parser.parse_args()

    if args.cmd == 'compare':
        compare_firmware(args.file1, args.file2, show_all=args.all)
    elif args.cmd == 'checksum':
        algs = ['crc16','crc32','sum16','sum32','md5'] if 'all' in args.algo else args.algo
        checksum_file(args.file, algs)
    elif args.cmd == 'convert':
        convert_file(args.input, args.output)
    else:
        parser.print_help()

    return 0

if __name__ == '__main__':
    sys.exit(main())
