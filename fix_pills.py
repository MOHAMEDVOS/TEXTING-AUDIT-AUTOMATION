import re
import sys

path = "dashboard/templates/index.html"
with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# Fix 1: ⚑ (U+2691 BLACK FLAG) -> &#9873;
c1 = content.count("\u2691")
content = content.replace("\u2691", "&#9873;")
sys.stdout.buffer.write(f"Fix 1 flag glyph: replaced {c1} occurrences\n".encode("utf-8"))

# Fix 2: ⚠ (U+26A0 WARNING) -> &#9888;
c2 = content.count("\u26a0")
content = content.replace("\u26a0", "&#9888;")
sys.stdout.buffer.write(f"Fix 2 warning glyph: replaced {c2} occurrences\n".encode("utf-8"))

with open(path, "w", encoding="utf-8") as f:
    f.write(content)

sys.stdout.buffer.write(b"Done.\n")
