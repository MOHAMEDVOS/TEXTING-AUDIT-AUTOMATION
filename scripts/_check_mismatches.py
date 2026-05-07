import re, sys, json
sys.path.insert(0, '.')

from ai.prefilter.tier1_phrases import (
    _WRONG_NUMBER_PATTERNS, _NOT_THIS_PERSON_PATTERNS,
    _NOT_INTERESTED_PATTERNS, _OPT_OUT_PATTERNS,
)

# Test: Regina Ann
text1 = "Do your home work. Have not owned that property in 2 YEARS."
print(f"=== Regina Ann: '{text1}' ===")
for p in _WRONG_NUMBER_PATTERNS:
    if p.search(text1):
        print(f"  MATCH: {p.pattern}")
print()

# Test: Tom Roll
text2 = "Not tom"
print(f"=== Tom Roll: '{text2}' ===")
for p in _NOT_THIS_PERSON_PATTERNS:
    if p.search(text2):
        print(f"  MATCH (identity): {p.pattern}")
for p in _WRONG_NUMBER_PATTERNS:
    if p.search(text2):
        print(f"  MATCH (wrong_num): {p.pattern}")
# Test the specific pattern
pat = re.compile(r"^\s*[Nn]ot\s+[A-Z]\w+\s*$", re.MULTILINE)
print(f"  Direct regex test: {bool(pat.search(text2))}")
# Issue: "tom" is lowercase — pattern requires [A-Z]
pat2 = re.compile(r"^\s*[Nn]ot\s+\w+\s*$", re.MULTILINE)
print(f"  Case-relaxed test: {bool(pat2.search(text2))}")
print()

# Test: Hongdong Zheng
text3 = "No, We don't have plan"
print(f"=== Hongdong: '{text3}' ===")
for p in _NOT_INTERESTED_PATTERNS:
    if p.search(text3):
        print(f"  MATCH: {p.pattern}")
# Test "No," specifically
print(f"  'no' multiline: {bool(re.search(r'^\\s*no\\.?\\s*$', text3, re.I | re.MULTILINE))}")
print(f"  'no thanks' in text: {'no thank' in text3.lower()}")

# Test: Delsa Evans (full contact text)
text4 = "Delsa passed Thanks. Maybe in the near future Check back with me in a couple months Not yet. Im not ready to sell yet. But possibly soon. Thanks for your understanding It's not really mine. My daughter lives there.  She has mentioned it   Sorry. Thats all I know No"
print(f"\n=== Delsa Evans (full contact text) ===")
for p in _NOT_INTERESTED_PATTERNS:
    if p.search(text4):
        print(f"  NI MATCH: {p.pattern}")
        break
print(f"  ABV_MV price guard: {bool(re.search(r'(\\$?\\d{3,}\\s*k|\\$?[1-9]\\d{5,})', text4, re.I))}")

# Test: Peter Benner opt-out check
text5 = "Hi Jack, thank you for the offer.However i'm not interested Thank you for the offer. However, I'm okay. No, i don't believe so. I'll keep that in mind."
print(f"\n=== Peter Benner ===")
for p in _OPT_OUT_PATTERNS:
    if p.search(text5):
        print(f"  OPT-OUT MATCH: {p.pattern}")
for p in _NOT_INTERESTED_PATTERNS:
    if p.search(text5):
        print(f"  NI MATCH: {p.pattern}")
        break
