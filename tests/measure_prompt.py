"""Measure token usage in the system prompt."""
import re

# Simple token estimator (~1.3 tokens per word for English)
def count_tokens(text):
    return int(len(text.split()) * 1.3)

with open("ai/prompts.py", "r", encoding="utf-8") as f:
    content = f.read()

# Extract SYSTEM_PROMPT between triple quotes
match = re.search(r'SYSTEM_PROMPT\s*=\s*"""(.*?)"""', content, re.DOTALL)
if not match:
    print("Could not find SYSTEM_PROMPT")
    exit(1)

system_prompt = match.group(1)
total = count_tokens(system_prompt)
print(f"SYSTEM_PROMPT total tokens: {total}")
print(f"Characters: {len(system_prompt)}")
print()

# Count by PART sections
parts = re.split(r"(## PART \d+)", system_prompt)
current_title = "PREAMBLE"
for chunk in parts:
    if chunk.startswith("## PART"):
        current_title = chunk
        continue
    t = count_tokens(chunk)
    if t > 5:
        first_line = chunk.strip().split("\n")[0][:50]
        print(f"  {current_title:35s} {t:>5} tokens | {first_line}")

print()

# Redundancy analysis
lines = system_prompt.split("\n")
never_lines = [l for l in lines if "NEVER" in l]
print(f"Lines containing 'NEVER': {len(never_lines)}")

continued = [l.strip() for l in lines if any(x in l.lower() for x in [
    "continued messaging", "continued contacting", "kept messaging",
    "persisted after", "continued to message", "stopped messaging",
    "failed to stop messaging",
])]
print(f"Lines about 'continued messaging' rule: {len(continued)}")

rebuttal = [l.strip() for l in lines if any(x in l.lower() for x in [
    "rebuttal sequence", "rebuttal order", "skipped rebuttal",
    "rebuttals were out of order", "proper rebuttal",
])]
print(f"Lines about 'rebuttal sequence' rule: {len(rebuttal)}")

doubt = [l.strip() for l in lines if any(x in l.lower() for x in [
    "when in doubt", "default to no flag", "borderline", "default to passing",
])]
print(f"Lines about 'when in doubt / borderline' rule: {len(doubt)}")

texter_only = [l.strip() for l in lines if "texter" in l.lower() and "only" in l.lower()]
print(f"Lines about 'texter-actions-only': {len(texter_only)}")

# Part 12 duplication check
part12_count = system_prompt.count("## PART 12")
print(f"\nPART 12 (output format) appears: {part12_count} time(s) in SYSTEM_PROMPT")

# Learned rules section size
part13_start = system_prompt.find("## PART 13")
if part13_start >= 0:
    part13 = system_prompt[part13_start:]
    part13_tokens = count_tokens(part13)
    print(f"PART 13 (Learned Corrections) size: {part13_tokens} tokens")
    rules = re.findall(r"RULE \d+", part13)
    print(f"Number of learned rules: {len(rules)}")

# Cost projection
print("\n--- COST PROJECTION ---")
convos_per_run = 500  # 10 texters x 50 convos
output_tokens = 400
runs_per_day = 4
days_per_month = 22

input_per_call = total + 650  # system prompt + transcript
daily_input = convos_per_run * runs_per_day * input_per_call
daily_output = convos_per_run * runs_per_day * output_tokens
monthly_input = daily_input * days_per_month
monthly_output = daily_output * days_per_month

# Groq llama-3.3-70b pricing
cost_in = monthly_input / 1_000_000 * 0.59
cost_out = monthly_output / 1_000_000 * 0.79
print(f"Input tokens/call: {input_per_call}")
print(f"Monthly input tokens: {monthly_input:,}")
print(f"Monthly output tokens: {monthly_output:,}")
print(f"Monthly cost (70b): ${cost_in + cost_out:.2f}")
