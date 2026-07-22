import re
from collections import Counter

c = open(r"C:\Users\Administrator\Desktop\WISEC10_translated.a2l", encoding="utf-8").read()
pattern = re.compile(r'/begin\s+(MEASUREMENT|CHARACTERISTIC|COMPU_METHOD|FUNCTION|AXIS_PTS|COMPU_VTAB|GROUP|PROJECT|MODULE)\s+(\w+)\s+"([^"]*)"', re.I)
ms = pattern.findall(c)

# Stats by type
total_by_type = Counter()
trans_by_type = Counter()
untrans_by_type = Counter()
untrans_samples = {k: [] for k in set(t for t,_,_ in ms)}

for typ, name, desc in ms:
    total_by_type[typ] += 1
    has_zh = any('一' <= x <= '鿿' for x in desc)
    if has_zh:
        trans_by_type[typ] += 1
    elif len(desc.strip()) > 0:
        untrans_by_type[typ] += 1
        if len(untrans_samples[typ]) < 3:
            untrans_samples[typ].append(f"{name}: {desc[:80]}")

print(f"{'Type':<20s} {'Total':>6s} {'Trans':>6s} {'Untrans':>8s} {'Rate':>7s}")
print("-" * 55)
for typ in sorted(total_by_type.keys(), key=lambda t: -total_by_type[t]):
    total = total_by_type[typ]
    trans = trans_by_type.get(typ, 0)
    untrans = untrans_by_type.get(typ, 0)
    rate = trans / total * 100 if total > 0 else 0
    print(f"{typ:<20s} {total:>6d} {trans:>6d} {untrans:>8d} {rate:>6.1f}%")
    for s in untrans_samples[typ]:
        print(f"    ↳ {s}")

# Also check comments
comments = re.findall(r'/\*([\s\S]*?)\*/', c) + re.findall(r'//([^\r\n]*)', c)
comment_total = len(comments)
comment_zh = sum(1 for x in comments if any('一' <= c <= '鿿' for c in x))
print(f"\nCOMMENT (block+line): {comment_total} total, {comment_zh} with Chinese ({comment_zh/comment_total*100:.1f}%)")
