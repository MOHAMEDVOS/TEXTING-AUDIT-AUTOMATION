import json
import sys
from pathlib import Path

log_path = Path(r"C:\Users\vos\.gemini\antigravity\brain\8aa4924d-200a-432e-9e4b-e2622710a0e9\.system_generated\logs\transcript.jsonl")
if not log_path.exists():
    print(f"Error: Log file not found at {log_path}")
    sys.exit(1)

recovered = {}

with open(log_path, "r", encoding="utf-8") as f:
    for line in f:
        try:
            data = json.loads(line)
        except Exception:
            continue
        
        # Check tool calls
        tool_calls = data.get("tool_calls", [])
        if not tool_calls:
            continue
            
        for call in tool_calls:
            if call.get("name") == "write_to_file":
                args = call.get("args", {})
                target = args.get("TargetFile")
                code = args.get("CodeContent")
                if target and code:
                    # Clean target path string (e.g. remove quotes if any)
                    target_str = target.strip('"\'')
                    # Keep the latest content for each target file
                    if any(name in target_str for name in ["api_bot.py", "firebase_auth.py", "gql_client.py"]):
                        recovered[target_str] = code

# Write the recovered files
for path_str, content in recovered.items():
    p = Path(path_str)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    print(f"Successfully recovered: {p} ({len(content)} characters)")

print("Recovery finished.")
