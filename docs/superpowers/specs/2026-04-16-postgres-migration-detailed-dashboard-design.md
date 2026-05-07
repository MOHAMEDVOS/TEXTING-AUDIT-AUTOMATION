# PostgreSQL Migration + Detailed Dashboard — Design Spec
**Date:** 2026-04-16  
**Status:** Approved  

---

## Problem

1. **SQLite limits** — the system uses SQLite (1.9 MB) for all data. Conversations are stored as JSON blobs inside `extractions.conversations_data`, making it impossible to query individual messages or flagged conversations efficiently. No vector search capability exists.

2. **Historical data is lost** — when the user resets the main dashboard for the next day's audit, the per-conversation results (red flags, AI analysis, full message threads) are wiped from the UI. There is no way to go back and review what happened on a specific day.

3. **No drill-down on issues** — the Trends section shows summaries like "Amna — 7 issues — April 14" but there is no way to see which 7 conversations were flagged, what the red flags were, or read the original messages.

---

## Goal

1. **Migrate from SQLite to PostgreSQL** with pgvector extension installed (ready for future embedding/vector search, not used yet).

2. **Normalize conversation data** — break JSON blobs into proper relational tables: `contacts`, `conversations`, `messages`. Each conversation tied to both the email account and the texter name.

3. **Permanent audit results** — when the audit runs, results are saved to Postgres immediately and permanently. Reset only clears the main dashboard UI, not the underlying data.

4. **Detailed Dashboard** — a new section under the Trends sidebar that lets the user drill into historical flagged conversations for any texter across any date range.

---

## Scope

### Sub-project 1: PostgreSQL Migration
- New PostgreSQL schema with normalized tables
- Rewrite `database/db.py` for asyncpg
- Update all queries in `dashboard/app.py` and `ai/scorer.py`
- Replace `aiosqlite` with `asyncpg` in requirements
- Install pgvector extension, add `embedding VECTOR(1536)` column to messages (nullable, unused)
- `DATABASE_URL` env var for local + Railway

### Sub-project 2: Detailed Dashboard
- New sidebar sub-item under Trends: "Detailed Dashboard"
- Filters: start date (required), end date (required), agent name (required)
- Results: list of flagged conversations only (conversations with red flags)
- Each row: contact name, labels, preview snippet, issue count, score, date
- Click row: opens the existing chat detail view (AI analysis + full conversation thread)
- Data comes from the permanent Postgres tables, never affected by reset

---

## Database Schema

### PostgreSQL connection

```
DATABASE_URL=postgresql://user:password@localhost:5432/texting_audit
```

Configured in `.env`, read by `config/settings.py`. Works the same on Railway (just a different connection string).

### Tables

#### `agents` (existing, adapted)
```sql
CREATE TABLE agents (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

#### `contacts` (new)
```sql
CREATE TABLE contacts (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    phone_number TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```
- One row per unique lead/prospect
- `phone_number` nullable for now (not currently extracted)
- Deduplicated by name (upsert on insert)

#### `conversations` (new — replaces JSON blob in extractions)
```sql
CREATE TABLE conversations (
    id SERIAL PRIMARY KEY,
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    texter_name TEXT NOT NULL,
    assigned_labels TEXT[],
    extracted_at TIMESTAMPTZ NOT NULL,
    audit_date DATE NOT NULL
);
CREATE INDEX idx_conversations_agent ON conversations(agent_id);
CREATE INDEX idx_conversations_texter ON conversations(texter_name);
CREATE INDEX idx_conversations_date ON conversations(audit_date);
CREATE INDEX idx_conversations_contact ON conversations(contact_id);
```
- `agent_id` = the email account used for extraction
- `texter_name` = the real person working that account
- `audit_date` = the date of the audit run (for filtering in Detailed Dashboard)
- `assigned_labels` = Postgres array, e.g. `{'Warm','AP drip'}`

#### `messages` (new — normalized from JSON)
```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE messages (
    id SERIAL PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    sender TEXT NOT NULL,
    body TEXT NOT NULL,
    sent_at TIMESTAMPTZ,
    embedding VECTOR(1536)
);
CREATE INDEX idx_messages_conversation ON messages(conversation_id);
```
- `sender` = 'agent' or 'lead'
- `embedding` = pgvector column, nullable, unused for now (future-proofing)
- `ON DELETE CASCADE` so deleting a conversation cleans up its messages

#### `extractions` (existing, simplified)
```sql
CREATE TABLE extractions (
    id SERIAL PRIMARY KEY,
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    extracted_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL,
    reporting_data JSONB,
    page_text TEXT,
    errors JSONB
);
CREATE INDEX idx_extractions_agent ON extractions(agent_id);
CREATE INDEX idx_extractions_date ON extractions(extracted_at);
```
- `conversations_data` column removed — data now in `conversations` + `messages` tables
- Keeps extraction metadata (status, errors, page_text for debugging)

#### `audit_scores` (existing, adapted)
```sql
CREATE TABLE audit_scores (
    id SERIAL PRIMARY KEY,
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    audit_date DATE NOT NULL,
    overall_score DOUBLE PRECISION,
    compliance_score DOUBLE PRECISION,
    sentiment_score DOUBLE PRECISION,
    professionalism_score DOUBLE PRECISION,
    response_time_score DOUBLE PRECISION,
    script_adherence_score DOUBLE PRECISION,
    red_flags JSONB,
    details JSONB
);
CREATE INDEX idx_scores_agent ON audit_scores(agent_id);
CREATE INDEX idx_scores_date ON audit_scores(audit_date);
```

#### `conversation_scores` (new — per-conversation AI analysis, permanent)
```sql
CREATE TABLE conversation_scores (
    id SERIAL PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    compliance_score DOUBLE PRECISION,
    sentiment_score DOUBLE PRECISION,
    professionalism_score DOUBLE PRECISION,
    script_adherence_score DOUBLE PRECISION,
    funnel_stage TEXT,
    pillars_gathered TEXT[],
    rebuttals_used TEXT[],
    label_assigned TEXT,
    label_correct BOOLEAN,
    label_should_be TEXT,
    label_reason TEXT,
    red_flags JSONB,
    actions_triggered TEXT[],
    summary TEXT,
    model_used TEXT,
    scored_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_conv_scores_conversation ON conversation_scores(conversation_id);
CREATE INDEX idx_conv_scores_red_flags ON conversation_scores USING GIN(red_flags);
```
- One row per conversation per audit run
- `red_flags` as JSONB with GIN index for querying "conversations that had flags"
- This is the table that powers the Detailed Dashboard
- Data is permanent — never deleted by reset

#### `audited_chats` (existing, adapted)
```sql
CREATE TABLE audited_chats (
    id SERIAL PRIMARY KEY,
    agent_email TEXT NOT NULL,
    contact_name TEXT NOT NULL,
    message_preview TEXT NOT NULL,
    audited_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(agent_email, contact_name)
);
```

#### `flag_feedback` (existing, adapted)
```sql
CREATE TABLE flag_feedback (
    id SERIAL PRIMARY KEY,
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    agent_name TEXT NOT NULL,
    contact_name TEXT NOT NULL,
    red_flag TEXT NOT NULL,
    evidence TEXT,
    status TEXT NOT NULL DEFAULT 'invalid',
    reason TEXT,
    category TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_flag_feedback_agent ON flag_feedback(agent_id);
CREATE INDEX idx_flag_feedback_flag ON flag_feedback(red_flag);
```

#### `session_events` (existing, adapted)
```sql
CREATE TABLE session_events (
    id SERIAL PRIMARY KEY,
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    agent_name TEXT NOT NULL,
    conversations_scored INTEGER DEFAULT 0,
    flags_generated INTEGER DEFAULT 0,
    model_used TEXT,
    run_timestamp TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_session_events_ts ON session_events(run_timestamp);
CREATE INDEX idx_session_events_agent ON session_events(agent_id);
```

#### `account_assignments` (existing, adapted)
```sql
CREATE TABLE account_assignments (
    id SERIAL PRIMARY KEY,
    account_email TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    assigned_date DATE NOT NULL,
    assigned_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(account_email, assigned_date)
);
CREATE INDEX idx_assignments_date ON account_assignments(assigned_date);
CREATE INDEX idx_assignments_email ON account_assignments(account_email);
```

#### `trend_snapshots` (existing, adapted)
```sql
CREATE TABLE trend_snapshots (
    id SERIAL PRIMARY KEY,
    agent_name TEXT NOT NULL,
    audit_date DATE NOT NULL,
    audit_timestamp TIMESTAMPTZ NOT NULL,
    account_email TEXT,
    total_issues INTEGER DEFAULT 0,
    overall_score DOUBLE PRECISION,
    compliance_score DOUBLE PRECISION,
    sentiment_score DOUBLE PRECISION,
    professionalism_score DOUBLE PRECISION,
    script_adherence_score DOUBLE PRECISION,
    conversations_analyzed INTEGER DEFAULT 0
);
CREATE INDEX idx_trends_agent ON trend_snapshots(agent_name);
CREATE INDEX idx_trends_date ON trend_snapshots(audit_date);
CREATE UNIQUE INDEX idx_trends_unique ON trend_snapshots(agent_name, audit_date);
```

---

## Data Flow

### Audit Run (permanent save)
```
User clicks "Run Audit"
  → scraper extracts conversations from SmarterContact
  → for each conversation:
      → UPSERT contact into contacts table (by name)
      → INSERT into conversations table (agent_id, contact_id, texter_name, audit_date)
      → INSERT messages into messages table (one row per message)
  → AI scores each conversation
  → for each scored conversation:
      → INSERT into conversation_scores table (scores, red_flags, summary, etc.)
  → INSERT aggregated audit_scores row (same as today)
  → INSERT trend_snapshots row (same as today)
  → Results appear on main dashboard
```

### Reset (UI only)
```
User clicks "Reset"
  → Clears the main dashboard UI state
  → Does NOT delete any rows from Postgres
  → conversations, messages, conversation_scores all remain permanently
  → Next audit run creates new rows for the new day
```

### Detailed Dashboard Query
```
User selects: texter="Amna", start=2026-04-01, end=2026-04-30
  → Query: SELECT conversations + conversation_scores
           WHERE texter_name = 'Amna'
           AND audit_date BETWEEN start AND end
           AND conversation_scores.red_flags IS NOT NULL
           AND jsonb_array_length(conversation_scores.red_flags) > 0
  → Returns: list of flagged conversations with scores
  → User clicks a row
  → Query: SELECT messages WHERE conversation_id = X
  → Opens existing chat detail view with AI analysis + full conversation
```

---

## Frontend — Detailed Dashboard

### Sidebar
Under the Trends icon in the left sidebar, add a second sub-item:

```
TRENDS
  · Trend                ← existing, no changes
  · Detailed Dashboard   ← new
```

Clicking "Detailed Dashboard" switches to the new view.

### Detailed Dashboard View

**Filters bar (top):**
- Start Date — date picker (required)
- End Date — date picker (required)
- Agent Name — dropdown from roster (required, no "all" option)
- Search/Apply button

**Results list:**
Each row shows:
- Contact initials badge (colored circle)
- Contact name
- Labels assigned (colored pills)
- Conversation preview snippet (first agent message)
- Date (audit_date)
- Issue count (number of red flags)
- Overall score (colored by threshold)

Only conversations with red flags appear.

**Detail view (on row click):**
Reuses the existing chat detail view — same layout:
- Back button to return to list
- Left panel: AI Audit Analysis (scores, funnel, pillars, labels, red flags with "Not Valid" buttons, summary)
- Right panel: Full conversation thread with all messages

No new UI component needed for the detail view — the existing one is reused.

---

## Files Changed

### Sub-project 1: PostgreSQL Migration

| File | Change |
|------|--------|
| `database/db.py` | Full rewrite: asyncpg connection pool, new schema with CREATE TABLE, all CRUD functions updated |
| `database/schema.sql` | New file: complete SQL schema for Postgres (all CREATE TABLE + indexes + pgvector extension) |
| `dashboard/app.py` | All `aiosqlite` queries → `asyncpg` queries. Add connection pool startup/shutdown. Update reset to only clear UI state. |
| `ai/scorer.py` | Write to `conversation_scores` table. Update flag_feedback query from SQLite to asyncpg. |
| `ai/session_logger.py` | Update INSERT to asyncpg |
| `ai/dream_worker.py` | Update queries to asyncpg |
| `config/settings.py` | Add `DATABASE_URL` from env, remove `DB_PATH` |
| `.env` | Add `DATABASE_URL=postgresql://user:password@localhost:5432/texting_audit` |
| `requirements.txt` | Add `asyncpg`, `pgvector`. Remove `aiosqlite` |
| `main.py` | Update database initialization call |

### Sub-project 2: Detailed Dashboard

| File | Change |
|------|--------|
| `dashboard/app.py` | Add `GET /api/detailed-dashboard` endpoint (filters → flagged conversations). Add `GET /api/conversation/{id}/messages` endpoint. |
| `dashboard/templates/index.html` | New sidebar sub-item "Detailed Dashboard" under Trends. New view div with filters + results list. Wire click-to-detail using existing chat detail view. |

---

## Out of Scope

- No embedding pipeline — pgvector column exists but stays NULL
- No data migration from SQLite — fresh start, old .db file kept as archive
- No changes to the existing Trends view
- No changes to the existing main dashboard audit flow (extract → score → display)
- No "show all texters" option in Detailed Dashboard filters
- No account email filter in Detailed Dashboard
- No pagination (conversation volume is manageable)
- No export/download of historical data
