"""Fail if code or docs contain invisible or direction-changing characters.

These can make code read differently from how it runs ("Trojan Source").
Escapes like \\u200b in a string are fine; the raw character is not.
"""
import sys
from pathlib import Path

BAD = {0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, *range(0x202A, 0x202F), *range(0x2066, 0x206A)}
ROOT = Path(__file__).resolve().parents[1]
found = 0
for p in ROOT.rglob("*"):
    if p.suffix not in {".py", ".md", ".toml", ".yml", ".txt"} or ".git" in p.parts:
        continue
    for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        for ch in line:
            if ord(ch) in BAD:
                print(f"{p.relative_to(ROOT)}:{n}: U+{ord(ch):04X}")
                found += 1
print(f"{found} invisible character(s) found")
sys.exit(1 if found else 0)
