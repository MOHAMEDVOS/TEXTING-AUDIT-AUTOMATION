from __future__ import annotations

import re
from pathlib import Path


def main() -> None:
    p = Path("ai/prompts.py")
    s = p.read_text(encoding="utf-8")
    m = re.search(r'SYSTEM_PROMPT\s*=\s*"""(.*?)"""', s, re.S)
    if not m:
        print("SYSTEM_PROMPT not found")
        return
    prompt = m.group(1)
    print(f"prompt_lines={len(prompt.splitlines())}")
    print(f"file_lines={sum(1 for _ in p.open(encoding='utf-8'))}")


if __name__ == "__main__":
    main()
