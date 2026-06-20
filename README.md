# Texting Audit Automation

An advanced, high-performance automated auditing system for SMS/texting conversations. Scrapes SmarterContact via GraphQL and REST APIs, evaluates agent performance using Groq AI (Llama 3.3 70B), and uses a **4-Tier ML Pre-Filter** to cut API costs by skipping clean chats.

> **Performance**: ~2,000 conversations scraped and fully audited in under 5 minutes — powered by async parallel workers (up to 20 concurrent) and the ML pre-filter skipping clean chats before they reach Groq.

## Key Features

- **API-Driven Scraper**: Pure HTTP client (HTTPX) targeting SmarterContact's GraphQL and REST APIs. No browser required. Firebase token auto-refreshes on expiry.
- **AI-Driven Auditing**: Full-text analysis against 4 metrics:
  - **Compliance**: Opt-out/STOP respect, kid-DNC rule (bare "I'm 15" detection), Wrong Number handling.
  - **Attitude**: Agent warmth, condescension detection, price-disagreement label guards.
  - **Professionalism**: Incoherent messages, name errors, bluffer guard (full-value stance = negotiation, not bluffing).
  - **Script Adherence**: 3-rebuttal playbook, 4-pillar qualification, WF hand-raise validation, F14 no-handoff flag.
- **4-Tier ML Pre-Filter**: Cost-saving local pipeline for obviously clean chats:
  - **Tier 1**: Keyword/phrase scanning.
  - **Tier 2**: kNN similarity matching (FAISS, 911+ examples).
  - **Tier 3**: Logistic Regression pattern predictor.
  - **Tier 4**: Groq AI fallback (full audit).
- **Shared AI Key Pool**: LRU load balancing across Groq keys with automatic rotation and cooldown. Up to 140 key attempts before a skip — no conversation dropped due to rate limits.
- **High Throughput**: 20 parallel async workers + ML pre-filter = ~2,000 conversations scraped and audited in under 5 minutes.
- **Performance Dashboard**: Real-time audit scores, red flags, AI provider status, and read-ack "Done" status (clears on account open).

## Architecture

### The Three Funnels

Every conversation is classified before rules are applied:

| Funnel | Description |
|--------|-------------|
| **Wide (WF)** | Initial hello — tone, opt-outs, not quitting after 1 "no", hand-raise push label validation |
| **Middle (MF)** | Nurturing — 1–2 pillars gathered |
| **Narrow (NF)** | Full qualification — all 4 pillars + handoff message sent |

### The Four Pillars

Required for NF / Hot Lead classification:

| Pillar | What the lead must provide |
|--------|---------------------------|
| **Condition** | Property state / needed repairs |
| **Asking Price** | A specific dollar number |
| **Motivation** | Why they are considering selling |
| **Timeline** | When they are ready to sell |

### Scoring Rules (notable)

- **Kid-DNC beats Wrong Number**: bare "I'm 15" (or similar minor statement) triggers DNC regardless of WN label.
- **Bluffer Guard**: agent stating full value as a stance counts as negotiation, not bluffing — prevents false F flags.
- **WF Hand-Raise**: validates "Lead, Pushed to client" push label exists; missing handoff message = F14 flag (−20 script score).
- **Condescension + Price-Disagreement**: label guards prevent false positives when leads argue price.
- **Read-Ack**: "Done" status auto-clears when the account is opened in the dashboard.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Core | Python 3.10+ |
| Scraping | HTTPX (async), Firebase Auth JWT rotation |
| AI Models | Groq Llama 3.3 70B, scikit-learn, FAISS, sentence-transformers |
| Database | PostgreSQL 14+ (asyncpg, pgvector) |
| UI / Dashboard | FastAPI + Jinja2, Vanilla JS, anime.js, Apple/Glass CSS |

## Folder Structure

| Package | Purpose |
|---------|---------|
| `ai/` | Groq analyzer, scorer, dream worker, 4-tier ML pre-filter |
| `config/` | Settings, rate limiter, key pool config |
| `dashboard/` | FastAPI app, HTML templates, static assets |
| `database/` | Postgres schema, migrations, asyncpg helpers |
| `scraper/` | GraphQL/REST API bot (`api_bot.py`), Firebase auth, queue manager |
| `scripts/` | Training, eval, data extraction CLIs |
| `main.py` | CLI entry point for running audits |

## Prerequisites

- Python 3.10+
- PostgreSQL 14+ with `pgvector` extension
- Groq API keys loaded into the `api_keys` table (`provider='groq'`)
- SmarterContact credentials in `.env` (Firebase auth handled automatically)

## Getting Started

1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure environment**:
   ```bash
   cp .env.example .env
   # Fill in DB creds, SmarterContact creds, and Groq keys
   ```

3. **Run a single audit**:
   ```bash
   python main.py --single "AgentName"
   ```

4. **Launch dashboard**:
   ```bash
   python dashboard/app.py
   ```

## ML Pipeline Management

```bash
# Retrain classifier
python -m ai.prefilter.train --test-split 0.2

# Rebuild kNN index
python -m ai.prefilter.index_builder --rebuild

# Evaluate accuracy (gate: FALSE-CLEAN ≤ 5%)
python scripts/eval_prefilter.py --limit 500

# Promote tiers to live (after gate passes)
python scripts/promote_prefilter.py
```

### Prefilter `.env` Flags

```env
PREFILTER_ENABLED=true
PREFILTER_SHADOW_MODE=true      # true = Groq scores everything for validation
PREFILTER_T1_LIVE=true
PREFILTER_T2_LIVE=false
PREFILTER_T3_LIVE=false
```

## Internal Documentation

- `docs/audit_workflow.html` — full system overview
- `docs/ml-prefilter-explained.html` — deep dive into the ML tiers
- `docs/how-the-audit-works.html` — rulebook and scoring guide

---
**Developed by Mohamed Abdo**
