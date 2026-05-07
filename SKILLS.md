# Project Skills Plan

Skills required to complete each phase of the TEXTING AUDIT AUTOMATION project.

---

## Phase 2 — AI Analysis (`ai/` module — currently empty)

| Skill | Purpose |
|-------|---------|
| `claude-api` | Build the Groq AI integration for analyzing conversation transcripts and generating audit scores (compliance, sentiment, professionalism, script adherence, red flags) |

**What to build:**
- `ai/analyzer.py` — calls Groq API with conversation transcripts
- `ai/scorer.py` — maps AI output to numeric scores and writes to `audit_scores` table
- `ai/prompts.py` — prompt templates for audit analysis

---

## Phase 3 — Scoring & Compliance (follows Phase 2)

Pure Python logic, no extra skills needed. Reads AI output and:
- Calculates composite audit scores
- Flags red flags per agent
- Writes scores to `audit_scores` table in SQLite

---

## Phase 4 — Flask Dashboard (`dashboard/templates/` — currently empty)

| Skill | Purpose |
|-------|---------|
| `frontend-ui-dark-ts` | Dark-themed monitoring dashboard with agent score tables, metrics, and conversation drill-downs |
| `tailwind-design-system` | Design tokens, component variants, and responsive layout for the dashboard |
| `product-design` | UX flows — agent list → score breakdown → flagged conversations |
| `ui-visual-validator` | Visual validation after the dashboard is built |

**What to build:**
- `dashboard/templates/index.html` — agent leaderboard / overview
- `dashboard/templates/agent.html` — per-agent score detail + conversation list
- `dashboard/static/` — Tailwind CSS + JS assets
- Flask routes in a new `dashboard/app.py`

---

## Phase 4 — Report Templates (`reports/templates/` — currently empty)

| Skill | Purpose |
|-------|---------|
| `frontend-ui-dark-ts` | Styled Jinja2 HTML templates for PDF/email audit reports |

**What to build:**
- `reports/templates/agent_report.html` — per-agent PDF report
- `reports/templates/summary_report.html` — weekly team summary
- `reports/generator.py` — renders templates and exports to PDF

---

## Code Quality (ongoing after each phase)

| Skill | Purpose |
|-------|---------|
| `simplify` | Review and clean up code after each phase is built |

---

## Build Order

```
1. Phase 2  →  claude-api skill        (AI analysis module)
2. Phase 3  →  no skill needed         (scoring logic, pure Python)
3. Phase 4  →  frontend-ui-dark-ts     (dashboard + report templates)
              tailwind-design-system
              product-design
              ui-visual-validator
4. Ongoing  →  simplify                (code cleanup after each phase)
```
