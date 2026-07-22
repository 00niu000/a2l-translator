import re
from collections import Counter

c = open(r"C:\Users\Administrator\Desktop\WISEC10_translated.a2l", encoding="utf-8").read()

# Main A2L types
pattern = re.compile(r'/begin\s+(MEASUREMENT|CHARACTERISTIC|AXIS_PTS|FUNCTION|COMPU_METHOD|COMPU_VTAB)\s+(?:\w+\s+)?"([^"]*)"', re.I)
ms = pattern.findall(c)

total = len(ms)
empty = 0
trans = 0
untrans = 0
formula = 0

for typ, desc in ms:
    d = desc.strip()
    if not d:
        empty += 1
    elif re.match(r'^[0-9+\-*/(). eE]+$', d) or d.startswith("Q ="):
        formula += 1
    elif any('一' <= x <= '鿿' for x in d):
        trans += 1
    else:
        untrans += 1

valid = total - empty - formula
print(f"=== Translation Success Rate ===")
print(f"Total entries: {total}")
print(f"Empty descriptions: {empty} (skipped)")
print(f"Formulas: {formula} (skipped)")
print(f"Valid translatable: {valid}")
print(f"Translated: {trans} ({trans/valid*100:.1f}%)")
print(f"Untranslated: {untrans} ({untrans/valid*100:.1f}%)")
