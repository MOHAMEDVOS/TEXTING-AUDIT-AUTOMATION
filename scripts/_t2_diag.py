"""Quick diagnostic: show T2 neighbor details for the 20 conversations T1 passed."""
import sys, json
sys.path.insert(0, '.')

import numpy as np
from ai.prefilter import embedder, tier2_embedding
from config import settings

# Load eval data
with open('scripts/eval_50_conversations.json') as f:
    convs = json.load(f)

# Load baseline to know which ones T1 passed
with open('scripts/eval_baseline.json') as f:
    baseline = {b['conversation_id']: b for b in json.load(f)}

# Force load
tier2_embedding._load_index()
index = tier2_embedding._index
meta = tier2_embedding._index_meta

print(f"Index: {index.ntotal} vectors, {sum(1 for m in meta if m['is_clean'])} clean, {sum(1 for m in meta if not m['is_clean'])} flagged")
print(f"Threshold: {settings.PREFILTER_T2_SIM_THRESHOLD}")
print()

# Run T1 to find which ones pass through
from ai.prefilter.tier1_phrases import evaluate as t1_eval

for c in convs:
    t1 = t1_eval(c['messages'], c['account_name'], c['contact_name'])
    if t1 is not None:
        continue  # T1 handled it
    
    cid = c['conversation_id']
    name = c['contact_name']
    bl = baseline.get(cid, {}).get('outcome', '?')
    
    # Embed and search
    text = embedder.conversation_to_text(c['messages'], c['account_name'])
    vec = embedder.embed(text)
    if vec is None:
        continue
    
    query = np.asarray([vec], dtype=np.float32)
    sims, idxs = index.search(query, 5)
    
    print(f"[{cid}] {name:30s} baseline={bl:15s}")
    for sim, idx in zip(sims[0], idxs[0]):
        if idx < 0 or idx >= len(meta):
            continue
        m = meta[idx]
        clean = "CLEAN" if m['is_clean'] else "FLAGD"
        print(f"    sim={sim:.4f}  conv={m['conversation_id']}  {clean}  scores={m['scores']}")
    print()
