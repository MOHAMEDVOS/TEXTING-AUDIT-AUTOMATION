# Texting Audit Automation — Project Context

## Project Overview
An advanced, high-performance automated auditing system for SMS/Texting conversations.
- **Target**: Scrapes SmarterContact conversations (via GraphQL and REST APIs).
- **Audit**: Evaluates agents against 4 metrics: Compliance, Attitude, Professionalism, and Script Adherence.
- **Scoring**: **ML-only. There is no LLM in the pipeline.** Groq was decommissioned;
  `ai/analyzer.py` makes no API calls and `groq` is not a dependency. Tier 4 is a
  deterministic rule generator, not an AI fallback.
- **Tech Stack**: Python 3.10+, FastAPI, PostgreSQL, scikit-learn/FAISS (local only).

---

## Core Rules
- **API Extraction**: SmarterContact data is fetched directly using HTTPX via GraphQL and REST API endpoints. This is robust, fast, and does not require a browser.
- **Firebase Auth Rotation**: The Firebase access token is automatically refreshed using `scraper/firebase_auth.py` when it expires.
- **No AI keys**: there is no LLM key pool. The `api_keys` table is dead and is no
  longer created on fresh installs.
- **ML Gates**: Prefilter promotion requires FALSE-CLEAN ≤ 5%. NOTE: the
  `prefilter_decisions` telemetry table was missing from `schema.sql` until the
  2026-08-25 review, so historic gate numbers were computed from no data.
- **Documentation**: Keep Obsidian Brain (`C:\Users\vos\Desktop\obsidian_brain`) updated with verified API formats and known gotchas.

---

## Tech Stack
- **Backend**: Python 3.10+, FastAPI, uvicorn
- **Database**: PostgreSQL (asyncpg, pgvector), SQLite (for some local caching)
- **Scraping**: GraphQL / REST API Bot (pure HTTP request client via `httpx`)
- **AI Models**: Sentence-Transformers (local), FAISS, scikit-learn. No hosted LLM.
- **Frontend**: FastAPI + Jinja2, Vanilla JS, anime.js, Apple-inspired Custom CSS

---

## Folder Structure
- `ai/`: Scorer and 3-Tier ML Pre-filter logic (`prefilter/`)
- `config/`: Settings, API key pools, and agent roster configs
- `dashboard/`: FastAPI app, HTML templates, and static assets
- `database/`: Schema definitions and DB helper modules
- `docs/`: Technical guides (audit workflow, scoring rulebook)
- `scraper/`: GraphQL & REST API client scraper and queue manager
- `scripts/`: ML training, evaluation, and system utilities
- `main.py`: Main CLI entry point for running audits

---

## Coding Standards

### Python
- Use strict typing where possible (type hints).
- Prefer `async`/`await` for all I/O, database, and browser operations.
- No external LLM calls — scoring is local and deterministic, so there are no API
  rate limits to handle in the scoring path.
- Log errors using the project's standard logger (`logging.basicConfig` level INFO).

### Frontend (Dashboard)
- Use **Vanilla JS** for interactivity; avoid adding heavy frameworks.
- Styling is **Pure CSS** using custom design tokens (Apple/Glassmorphism theme).
- Keep `index.html` logic modular; use DOM-based event listeners.
- Use `anime.js` for all micro-animations and transitions.

### Styling
- **Dark Mode First**: Default theme is Dark (Black/Electric Blue).
- **Design Tokens**: Use CSS variables for colors and spacing (e.g., `--brand`, `--surface`).
- **No Inline Styles**: Move complex styles to the `<style>` block.

---

## Naming Conventions

### Files & Folders
- **Python**: `snake_case.py`
- **Frontend**: `kebab-case.html`, `snake_case.js`
- **Folders**: `snake_case/`

### Code
- **Variables/Functions**: `snake_case` (Python/JS)
- **Classes**: `PascalCase`
- **Constants**: `UPPER_SNAKE_CASE`

---

## API Rules
- **FastAPI Endpoints**: Use standard REST verbs (GET for data, POST for actions, DELETE for resets).
- **Validation**: Use Pydantic models for request bodies.
- **Response Format**: Consistent JSON returns: `{"success": true, "data": {...}}` or `{"success": false, "error": "msg"}`.
- **Error Handling**: Use `HTTPException` with appropriate status codes (404, 400, 500).

---

## SmarterContact API Client
- **Auth Service**: `scraper/firebase_auth.py` authenticates agent credentials against the Firebase Auth REST API (`identitytoolkit.googleapis.com`) to fetch `idToken` (JWT) and `refreshToken`.
- **GraphQL Client**: `scraper/gql_client.py` constructs and executes requests to the SmarterContact backend.
- **API Bot**: `scraper/api_bot.py` uses the GraphQL client to pull all chats, details, and transcripts, and normalizes them into the exact output format expected by the DB and ML scorer.
---

## Audit Architecture (Three Funnels & Four Pillars)

The system classifies every conversation into a **Funnel** type to apply relevant rules:
1.  **Wide Funnel (WF - The Hello)**: Focus on tone, opt-outs, and not giving up after 1 'no'.
2.  **Middle Funnel (MF - The Nurture)**: 1-2 pillars gathered.
3.  **Narrow Funnel (NF - The Qualify)**: All 4 pillars gathered + handoff msg sent.

**The Four Pillars (Required for NF/Hot Leads):**
-   **Condition**: Lead describes property state/repairs.
-   **Asking Price**: Lead provides a specific dollar number.
-   **Motivation**: Lead explains *why* they are considering selling.
-   **Timeline**: Lead states a timeframe for selling.

**Notable Scoring Rules (implemented):**
-   **Kid-DNC > Wrong Number**: bare "I'm 15" (minor) triggers DNC regardless of WN label.
-   **Bluffer Guard**: agent stating full value as a stance = negotiation, not bluffing. Prevents false F flags.
-   **WF Hand-Raise**: validates "Lead, Pushed to client" push label; missing handoff msg = **F16** flag. (F14 is the *address-denial* flag — don't confuse the two.)
-   **Condescension + Price-Disagreement guards**: label checks prevent false positives when leads argue price.
-   **Read-Ack**: "Done" status auto-clears when the account is opened in the dashboard.

---

## ML Pre-Filter Pipeline

Originally built to reduce Groq API costs by handling "clean" conversations
locally. Groq is gone, so T2/T3 no longer save anything — see the 2026-08-25
review for the recommendation to either repair or remove them.
-   **Tier 1 (Phrase Matching)**: Instant catch for silent contacts or trivial opt-outs. LIVE.
-   **Tier 2 (kNN Embedding)**: FAISS index. Built but DISABLED in production.
-   **Tier 3 (Classifier)**: Logistic regression. Built but DISABLED in production.
-   **Tier 4 (Deterministic rule generator)**: the terminal tier. Since Groq was
    removed this produces effectively **100% of the product's output** — see
    `ai/prefilter/tier4_flag_generator.py` and `tier1_phrases_v2.py`.

**Caution:** T2/T3 committed artifacts were trained on scikit-learn 1.8.0 while
`requirements.txt` pins 1.7.2, and the embedding text builder cannot distinguish
agent from lead. Do not enable T2/T3 without rebuilding both.

**Operational Commands:**
-   **Run Evaluation**: `python scripts/eval_prefilter.py --limit 500`
-   **Promote Tiers**: `python scripts/promote_prefilter.py` (checks gates: FALSE-CLEAN ≤ 5%)
-   **Rebuild kNN Index**: `python -m ai.prefilter.index_builder --rebuild`
-   **Retrain Classifier**: `python -m ai.prefilter.train --test-split 0.2`

**Env Config (`.env`):**
-   `PREFILTER_ENABLED=true`
-   `PREFILTER_SHADOW_MODE=true` (historically: run tiers without acting on them,
    for validation against the then-available ground truth)
-   `PREFILTER_T1_LIVE=true`, `PREFILTER_T2_LIVE=false`, `PREFILTER_T3_LIVE=false`

---

## Knowledge Base (Obsidian)

**Vault:** `C:\Users\vos\Desktop\obsidian_brain`
**Project doc:** `01-projects/TEXTING AUDIT AUTOMATION.md`

When significant changes happen, update the vault:
- New selector verified → update the Verified Selectors table in project doc
- SmarterContact UI changes → update selectors + note the date verified
- Bug or gotcha discovered → add to `03-decisions/Known Gotchas.md`
- New runbook needed → create in `04-how-to/`
- Session log → append to the **Session Log** section in the project doc

---

## Scoring Engine (updated 2026-08-25)

**There is no AI key pool.** Groq was decommissioned; `ai/analyzer.py` opens with
"ML-only conversation analyzer. No LLM calls" and `ai/prompts.py` records that the
prompt builder was removed with it. The previous "AI Key Pool Model" section
described a subsystem that no longer exists and is deleted.

Scoring is fully deterministic:
- `ai/prefilter/tier1_phrases_v2.py` — phrase and pattern rules (LIVE)
- `ai/prefilter/label_validator.py` — label correctness rules
- `ai/prefilter/tier4_flag_generator.py` — terminal flag generation

Because these rules produce all output and there are no tests over them, treat any
change to them as a change to the product's results. Capture golden-transcript
output before editing and diff after.

**Known review findings** are tracked in `docs/reviews/2026-08-25-deep-code-review.md`.


