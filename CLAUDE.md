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
- `config/`: Settings and rate-limiter config (`settings.py`, `rate_limiter.py`). No LLM key pool — that table is gone.
- `dashboard/`: FastAPI app, HTML templates, and static assets
- `database/`: Schema definitions and DB helper modules
- `docs/`: Technical guides (audit workflow, scoring rulebook)
- `scraper/`: GraphQL & REST API client scraper and queue manager
- `scripts/`: operational utilities (account import, assignment backfill, post-audit reflection, reporting). ML training/eval scripts live under `ai/prefilter/` (`index_builder.py`, `train.py`, `shadow_harness.py`), not here.
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
- **Response Format**: New endpoints should return `{"success": true, "data": {...}}` or `{"success": false, "error": "msg"}`. In practice `dashboard/app.py` has a sizable minority of endpoints (control/admin/validation actions) that return `{"status": "ok", ...}`-shaped bodies instead — match the convention of the endpoint you're editing rather than assuming one format project-wide.
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
-   **Shift-aware timing**: the team is staffed **10:00 AM – 7:00 PM ET, Mon–Fri**
    (`SHIFT_START_HOUR` / `SHIFT_END_HOUR` / `SHIFT_DAYS` in `config/settings.py`).
    Every elapsed-time rule measures **shift minutes** via `ai/shift.py`, never
    wall-clock minutes, so an overnight or weekend pause can never raise **F17**
    (slow response). Thresholds (10/15/25 min) are unchanged — only the
    denominator is. `ai/shift.py` is the single definition of the window; do not
    reintroduce a local business-hours constant.

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

**Caution:** T2/T3 committed artifacts were trained on scikit-learn 1.8.0
(`ai/prefilter/artifacts/manifest.json` stamps `sklearn_version: 1.8.0`) while
`requirements.txt` pins 1.7.2 — `joblib.load()` throws `InconsistentVersionWarning`
for every estimator under the installed version. Do not enable T2/T3 without
retraining under 1.7.2. The embedding text builder's agent/lead blindness (deep
review F10) was fixed in commit c901424 — `ai/prefilter/embedder.py` and
`ai/prefilter/index_builder.py` now tag each message `AGENT:`/`CONTACT:` based on
`sender`, not a broken heuristic that always resolved to `CONTACT`.

**Operational Commands:**
-   **Rebuild kNN Index**: `python -m ai.prefilter.index_builder --rebuild`
-   **Retrain Classifier**: `python -m ai.prefilter.train --test-split 0.2`
-   **T4 vs Groq-history comparison**: `python -m ai.prefilter.shadow_harness --limit 50 [--csv path]`
    (there is no live Groq to compare against anymore; this replays stored
    `groq_scores` ground truth from `prefilter_decisions`/`conversation_scores`).
    `scripts/eval_prefilter.py` and `scripts/promote_prefilter.py`, referenced by
    older docs, no longer exist in `scripts/`.

**Env Config (`.env`):**
-   `PREFILTER_ENABLED=true`
-   `PREFILTER_SHADOW_MODE=false` (default; when true, tiers run without acting on
    them, for validation against stored ground truth)
-   `PREFILTER_T1_LIVE=true`, `PREFILTER_T2_LIVE=false`, `PREFILTER_T3_LIVE=false`, `PREFILTER_T4_LIVE=true`
-   `PREFILTER_REQUIRE_VALIDATION=false` — when true, Tier 2's kNN index only draws
    from conversations with a `validation_log.status='valid'` row; stays false
    until enough manager validations exist to not empty the index.

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

Because these rules produce all output, treat any change to them as a change to
the product's results. Test coverage is thin and partial — `tests/test_flag_split.py`
and `tests/test_response_time.py` pin down `tier4_flag_generator.py`'s culprit
splitting and `ai/response_time.py`/`ai/shift.py`, and `tests/test_scorer_filters.py`
covers flag suppression in `ai/scorer.py`, but `tier1_phrases_v2.py` and
`label_validator.py` (the bulk of the rule volume) have no dedicated tests.
Capture golden-transcript output before editing and diff after.

**Known review findings** are tracked in `docs/reviews/2026-08-25-deep-code-review.md`.

---

## Flag Validation Gate

A flag reaches the Trend Dashboard, the Detailed Dashboard, or an agent's
history **only** after an auditor clicks "Mark Valid" on that conversation.
This is enforced by `validation_log` (`database/schema.sql`) and
`POST /api/redflag/valid` (`dashboard/app.py`) — the endpoint writes a
`status='valid'` row keyed on `(agent_id, LOWER(contact_name))` and then calls
`_recompute_trend_counts` to fold the change into that day's `trend_snapshots`
row. It never touches `conversation_scores`/`audit_scores`, so raw audit output
stays intact and a validation can be toggled back off (row deleted, counts
recomputed). This replaced the old model where a flag was valid by default and
a dismissal permanently deleted it; a one-time backfill in `schema.sql`
auto-validated everything audited before the 2026-08-27 cutover date so
historical counts didn't shift, but anything audited on/after that date
requires a manual click.

`trend_snapshots` carries two flag-specific rollup columns —
`late_response_flags` and `wrong_label_flags` — computed only from validated
conversations, same as the other counts on that table.


