import sys, json
sys.path.insert(0, r"c:\Users\vos\Desktop\TEXTING AUDIT AUTOMATION")

from ai.prefilter import tier1_phrases

with open(r"c:\Users\vos\Desktop\TEXTING AUDIT AUTOMATION\scripts\eval_50_conversations.json") as f:
    convs = json.load(f)

# Sample the first 10 that T1 short-circuited
for conv in convs[:25]:
    result = tier1_phrases.evaluate(conv["messages"], conv["account_name"], conv["contact_name"])
    if result and result.decision == "short_circuit":
        print(f"[{conv['conversation_id']}] {conv['contact_name'][:25]:25} decision={result.decision} conf={result.confidence:.2f} notes={result.notes!r}")
        if hasattr(result, 'predicted_scores'):
            print(f"  predicted_scores={result.predicted_scores}")
        print(f"  result_dict_keys={list(result.result.keys()) if result.result else None}")
        break  # just need 1 example
