# Texting Audit Automation — Project Context

## Project Overview
An advanced, high-performance automated auditing system for SMS/Texting conversations.
- **Target**: Scrapes SmarterContact (React SPA) conversations.
- **Audit**: Evaluates agents against 4 metrics: Compliance, Attitude, Professionalism, and Script Adherence.
- **Efficiency**: Uses a **3-Tier ML Pre-Filter** (Keywords, kNN Similarity, Logistic Regression) to reduce Groq AI costs by skipping "clean" chats.
- **Tech Stack**: Python 3.10+, Playwright, Groq (Llama 3.3 70B), FastAPI, PostgreSQL.

---

## Core Rules
- **Playwright Navigation**: Never use `networkidle` or `wait_for_load_state("networkidle")` — SmarterContact is an SPA that keeps connections open indefinitely. Use `wait_until="load"` or poll for specific URL changes/selectors.
- **Login Persistence**: After clicking Login, poll URL for `/login` removal. Do NOT check `_is_logged_in()` immediately (React takes 2-3s to render).
- **AI Key Pool**: Groq keys are in `config/groq_keys.json` (flat list). Dedicated NIM keys (provider: "nim") are in `config/agent_keys.json`. Non-NIM entries in `agent_keys.json` are ignored.
- **ML Gates**: Prefilter promotion requires FALSE-CLEAN ≤ 5%.
- **Documentation**: Keep Obsidian Brain (`C:\Users\vos\Desktop\obsidian_brain`) updated with verified selectors and known gotchas.

---

## Tech Stack
- **Backend**: Python 3.10+, FastAPI, uvicorn
- **Database**: PostgreSQL (asyncpg, pgvector), SQLite (for some local caching)
- **Scraping**: Playwright (Async)
- **AI Models**: Groq (Llama 3.3 70B), Sentence-Transformers (Local), FAISS, XGBoost
- **Frontend**: FastAPI + Jinja2, Vanilla JS, anime.js, Apple-inspired Custom CSS

---

## Folder Structure
- `ai/`: Scorer and 3-Tier ML Pre-filter logic (`prefilter/`)
- `config/`: Settings, API key pools, and agent roster configs
- `dashboard/`: FastAPI app, HTML templates, and static assets
- `database/`: Schema definitions and DB helper modules
- `docs/`: Technical guides (audit workflow, scoring rulebook)
- `scraper/`: Playwright automation and browser-bot logic
- `scripts/`: ML training, evaluation, and system utilities
- `main.py`: Main CLI entry point for running audits

---

## Coding Standards

### Python
- Use strict typing where possible (type hints).
- Prefer `async`/`await` for all I/O, database, and browser operations.
- Handle Groq rate limits using the built-in LRU KeyPoolManager rotation.
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

## SmarterContact Selectors (April 2026)
| Element | Selector |
|---|---|
| Email input on login | `input#email` |
| Password input | `input#password` |
| TOS checkbox | `label.chakra-checkbox` (fallback: `span.chakra-checkbox__control`) |
| Login button | `button:has-text("Log in")` |
| Chat panel | `div[data-test-id="messenger_nav_inbox_all_contact-panel_messages"]` |
| Messages inside panel | `p.chakra-text` inside the panel |
| Conversation rows | `[data-test-class='messenger_nav_inbox_all_messages_row']` |
| Virtualized list | `div.ReactVirtualized__Grid__innerScrollContainer` |
| Message Bubbles | `p.chakra-text` inside the chat panel |
| Inbox Row | `[data-test-class='messenger_nav_inbox_all_messages_row']` |

---

## Do NOT do these things

- Do NOT use `wait_until="networkidle"` anywhere — SPAs keep connections open, it always times out
- Do NOT use `wait_for_load_state("networkidle")` after login button click
- Do NOT check `_is_logged_in()` immediately after clicking Login — React takes 2-3s to render
- Do NOT wait on selectors after login before navigating to messenger — page is blank
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

---

## ML Pre-Filter Pipeline

Reduces Groq API costs by handling "clean" conversations locally.
-   **Tier 1 (Phrase Matching)**: Instant catch for silent contacts or trivial opt-outs.
-   **Tier 2 (kNN Embedding)**: Matches against 911+ past clean conversations (FAISS index).
-   **Tier 3 (Classifier)**: Logistic regression predicts P(flag) and audit scores.
-   **Tier 4 (Groq AI)**: Full audit fallback if all tiers are uncertain.

**Operational Commands:**
-   **Run Evaluation**: `python scripts/eval_prefilter.py --limit 500`
-   **Promote Tiers**: `python scripts/promote_prefilter.py` (checks gates: FALSE-CLEAN ≤ 5%)
-   **Rebuild kNN Index**: `python -m ai.prefilter.index_builder --rebuild`
-   **Retrain Classifier**: `python -m ai.prefilter.train --test-split 0.2`

**Env Config (`.env`):**
-   `PREFILTER_ENABLED=true`
-   `PREFILTER_SHADOW_MODE=true` (True = Groq scores everything for validation)
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

## AI Key Pool Model (April 2026)

**Two stores in `ai.analyzer.KeyPoolManager`:**

1. **Groq shared pool** — `config/groq_keys.json` (flat list of key strings).
   Every agent NOT listed in `agent_keys.json` as a NIM entry uses this pool.
   Selection is LRU; rate-limited keys rotate automatically; quota-exhausted
   keys are permanently removed from rotation.

2. **NIM dedicated keys** — `config/agent_keys.json` (only `provider: "nim"` entries).
   Each NIM agent has its own key. Non-NIM entries here are ignored (logged warning).

**Guarantee:** No conversation is skipped due to rate limits. The Groq pool
cycles up to 10 times (≈140 key attempts) before giving up. A skip only
happens when the model returns malformed JSON — that is a data issue, not
a key issue.

**Do NOT:**
- Put Groq keys in `agent_keys.json` — they'll be logged as warnings and ignored
- Use `networkidle` waits anywhere (existing rule — SPA login)
