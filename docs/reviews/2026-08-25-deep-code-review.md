# Deep Code Review — TEXTING-AUDIT-AUTOMATION

**Date:** 2026-08-25
**Scope:** ~19k lines Python + ~13k lines HTML/JS, commit `e6061b5` on `main`
**Produced by:** the `deep-code-reviewer` subagent (`.claude/agents/deep-code-reviewer.md`)

---

## Remediation status (branch `fix/deep-review-remediation`)

**30 of 40 findings fixed.** 92 tests now pass where there were previously none.

| Status | Findings |
|---|---|
| **Fixed** | F1, F2, F6, F7, F8, F10, F11, F12, F13, F14, F15, F20, F21, F23, F24, F25, F26, F27, F28, F29, F30, F31, F32, F34, F35, F36, F37, F38, F39, F40 |
| **Partly fixed** | F4 (gitleaks in CI; **key rotation is a manual Firebase Console step**), F16 (TTL cache + supporting index added; the full set-based SQL rewrite is deferred) |
| **Not started** | F3, F5, F9, F17, F18, F19, F22, F33 |

### Requires a manual step before it takes effect

- **F4** — rotate the Firebase Web API key in the Firebase Console. Nothing in
  the code can do this, and until it happens the key in git history stays live.
- **F7 / F29 / F12** — apply `database/migrations/007_data_integrity.sql`. The
  application code works with or without it (it probes for the constraint at
  startup and logs a warning when absent), so there is no deploy-ordering
  hazard, but the duplicate rows keep accumulating until it runs.
- **F23** — new conversations are now dated correctly, but existing rows still
  carry UTC-derived `convo_date` values. Backfill and re-run `_reattribute_day`
  over the affected range.

### Behaviour that will visibly change

- **Flag counts will rise** once F1 ships — previously-suppressed flags return.
  This is the correction, not a regression. Warn the team first.
- **Some agents will show "failed" instead of "done"** (F6) — those runs were
  already failing; the badge was lying.
- **F17 response-time flags will drop**, possibly sharply (F28) — historical
  slow replies outside the 30-day window no longer count.
- **Detailed Dashboard cards will show message previews** where they previously
  read "No messages" (F34).

### Found during remediation, not in the original review

`ai/prefilter/flag_triggers.py::_split_text` had the same `sender == "agent"`
bug as F10 — every message was classified as the lead and `agent_text` was
always empty. **Latent, not live**: the module has no importers. Fixed anyway.

---

## Findings

### F1. One "Not Valid" click permanently disables a compliance flag for every agent, forever — [CONFIRMED]
- **Severity:** Critical
- **Location:** `ai/scorer.py:67-82` (`_load_invalid_flag_patterns`), `:119-159` (`_filter_flags`), applied at `:325`, `:419`
- **Problem:** The suppression list is loaded globally with no scoping at all, then fuzzy substring-matched against every new flag on every conversation of every agent:
  ```python
  cur.execute("SELECT red_flag FROM flag_feedback")           # no agent/conversation/status filter
  patterns = {row[0].lower().strip() for row in cur.fetchall() if row[0]}
  ...
  if len(p) < 15: return f == p
  if f in p or p in f: return True                            # bidirectional substring
  ```
  A reviewer clicking "Not Valid" on `"Continued texting after explicit opt-out."` (41 chars) for one conversation writes that exact string to `flag_feedback`. From the next run on, `_filter_flags` strips that flag from **every conversation for every agent, permanently**. A precise, conversation-scoped mechanism already exists right beside it (`_load_invalid_flags_by_conversation`, `ai/scorer.py:85-116`) — but the global path runs first and dominates. Worse, `/api/redflag/invalid` (`dashboard/app.py:2180`) does not set `status`, so the column defaults to `'invalid'` even when the UI sends `correctness='correct'`.
- **Impact:** This is a compliance-audit product. Its single most important output — "did the agent keep texting after an opt-out" — can be silently and irreversibly switched off company-wide by one reviewer misclick. Nothing in the UI indicates a flag has been globally suppressed. Audits will look clean while the underlying violations continue.
- **Recommended Fix:** Delete `_load_invalid_flag_patterns` and the two `_filter_flags(..., invalid_patterns)` call sites. Keep only the conversation-scoped path, and match on `flag_id` (from `flag_details`) rather than free-text substring. If a global suppression is genuinely wanted, gate it behind an explicit, separate table (`suppressed_flag_ids`) with an owner-only endpoint and a visible banner in the UI.
- **Priority:** Fix immediately
- **Regression risk:** Previously-hidden flags will reappear en masse on the next run — expect a one-time spike in flag counts. Before shipping, run `SELECT red_flag, COUNT(*) FROM flag_feedback GROUP BY 1 ORDER BY 2 DESC` to see exactly which flags are currently suppressed and how many reviewers rejected each.

---

### F2. Stored XSS in the Detailed Dashboard result cards — [CONFIRMED]
- **Severity:** Critical
- **Location:** `dashboard/static/index.html:5065-5082` (`renderDetailedResults` card builder)
- **Problem:** This is the only render path in the file that skips the `esc()` helper defined at line 3199:
  ```js
  <span style="...">${contact}</span>
  ${showTexter && texter ? `<span style="...">${texter}</span>` : ""}
  ${labels ? `<span style="...">${labels}</span>` : ""}
  <div style="...">${preview}</div>
  ```
  `contact` is `row.contact_name` — the SmarterContact contact name, third-party scraped data. `labels` is the joined SmarterContact label titles. Every other renderer in the file (`renderChatMessages:4415`, `renderAiAnalysis:4524`, `renderConvoList:4255`, the trends table at `5957`) escapes correctly, so this is an isolated gap, not a design choice.
- **Impact:** A contact name or label title containing `<img src=x onerror=...>` executes in the dashboard of anyone who opens the Detailed Dashboard. Because every authenticated session is effectively a full administrator (see F5), that payload can call `DELETE /api/agents/{id}`, `DELETE /api/reset-all`, or `POST /api/tool-access` to grant an attacker persistent access — all with the victim's session cookie.
- **Recommended Fix:** Wrap all five interpolations in `esc()`. Then add a lint gate requiring `esc(` inside `innerHTML` templates. Better still, add a `h` tagged-template helper that escapes every substitution by default and migrate `innerHTML` sites to it.
- **Priority:** Fix immediately
- **Regression risk:** None functionally. Verify that contact names containing legitimate `&` or `'` still render correctly.

---

### F3. SmarterContact account passwords stored and updated in plaintext — [CONFIRMED]
- **Severity:** High
- **Location:** `database/schema.sql:11` (`password TEXT`), `dashboard/app.py:2059-2062` and `:2101-2105`, read back at `scraper/queue_manager.py:62`
- **Problem:** `INSERT INTO accounts (name, email, password, ...) VALUES ($1, $2, $3, ...)` stores the raw credential. `.env.example:13` advertises a `CREDENTIALS_KEY` for "Encryption key for agent credentials (auto-generated on first run)" and `config/settings.py:74` reads it — but nothing in the codebase ever uses it. These are not hashable (the scraper must replay them to Firebase), so they need symmetric encryption, which is exactly what the unused `cryptography==44.0.0` dependency is for.
- **Impact:** A Postgres backup leak, a read-only DB credential leak, or a SQL-injection anywhere in the app hands the attacker live login credentials for every SmarterContact account — which contain the full SMS history of thousands of real estate leads (PII). Passwords also transit the `POST /api/agents/add` body in cleartext to the app.
- **Recommended Fix:** Encrypt at rest with `cryptography.fernet.Fernet(CREDENTIALS_KEY)`. Add `password_enc BYTEA`, write both during a migration window, switch `queue_manager.load_agents` to decrypt, then drop `password`. Make `CREDENTIALS_KEY` a required setting (raise like `DATABASE_URL` does) rather than defaulting to `""`.
- **Priority:** Fix this sprint
- **Regression risk:** A wrong or rotated `CREDENTIALS_KEY` bricks every scrape at once. Keep the plaintext column readable for one deploy cycle and verify decryption for all accounts before dropping it.

---

### F4. Live Firebase Web API key remains in git history — [CONFIRMED]
- **Severity:** High
- **Location:** git history of `scraper/firebase_auth.py`, removed by `e53c6c2 security: harden secrets`; still retrievable via `git log -p -- scraper/firebase_auth.py`
- **Problem:** A 39-character `AIzaSy…`-prefixed Google API key was hardcoded as `FIREBASE_API_KEY = "..."` and is still present in every clone of the repository. The current file correctly reads it from the environment (`firebase_auth.py:15`), but removal from HEAD does not remove it from history. Location and shape only — the value is not reproduced here.
- **Impact:** The key gates `identitytoolkit.googleapis.com/v1/accounts:signInWithPassword` for the SmarterContact Firebase project. Combined with F3 (plaintext passwords) it completes a credential chain. Firebase Web API keys are semi-public by design, but this one should not be treated as such given what it unlocks here.
- **Recommended Fix:** Rotate the key in the Firebase Console first (that is the only step that actually mitigates it). Then scrub history with `git filter-repo --replace-text` or accept the exposure and rely on rotation. Add a pre-commit secret scanner (`gitleaks`) so this cannot recur.
- **Priority:** Fix immediately (rotation), backlog (history scrub)
- **Regression risk:** Every deployment and every developer `.env` must get the new key simultaneously or all scraping stops.

---

### F5. No authorization tiers — every allowlisted login is a full administrator — [CONFIRMED]
- **Severity:** High
- **Location:** `dashboard/app.py:356-364` (`require_admin`), applied at `:1511`, `:1537`, `:1726`, `:1975`, `:1994`, `:2035`, `:2073`, `:2127`, `:2157`, `:2492`, `:2879`, `:3217`, `:3433`, `:3504`, `:3842`, `:4168`, `:4189`
- **Problem:**
  ```python
  _ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
  def require_admin(request: Request) -> None:
      if not _ADMIN_TOKEN:
          return  # gate disabled — auth not configured
  ```
  `.env.example:32` documents `ADMIN_TOKEN=` as the default ("Leave unset for local dev"), and `lifespan` only logs a warning when it is absent (`app.py:168-172`). When unset, `Depends(require_admin)` is a no-op on all 17 mutating routes. The browser never sends an `X-Admin-Token` header anywhere in `static/index.html` — so setting `ADMIN_TOKEN` on Railway would 401 the entire UI. In practice the gate is dead either way. Only two endpoints have real per-role checks (`/api/reset-dedup-cache:1435`, `/api/reset-history:1451`, both owner-only).
- **Impact:** Anyone whose email is in `tool_access` — a list any of them can extend via `POST /api/tool-access`, which is itself only `require_admin`-gated — can delete every agent and all their audit data (`DELETE /api/agents/{id}`), wipe all trend history, archive every conversation, and rewrite ownership attribution. There is no read-only role for the managers who only need to review flags.
- **Recommended Fix:** Add a `role` column to `tool_access` (`owner` | `admin` | `reviewer`) and replace `require_admin` with a real session-based dependency: `def require_role(min_role)` that reads `request.session["user_email"]`, looks up the role, and raises 403. Delete the `X-Admin-Token` mechanism entirely — it protects nothing and cannot be satisfied by the UI.
- **Priority:** Fix this sprint
- **Regression risk:** Existing users all need a role assigned during migration; default everyone to `admin` except the owner, then downgrade deliberately, so nobody is locked out mid-day.

---

### F6. A run where every conversation failed to score is reported as "done" — the failure code is unreachable — [CONFIRMED]
- **Severity:** High
- **Location:** `main.py:215-251` (`run_single_agent`)
- **Problem:** The `finally` block computes `final_status` — including the `"scoring_failed"` case — but the code that *writes* it sits after the `try/except/finally`:
  ```python
  finally:
      ...
              if valid == 0:
                  final_status = ("failed", "scoring", "Scoring failed — no conversations were saved. ...")
  # ── unreachable ──
  if final_status:
      _write_run_status(agent_name, final_status[0], ...)
  ```
  Every path through the `try` (lines 168, 185, 203) and the `except` (line 214) returns, so `finally` runs and the function exits. Confirmed with AST: the `Try` node spans 144-241 and the `If` at 243 is the next statement in the function body — dead code.
- **Impact:** Two consequences. (1) The "Done — N conversation(s) ready" message is never written, so the dashboard falls back to `proc.returncode == 0` (`app.py:1024`). (2) More seriously, a run in which extraction succeeded but **every** conversation failed scoring exits 0 (`main.py:420` only checks `status != "success"`), so the UI shows a green "Done" badge for an audit that produced nothing. Managers see a clean agent instead of a broken pipeline.
- **Recommended Fix:** Move the `if final_status:` block to the end of the `finally`, or restructure so the function has a single exit point that writes status before returning. Also make `main()` exit non-zero when `final_status[0] == "failed"`.
- **Priority:** Fix this sprint
- **Regression risk:** Runs that currently show "done" will start showing "failed". That is the point, but expect an initial wave of red badges that reveals pre-existing scoring failures.

---

### F7. Re-ingestion dedupe is dead — `mark_chat_audited` is never called — [CONFIRMED]
- **Severity:** High
- **Location:** `database/db.py:497-507` (`mark_chat_audited`), checked at `scraper/api_bot.py:227`
- **Problem:** `grep -rn "mark_chat_audited"` across the whole repo returns exactly one hit — the definition. Nothing writes to `audited_chats`. The read side runs on every "today" scrape:
  ```python
  if await db.is_chat_audited(self.email, contact_name, last_msg_content):
      logger.debug(f"  Skip (already audited): {contact_name}")
      return None
  ```
  and always returns `False`. Meanwhile `save_extraction` (`db.py:346-358`) unconditionally `INSERT`s a new `conversations` row — there is no upsert key on `(agent_id, contact_id, audit_date)`.
- **Impact:** Every re-run of the same day re-scrapes, re-inserts, and re-scores the same conversations. Row counts in `conversations`, `messages`, and `conversation_scores` grow linearly with *runs*, not with *work*. The UI hides this by de-duplicating on contact name in Python (`app.py:869-876`, `app.py:687-696`) — which is precisely why the growth is invisible while it silently destroys the performance of F16. `DELETE /api/reset-dedup-cache` (`app.py:1440`) clears a table that is always empty.
- **Recommended Fix:** Call `await db.mark_chat_audited(self.email, contact_name, last_msg_content)` after a conversation is successfully scored (in `scorer.py`, not in the scraper — marking before scoring would lose failures). Separately, add `UNIQUE(agent_id, contact_id, audit_date)` on `conversations` and turn `save_extraction`'s insert into an upsert, so the invariant holds even if the cache is cleared.
- **Priority:** Fix this sprint
- **Regression risk:** Once dedupe works, re-running an agent will produce "0 conversations" instead of re-auditing — which will look like a bug to users. Ship it together with a visible "already audited today" state, and keep the `--date-filter != today` re-audit escape hatch.

---

### F8. All ML pre-filter telemetry is silently discarded — the tables do not exist — [CONFIRMED]
- **Severity:** High
- **Location:** `database/schema.sql` (no `CREATE TABLE prefilter_decisions`, no `conversation_embeddings`), writer at `ai/prefilter/pipeline.py:255-274`
- **Problem:** `schema.sql` is the only DDL the app ever runs (`app.py:145-146`, executed on **every** boot). It mentions `prefilter_decisions` once, in a comment header at line 299, and never creates it. `conversation_embeddings` appears zero times. The insert is fire-and-forget with a debug-level swallow:
  ```python
  except Exception as e:
      logger.debug(f"[Prefilter] failed to record decision: {e}")
  ```
  Both tables live only in `database/migrations/001_*.sql` and `002_*.sql`, which are manual and — critically — **define `prefilter_decisions` with two incompatible schemas**. 001 has `decision TEXT NOT NULL CHECK(...)`, `predicted_scores`, `notes`. 002 has `short_circuited BOOLEAN`, `predicted`, `groq_actual`, and no `decision`/`notes` columns. Both use `CREATE TABLE IF NOT EXISTS`, so whichever ran first wins and the other is a silent no-op. `pipeline.py` writes the 001 shape.
- **Impact:** On any Railway database provisioned from `schema.sql` alone (the documented path), **zero** prefilter decisions have ever been recorded. That means shadow mode produces no data, `scripts/eval_prefilter.py` has nothing to evaluate, and `scripts/promote_prefilter.py`'s "FALSE-CLEAN ≤ 5%" promotion gate — the documented safety mechanism in `CLAUDE.md` — cannot be computed. If 002 ran instead of 001, every insert fails on `UndefinedColumnError` with the same result. The team believes it has an evaluation harness; it does not.
- **Recommended Fix:** Fold the 001 definitions into `schema.sql` (dropping and recreating `prefilter_decisions` if it exists with the 002 shape — it holds no data worth keeping). Delete `002_prefilter.sql`. Then raise the swallow from `logger.debug` to `logger.warning` so the next silent failure is visible. Add a migration-tracking table (`schema_migrations(version, applied_at)`) so "which migrations ran" is answerable.
- **Priority:** Fix this sprint
- **Regression risk:** Low — the tables are empty. Verify `_record_decision_async` succeeds by checking `SELECT COUNT(*) FROM prefilter_decisions` after one audit run.

---

### F9. The committed classifier artifact was trained on scikit-learn 1.8.0 but the pin is 1.7.2 — [CONFIRMED]
- **Severity:** High
- **Location:** `ai/prefilter/artifacts/manifest.json` (`"sklearn_version": "1.8.0"`) vs `requirements.txt:35` (`scikit-learn==1.7.2`); guard at `ai/prefilter/tier3_classifier.py:57-67`
- **Problem:** Commit `be6c8c4` pinned sklearn back to 1.7.2 for Python 3.10 / Railway compatibility. Commit `50446e7` then committed artifacts regenerated on a developer machine running 1.8.0. The code detects this but only warns:
  ```python
  if _artifact_skl and _artifact_skl != sklearn.__version__:
      logger.warning("[Prefilter T3] sklearn version drift — artifact trained on %s, runtime has %s. Predictions may be invalid; ...")
  ```
  `requirements.txt` even documents the hazard in a trailing comment: *".joblib artifacts are version-sensitive — retrain after any upgrade."*
- **Impact:** In production, `joblib.load` of a 1.8.0-pickled estimator on 1.7.2 either raises (T3 disables itself via `_load_failed`) or unpickles into an object with missing/renamed attributes and produces garbage probabilities. Today the blast radius is contained because `PREFILTER_T3_LIVE=false` on Railway — but that means the moment anyone flips T3 live to save cost, scoring silently degrades with only a `WARNING` in the log. The kNN index has the same coupling risk via `faiss-cpu>=1.7.4` (unpinned).
- **Recommended Fix:** Retrain on 1.7.2 and recommit the artifacts (`python -m ai.prefilter.train --test-split 0.2` then `python -m ai.prefilter.index_builder --rebuild`). Escalate the drift guard from `logger.warning` to `_load_failed = True; return False` — refusing to load is strictly safer than loading a possibly-corrupt model. Pin `faiss-cpu` exactly. Add a CI check that `manifest.json.classifier.sklearn_version` equals the pinned version.
- **Priority:** Fix this sprint
- **Regression risk:** Retraining changes T3's decision boundary. Since T3 is not live, verify offline against the `shadow_report_v2.csv` baseline before enabling.

---

### F10. Embeddings cannot distinguish the agent from the lead — [CONFIRMED]
- **Severity:** High
- **Location:** `ai/prefilter/embedder.py:145-159` (`conversation_to_text`) vs `ai/prefilter/index_builder.py:59-65`; producer at `scraper/api_bot.py:66-67`
- **Problem:** Three places disagree about what marks a message as agent-sent.
  - The scraper writes the agent's **first name token** as the sender: `sender = (agent_name.split()[0] if agent_name else "Agent") if direction == "OUTGOING" else "Contact"`.
  - Inference-time text building tests for a literal string:
    ```python
    sender = (m.get("sender") or "").lower()
    role = "AGENT" if sender == "agent" else "CONTACT"
    ```
    Since `sender` is `"noah"` or `"resva1006"`, this is **always** `"CONTACT"`. Used by `tier2_embedding.py:114` and `tier3_classifier.py:96`.
  - Index-build time uses a *third* rule in SQL: `CASE WHEN LOWER(m.sender) = LOWER(COALESCE(ac.name,'agent')) OR LOWER(m.sender) = 'agent' THEN 'AGENT: ' ...`. This matches only when the account's `name` is a single token (e.g. `Resva1006` → sender `Resva1006` → match), and fails for any account name with a space (`Noah Mallen` → sender `Noah` ≠ `noah mallen`).
- **Impact:** Every query vector is built from a transcript where the agent and the lead are indistinguishable, so Tier 2 kNN and Tier 3 cannot learn "the agent said X after the lead said Y" — which is the entire semantics of the audit. The index itself is inconsistently labelled depending on whether an account name happens to contain a space, so the corpus is internally heterogeneous *and* skewed against the query distribution. This quietly caps the accuracy ceiling of T2/T3 and would keep them from ever passing the FALSE-CLEAN ≤ 5% gate.
- **Recommended Fix:** Introduce one shared predicate — reuse `database/db.py:80-82`'s `_is_outgoing(sender)` — and call it from `conversation_to_text`, from the index-builder SQL (replace the CASE with a check against the `('contact','lead','unknown','')` set), and from `ai/response_time.py:87-89`. Better: store a boolean `messages.is_outgoing` column at ingest and stop inferring role from a free-text name at all. Rebuild the index and retrain after the change.
- **Priority:** Fix this sprint (blocks any T2/T3 promotion)
- **Regression risk:** Invalidates the current index and classifier — both must be rebuilt. Since neither is live in production, the runtime risk is nil; the risk is that measured accuracy will *change* and prior evaluation numbers become non-comparable.

---

### F11. A finishing audit deletes other agents' `audit_scores` during parallel runs — [CONFIRMED]
- **Severity:** High
- **Location:** `database/db.py:605-629` (`cleanup_failed_audits`)
- **Problem:** The function's own docstring explains why scoping matters — *"When `agent_id` is provided, cleanup is scoped to that agent only — critical for parallel runs"* — and the first half honors it. The second half does not:
  ```sql
  DELETE FROM audit_scores s
   WHERE NOT EXISTS (
       SELECT 1 FROM conversations c
       JOIN conversation_scores cs ON cs.conversation_id = c.id
       WHERE c.agent_id = s.agent_id
         AND c.is_archived = FALSE
         AND cs.compliance_score IS NOT NULL
   )
  ```
  No `agent_id` parameter, no scoping. It runs in `main.py`'s `finally` on every single-agent run.
- **Impact:** Two live scenarios. (1) The 23:00 scheduled reset (`app.py:97`) sets `is_archived = TRUE` on every conversation; the next audit anyone runs then deletes the `audit_scores` summary row for **every** agent. (2) During a multi-agent run, agent A finishing first deletes agent B's summary if B's conversations happen to be archived. The loop immediately below (`db.py:632-651`) then rewrites `audit_scores.details` for the survivors row-by-row (an N+1 write). Trend snapshots survive, so the loss is partial and inconsistent — the dashboard shows no score while trends show one.
- **Recommended Fix:** Add `AND s.agent_id = $1` (parameterized, guarded by `if agent_id is not None`) to the `DELETE FROM audit_scores`, and scope the `SELECT id, agent_id, details FROM audit_scores` sweep the same way. If a global sweep is genuinely wanted, move it to an explicit maintenance command, not a per-run `finally`.
- **Priority:** Fix immediately
- **Regression risk:** Orphaned `audit_scores` rows for agents that never run again will now persist. Add a one-off cleanup script rather than leaving the unscoped delete in the hot path.

---

### F12. Destructive endpoints are not transactional, and agent deletion aborts halfway on a foreign key — [CONFIRMED]
- **Severity:** High
- **Location:** `dashboard/app.py:2131-2143` (`api_delete_agent`), `:1455-1464` (`api_reset_history`), `:1981-1985` (`api_reset_all`), `:93-97` (`_scheduled_reset_all`)
- **Problem:** None of these wrap their multi-statement deletes in `async with conn.transaction()` — asyncpg autocommits each `execute` individually. (The assignments code does this correctly at `app.py:2948`, `3286`, `3537`, `4205`, so the pattern is known.) `api_delete_agent` then hits an unhandled FK:
  ```python
  await conn.execute("DELETE FROM audited_chats   WHERE agent_email = $1", email)
  ... 5 more deletes, each committed ...
  await conn.execute("DELETE FROM accounts        WHERE id         = $1", agent_id)
  ```
  `validation_log.agent_id INTEGER NOT NULL REFERENCES accounts(id)` (`schema.sql:274`) has **no** `ON DELETE` action and is never deleted here.
- **Impact:** Deleting an agent that has any `validation_log` rows destroys all of their conversations, messages, scores, extractions, and feedback — commits every one of those — then fails on the last statement with a 500. The account row survives with all of its history gone, and the operation is not retryable to a clean state. `api_reset_history` has the same shape across nine deletes: a mid-sequence failure leaves the database in a partially-wiped state that no code path can reconcile.
- **Recommended Fix:** Wrap each handler body in `async with conn.transaction():`, and add `DELETE FROM validation_log WHERE agent_id = $1` before the `accounts` delete. Longer term, add `ON DELETE CASCADE` to `validation_log.agent_id` and `flag_feedback.agent_id` so the schema enforces the invariant instead of relying on the handler remembering every table.
- **Priority:** Fix immediately
- **Regression risk:** Deletes that currently "half work" will now fail cleanly and loudly. That is correct, but check whether any account rows already exist in the orphaned state described above before shipping.

---

### F13. Every container restart resurrects deleted blacklist labels — [CONFIRMED]
- **Severity:** Medium
- **Location:** `database/schema.sql:354-358`, executed by `dashboard/app.py:145-146`
- **Problem:** `app.py`'s `lifespan` runs the entire `schema.sql` unconditionally on every boot (unlike `db.py:186-201`, which correctly checks whether tables exist first). `schema.sql` contains seed data:
  ```sql
  INSERT INTO blacklist_labels (name, skip_mode) VALUES
      ('Extra',    'any'),
      ('New Lead', 'only')
  ON CONFLICT (name) DO NOTHING;
  ```
  `ON CONFLICT DO NOTHING` makes it idempotent against *duplicates*, not against *deletion*.
- **Impact:** A user who removes "Extra" from the blacklist via `DELETE /api/blacklist-labels/{id}` sees it silently return after the next Railway redeploy — and conversations labelled "Extra" start being skipped again without explanation. Every Railway deploy, crash-restart, and scale event re-applies it. This is the kind of bug that costs hours to diagnose because it looks intermittent.
- **Recommended Fix:** Move seed data out of `schema.sql` into a one-time bootstrap that only runs when the table is empty (mirror `_seed_tool_access`'s `if count <= 1` guard at `app.py:241`). More broadly, stop executing full DDL on every boot — gate it behind the same `tables_exist` check `db.py` already uses, and move schema evolution into tracked migrations.
- **Priority:** Fix this sprint
- **Regression risk:** A genuinely fresh database will no longer get the defaults unless the bootstrap path is correct. Test against an empty database.

---

### F14. The full dashboard SPA — including the owner's personal email — is served without authentication — [CONFIRMED]
- **Severity:** Medium
- **Location:** `dashboard/app.py:306` (`SessionAuthMiddleware._OPEN` skips `/static/`), `:348` (`StaticFiles` mount), disclosed strings at `dashboard/static/index.html:2600` and `:6303`
- **Problem:** The middleware short-circuits on `path.startswith("/static/")`, and the mounted directory contains `index.html` — the same file `GET /` serves after auth. So `GET /static/index.html` returns the entire 8,125-line application to anyone. It hardcodes the owner's personal Gmail address twice for client-side gating.
- **Impact:** Unauthenticated disclosure of the complete internal API surface (every route, parameter, and response shape) plus the owner's personal email address — a ready-made phishing and credential-stuffing target, since that same address is the OAuth identity that controls the tool. The authorization itself is still enforced server-side (`app.py:1435`, `:1451`), so this is disclosure, not privilege escalation.
- **Recommended Fix:** Move `index.html` out of the statically-mounted directory (e.g. `dashboard/views/index.html`) and keep serving it via the authenticated `GET /` `FileResponse`. Replace the hardcoded owner email in the client with the existing `/api/me` response plus a server-supplied `is_owner` boolean, so no email literal ships to the browser.
- **Priority:** Fix this sprint
- **Regression risk:** None, provided `GET /` is updated to the new path. `login.html` and `sms.png` must stay under `/static/`.

---

### F15. Session expiry leaves the dashboard permanently stuck on an error — [CONFIRMED]
- **Severity:** Medium
- **Location:** `dashboard/static/index.html` — no occurrence of `401` or a `/login` redirect anywhere in 8,125 lines (only `/auth/logout` at line 1859); `dashboard/app.py:318-321`
- **Problem:** The server returns JSON 401 for expired sessions on `/api/*`. The client treats every non-200 identically (`if (!res.ok) throw new Error("HTTP " + res.status)`), rendering the error into the DOM. `pollStatus` (`:5404`) swallows it into `console.warn` and keeps polling every 3 s forever.
- **Impact:** After the 7-day cookie expires (or after a restart when `SESSION_SECRET_KEY` is unset), the dashboard shows "Failed to load: HTTP 401" and never recovers. The user's only recourse is to guess that they should hard-reload. Meanwhile the app keeps hammering `/api/status` every 3 seconds indefinitely.
- **Recommended Fix:** Add one wrapper used by all 45 `fetch` sites:
  ```js
  async function api(url, opts) {
    const r = await fetch(url, opts);
    if (r.status === 401) { window.location.href = "/login?error=session_expired"; throw new Error("unauthenticated"); }
    return r;
  }
  ```
  and stop the poll loop on 401.
- **Priority:** Fix this sprint
- **Regression risk:** A transient 401 (e.g. during a deploy) would bounce a working user to the login page. Acceptable, and better than the current silence.

---

### F16. `/api/agents` scans every conversation and score in the database on every call — [CONFIRMED]
- **Severity:** High
- **Location:** `dashboard/app.py:672-758` (`_compute_review_stats_bulk`), called unconditionally from `_fetch_agents_with_scores:458`
- **Problem:** Four unbounded queries with no `LIMIT`, no date filter, and no agent filter:
  ```sql
  SELECT c.agent_id, c.id, ct.name AS contact_name
    FROM conversations c JOIN contacts ct ON ct.id = c.contact_id
   WHERE c.is_archived = FALSE
   ORDER BY c.extracted_at DESC, c.id DESC
  ```
  followed by all of `flag_feedback`, all of `flagged_conversation_reviews`, and a `DISTINCT ON` over `conversation_scores` for every returned conversation id. All rows are then materialized in Python and de-duplicated by contact name in a loop. There is no index supporting `(is_archived, extracted_at DESC)` — `schema.sql:58-61` only indexes `agent_id`, `texter_name`, `audit_date`, `contact_id`.
- **Impact:** Cost scales with total historical rows, not with what the page shows. Because F7 makes `conversations` grow by a full copy on every re-run, this compounds fast. Every audit completion triggers `loadAgents()` (`index.html:5392`), and a 20-agent run means 20 full-table scans plus 20 sorts of the entire scores table, on Railway where each round trip also pays network latency. At 10× current rows the endpoint becomes the dominant cost of the dashboard; at 100× it will time out.
- **Recommended Fix:** Push the whole computation into one set-based SQL query returning `(agent_id, needs_review, flagged)` — the dedupe-by-contact is expressible as `DISTINCT ON (c.agent_id, LOWER(TRIM(ct.name)))`, and the flag/label predicates already exist in SQL form at `app.py:435-438`. Scope it to a date window. Add `CREATE INDEX idx_conversations_active ON conversations(agent_id, extracted_at DESC) WHERE is_archived = FALSE`. Cache the result for 10-15 s — the numbers only change when an audit finishes.
- **Priority:** Fix this sprint
- **Regression risk:** The Python and SQL de-duplication must agree exactly on tie-breaking (`extracted_at DESC, id DESC`) or per-agent review counts will shift. Diff old vs new output for all agents before switching.

---

### F17. `/api/detailed-dashboard` has no result limit and a non-sargable date predicate — [CONFIRMED]
- **Severity:** Medium
- **Location:** `dashboard/app.py:3915-3986` (`_DETAILED_SQL`), also `:842-867` (`_fetch_agent_conversations`)
- **Problem:** The date filter wraps the column in a `CASE`/`TO_DATE` expression, which no index can serve:
  ```sql
  WHERE ... AND (
      CASE WHEN c.convo_date <> '' THEN TO_DATE(c.convo_date, 'MM/DD/YYYY')
           ELSE c.audit_date END
    ) BETWEEN $1 AND $2
  ```
  There is no `LIMIT` anywhere in the statement, and `texter_name = 'all'` removes the only other selective predicate. `_fetch_agent_conversations` similarly loads **every** message of **every** conversation for an agent (`app.py:910-917`) with no bound, and ships them all to the browser.
- **Impact:** A user selecting "All Texters" over a wide date range triggers a sequential scan of `conversations` with a per-row lateral subquery against `conversation_scores`, then serializes the whole result to JSON. Combined with the row growth from F7 this becomes a reliable way for any user to stall the single-worker uvicorn process — which is the same process hosting the embedding service (F22).
- **Recommended Fix:** Store `convo_date` as a real `DATE` column (it is already produced from a parsed timestamp — see F23 — so the text round-trip buys nothing), or add an expression index. Add `LIMIT`/`OFFSET` with a server-enforced cap (the pattern already exists correctly at `app.py:2362`) and paginate the UI.
- **Priority:** Fix this sprint
- **Regression risk:** Pagination changes the client contract — `renderDetailedResults` currently assumes it receives everything. Ship the server cap and the UI "load more" together.

---

### F18. The 30-minute unread rule never fires — [CONFIRMED]
- **Severity:** Medium
- **Location:** `ai/scorer.py:169-203` (`_check_overdue_unreads`), called at `:472`; data source at `scraper/api_bot.py:153`
- **Problem:** `score_agent_conversations(..., unread_conversations: list[dict] | None = None, ...)` — and `main.py:190-196`, the only production caller, never passes it. So `overdue = _check_overdue_unreads(unread_conversations or [])` always iterates an empty list. Even if it were passed, the scraper hardcodes `"unread_conversations": []` and never populates it.
- **Impact:** A documented, business-meaningful rule ("Unread message from X has been waiting N minutes with no response (30-min rule)") has never produced a single flag. Managers reasonably believe unresponsive agents are being caught. `_check_overdue_unreads` also carries a latent timezone bug: `EASTERN` at `scorer.py:163` is computed once at import from `time.daylight and time.localtime().tm_isdst`, freezing the UTC offset for the process lifetime and going wrong across a DST boundary.
- **Recommended Fix:** Decide whether the rule is wanted. If yes: have `api_bot.extract_all` populate `unread_conversations` from the `findConversations` items where `unreadMessages > 0` (the GraphQL query at `gql_client.py:31-32` already fetches `isRead` and `unreadMessages`), thread it through `main.py`, and replace the frozen `EASTERN` constant with `config.settings.TIMEZONE`. If no: delete the function and the parameter so the code stops implying a capability it lacks.
- **Priority:** Fix this sprint (decide), backlog (implement)
- **Regression risk:** Turning it on adds a new flag class to every agent's score at once. Roll out behind a flag and review the first day's output manually.

---

### F19. A 1,037-line function makes the core scoring rules effectively unmodifiable — [CONFIRMED]
- **Severity:** High
- **Location:** `ai/prefilter/tier1_phrases_v2.py` — `evaluate()` spans 1,037 lines; `ai/prefilter/tier4_flag_generator.py` — `generate()` spans 461 lines; `ai/prefilter/label_validator.py` — `_expected_label()` spans 205 lines
- **Problem:** Measured via AST. `tier1_phrases_v2.py` is 2,201 lines across 20 functions, and one of them is half the file. `label_validator.py` is 1,427 lines with 368 module-level regexes and 19 functions. These are the deterministic rule engines that now produce **100% of the product's output** (Groq is gone — see F31), so every scoring decision flows through them.
- **Impact:** `git log` shows `tier1_phrases_v2.py` changed 21 times and `label_validator.py` 16 times — this is the most actively edited logic in the repo, and it is the least testable. With zero tests (F20), each of those 37 commits was an unverified change to the core product output. The recent history is full of fix-a-false-positive commits (`5a559a7 fix(ml): resolve 18 reviewer-rejected label flags`, `118ae4f fix(ml): DNC pattern coverage`) — the classic signature of rules being tuned by production feedback with no regression net.
- **Recommended Fix:** Do not rewrite it. Instead, extract seams incrementally: each `if <condition>: flag(...)` block inside `evaluate()` is already an independent predicate over `(messages, labels, funnel_tier)`. Move them one at a time into a registry of small named functions — `def f14_address_denial(ctx) -> Flag | None` — with the dispatcher iterating the registry. Each extraction is mechanical, individually verifiable against a golden-transcript fixture (F20), and immediately buys a unit-testable rule. Start with the rules that have historically regressed: F14, F16, F17, and the DNC/Wrong-Number precedence.
- **Priority:** Backlog (but start it behind the test harness from F20)
- **Regression risk:** High if done in one pass, near-zero if done one rule at a time behind golden-transcript tests. Capture the current output for ~200 real conversations *before* touching anything and assert byte-identical results after each extraction.

---

### F20. Zero tests, no test harness, no CI — and `tests/` is gitignored — [CONFIRMED]
- **Severity:** High
- **Location:** repository-wide; `.gitignore:66` (`tests/`), `:52` (`.pytest_cache/`); no `.github/`; no `pytest` in `requirements.txt`
- **Problem:** `find` across the repo returns no `test_*.py`, no `*_test.py`, no `conftest.py`, no `pytest.ini`. There is no `tests/` directory, and `.gitignore` actively excludes one. There is no CI configuration of any kind.
- **Impact:** This is the finding that makes every other finding worse. Ten of the confirmed bugs in this report (F1, F6, F7, F10, F11, F12, F18, F23, F24, F28) are the kind that a single unit test would have caught at write time. More importantly, the fixes recommended here — especially F1, F7, F10, and F11 — change core scoring and data-lifecycle behavior with no way to verify that anything else still works. Right now the only regression detector is a manager noticing wrong numbers on a dashboard days later.
- **Recommended Fix:** See Testing Recommendations. Minimum viable step: remove `tests/` from `.gitignore`, add `pytest` + `pytest-asyncio` to `requirements.txt`, and write the pure-function tests first — they need no database and no network.
- **Priority:** Fix this sprint
- **Regression risk:** None.

---

### F21. A new Postgres connection is opened per conversation from a thread pool — [CONFIRMED]
- **Severity:** High
- **Location:** `ai/prefilter/semantic_learner.py:111` (`with psycopg2.connect(dsn) as conn`), reached via `ai/prefilter/pipeline.py:114,126` → `_try_semantic_capture`
- **Problem:** `capture_candidate` opens a fresh synchronous connection for a single `INSERT ... ON CONFLICT DO NOTHING`. It is invoked on **every** short-circuited conversation, from inside `asyncio.to_thread` (`ai/scorer.py:311`) under `asyncio.Semaphore(15)` (`:294`). Note also that `with psycopg2.connect(...) as conn` commits the transaction but does **not** close the connection.
- **Impact:** Each audit subprocess can hold 15 concurrent psycopg2 connections plus its asyncpg pool of 2 (`db.py:184`). `MAX_PARALLEL_WORKERS` is documented as raised to 20 (`9257f40`), and the dashboard holds up to 10 more (`app.py:143`). Worst case is roughly 20 × 17 + 10 ≈ 350 connections against a Postgres default `max_connections` of 100. The symptom is `FATAL: sorry, too many clients already` — which `capture_candidate` swallows into `logger.warning` and `_load_invalid_flag_patterns` swallows into an empty set, so a connection storm degrades scoring silently rather than failing loudly. Commit `4077f6d fix(db,gql): prevent concurrency deadlocks, pool exhaustion` suggests this class of problem has already bitten once.
- **Recommended Fix:** Pass the existing asyncpg pool down to the capture path and make it async, or batch captures in memory and flush once per agent run (they are not latency-sensitive — `auto_promote` waits 24 h before using them anyway). Failing that, use a module-level `psycopg2.pool.ThreadedConnectionPool` sized to the semaphore. Also lower `MAX_PARALLEL_WORKERS` until the connection math fits the database's real limit, and add a startup log line reporting `SHOW max_connections`.
- **Priority:** Fix this sprint
- **Regression risk:** Batching delays candidate visibility by one run — harmless given the 24 h stabilization window.

---

### F22. The embedding model and FAISS index run inside the web server process — [CONFIRMED]
- **Severity:** Medium
- **Location:** `dashboard/app.py:67` (`EMBEDDING_SERVICE_URL`), `:206-208` (router mount), `:183-188` (warmup thread); `ai/prefilter/embedding_service.py:43-52`
- **Problem:** The dashboard hosts `/internal/embed` and `/internal/embed_batch` as sync `def` handlers, so FastAPI runs them in its threadpool inside the same process that serves the UI. Audit subprocesses call back into it over `http://127.0.0.1:$PORT`. The warmup also loads the FAISS index and the sklearn classifier into that process (`embedding_service.py:69-78`).
- **Impact:** The design correctly avoids paying a 15-20 s model load per subprocess — that part is sound. But it puts CPU-bound transformer inference in direct contention with HTTP request handling on a single Railway container. `PREFILTER_TORCH_THREADS=1` limits each *call*, not the number of concurrent calls: up to 20 subprocesses × 15 concurrent conversations can queue against a 40-slot threadpool. When that happens the dashboard's own endpoints — including the 3-second `/api/status` poll — starve behind embedding work. Commit `e6061b5 perf: cut Railway CPU` shows this pressure is already real.
- **Recommended Fix:** Short term, bound the concurrency: put a `threading.Semaphore(N)` around `_embed_batch_local` sized to the container's CPU quota, and return 503 rather than queueing when saturated (`embedder._embed_via_service` already falls back to a local model on failure). Medium term, split the embedding service into its own Railway service so ML load cannot degrade the UI — the HTTP boundary already exists, only the URL needs to change.
- **Priority:** Backlog
- **Regression risk:** A separate service means `EMBEDDING_SERVICE_URL` must be reachable from subprocesses; the localhost-only guard at `app.py:309-315` would need to become a shared-secret check.

---

### F23. `convo_date` is computed in UTC, so evening conversations are filed on the wrong day — [CONFIRMED]
- **Severity:** Medium
- **Location:** `scraper/api_bot.py:243-247`
- **Problem:**
  ```python
  last_at = convo.get("lastMessageAt") or ""
  convo_date = datetime.fromisoformat(last_at.replace("Z", "+00:00")).strftime("%m/%d/%Y") if last_at else ""
  ```
  `lastMessageAt` is UTC and no conversion to the business timezone happens before formatting. This is the exact bug the codebase has already fixed twice elsewhere — `app.py:500-502` carries the comment *"naive date.today() is UTC on Railway and drifts a day ahead after ~8 PM EST"*, and commits `308f12c` and `36f0064` fixed the same class of bug in the date-range filter.
- **Impact:** Any conversation whose last message lands after 8:00 PM EST gets `convo_date` = tomorrow. That value drives the texter-attribution join (`app.py:856-860`, `:3964-3968`) and the Detailed Dashboard date filter (`:3973-3978`). So evening work — which is exactly when the split-day shuffles this codebase invests so heavily in tracking occur — is attributed to the *next* day's assigned texter and disappears from the correct day's report.
- **Recommended Fix:** `.astimezone(TIMEZONE).strftime("%m/%d/%Y")`, importing `TIMEZONE` from `config.settings` as `gql_client.py:16` already does. Backfill existing rows with a one-off `UPDATE` that recomputes `convo_date` from the conversation's last message `sent_at` in the business timezone.
- **Priority:** Fix this sprint
- **Regression risk:** Backfilling moves historical conversations between days, changing past reports and possibly re-attributing them to a different texter. Run `_reattribute_day` (`app.py:2725`) over the affected range afterwards, and warn the team that yesterday's numbers will shift once.

---

### F24. Two writers store `trend_snapshots.audit_timestamp` with different timezone semantics — [CONFIRMED]
- **Severity:** Medium
- **Location:** `ai/scorer.py:735` (`_dt.now()`) vs `dashboard/app.py:1317` (`get_now()`)
- **Problem:** Both write the same `TIMESTAMPTZ` column via the same `ON CONFLICT ... DO UPDATE`, but `scorer.py` passes a **naive** `datetime.now()` while `app.py`'s backfill passes a timezone-aware `get_now()`. asyncpg interprets a naive datetime bound to `timestamptz` as UTC, so the scorer's value is stamped as if local wall-clock time were UTC — a 4-5 hour skew. The inline comment even says *"asyncpg needs a datetime object, not an isoformat string"*, showing the type was fixed but the timezone was not.
- **Impact:** `audit_timestamp` is inconsistent depending on which code path last touched the row. Low blast radius today because the UI keys on `audit_date`, but it silently corrupts any future time-of-day analysis.
- **Recommended Fix:** Use `get_now()` in `scorer.py:735`. Add a project rule — enforced by grep in CI — that `datetime.now()` without a `tz` argument is banned outside `config/settings.py`.
- **Priority:** Backlog
- **Regression risk:** None.

---

### F25. Firebase token refresh has no lock — up to 10 concurrent refreshes race — [PLAUSIBLE]
- **Severity:** Medium
- **Location:** `scraper/firebase_auth.py:32-47` (`AuthSession.ensure_fresh`), concurrency at `scraper/api_bot.py:259-262`
- **Problem:** `ensure_fresh` is a classic check-then-act with no `asyncio.Lock`. `api_bot` fans out `MSG_BATCH_SIZE = 10` concurrent `find_messages` calls, each of which awaits `_post` → `ensure_fresh` (`gql_client.py:163`). All ten can observe `is_expired` before any assignment lands. There is also no status check before `resp.json()`, so an HTML error page from Google raises a `JSONDecodeError` rather than the intended `RuntimeError`.
- **Impact:** Ten simultaneous refresh POSTs per expiry, with last-write-wins on `refresh_token`. Firebase refresh tokens are long-lived and reusable, so this is usually benign — but if Google rotates or rate-limits, a lost update leaves a stale token and the run fails mid-way with the failure surfacing as a per-conversation "Message fetch failed" that `api_bot.py:237-239` swallows into a warning.
- **Recommended Fix:** Add `self._lock = asyncio.Lock()` and double-check inside it. Add `resp.raise_for_status()` before `.json()`. Note the `field` import at line 11 is already unused.
- **Priority:** Backlog
- **Regression risk:** None. To confirm the race is live, log a counter each time the refresh branch is entered and check whether it exceeds 1 per hour per agent.

---

### F26. Rate limiting is likely keyed on Railway's proxy IP, and the bucket registry never evicts — [PLAUSIBLE]
- **Severity:** Medium
- **Location:** `dashboard/app.py:271` (`ip = request.client.host`), `config/rate_limiter.py:131` (`self._buckets`), `:184-194` (`status`)
- **Problem:** Three coupled issues. (1) `request.client.host` is the TCP peer. uvicorn's `ProxyHeadersMiddleware` only trusts `X-Forwarded-For` from `forwarded_allow_ips` (default `127.0.0.1`), which Railway's edge is not — so `client.host` is almost certainly the edge address for all external traffic. Every user then shares one token bucket per route prefix: `/api/` is `capacity=60, rate=6.0`. (2) `self._buckets` is an unbounded dict with no TTL and no eviction. (3) `GET /api/rate-limit/status` returns every bucket key, and `route_bucket` embeds the IP.
- **Impact:** In the shared-bucket case, several managers with the dashboard open (each polling `/api/status` every 3 s) can collectively cross 6 req/s and 429 each other, and any single user can trivially deny service to everyone. In the per-IP case, it is an unbounded memory leak. Either way, `/api/rate-limit/status` discloses client IP addresses to every authenticated user.
- **Recommended Fix:** First determine which case applies. Then key the bucket on `request.session["user_email"]` rather than IP — this is an authenticated internal tool; per-user is the meaningful unit and is unspoofable. Add LRU eviction or a periodic sweep of buckets idle for over an hour. Mask the key in the status response.
- **Priority:** Backlog
- **Regression risk:** Per-user keying means unauthenticated requests need a separate fallback bucket.

---

### F27. One conversation raising aborts an entire agent's scoring run — [CONFIRMED]
- **Severity:** Medium
- **Location:** `ai/scorer.py:362-363`
- **Problem:** `results = await asyncio.gather(*coros)` without `return_exceptions=True`. `_process_convo` calls `analyze_conversation` (which catches broadly), but the code *after* it in the same coroutine does not: `_filter_flags` (`:325`), `check_response_time` (`:337`), and the `int(_cid)` conversions (`:329`) all run unguarded.
- **Impact:** A single malformed conversation raises out of `gather`, discarding the successfully-scored results of every other conversation in the batch. `main.py:204` then marks the whole run failed, and `cleanup_failed_audits` deletes the now-unscored conversations as "ghost rows" (`db.py:574-603`), so the extracted data is thrown away too. One bad row costs the entire agent's audit.
- **Recommended Fix:** `await asyncio.gather(*coros, return_exceptions=True)`, then filter: log each exception with its contact name and keep the successful results. This also gives you the per-conversation failure signal that F6's status reporting needs.
- **Priority:** Fix this sprint
- **Regression risk:** Partial results will now be persisted where previously nothing was. That is desired, but `count_valid_scored_conversations` (`db.py:406`) should drive a "N of M scored" message so partial success is visible rather than silent.

---

### F28. The response-time flag re-scans the entire conversation history, not the audit window — [CONFIRMED]
- **Severity:** Medium
- **Location:** `ai/scorer.py:337` (`check_response_time(parsed, labels)`) vs `ai/analyzer.py:169` (`messages = filter_recent_messages(messages)`)
- **Problem:** `analyze_conversation` applies a 30-day rolling window before scoring — a deliberate design decision documented at `analyzer.py:16-19` (*"prevents stale history from skewing current scores"*). But `scorer.py` calls `check_response_time` with `parsed`, the **unfiltered** message list, and `check_response_time` returns the single worst gap across the whole thread (`response_time.py:149-172`).
- **Impact:** F17 is decided on data the rest of the audit deliberately excludes. A thread containing one slow reply from eight months ago is flagged "Critical Delay" today, tomorrow, and every day it is re-audited — and it deducts 25 points from Script Adherence each time (`scorer.py:345`). Agents are repeatedly penalized for a single historical lapse they cannot fix, and the flag never ages out. `_business_minutes_between` also iterates day-by-day (`response_time.py:116-126`), so a multi-year gap spins several hundred loop iterations per conversation.
- **Recommended Fix:** Pass the same windowed message list. Export `filter_recent_messages` from `analyzer.py` and apply it in `scorer.py` before calling `check_response_time`, or better, have `analyze_conversation` return the filtered list so both consumers provably use identical input. Add an early bail in `_business_minutes_between` when the span exceeds the window.
- **Priority:** Fix this sprint
- **Regression risk:** F17 flag counts will drop, possibly sharply. That is a correction, not a regression — but tell the team before the numbers move, and spot-check a handful of currently-flagged conversations.

---

### F29. `contacts` has no unique constraint and is inserted via check-then-act — [CONFIRMED]
- **Severity:** Medium
- **Location:** `database/db.py:247-257` (`_upsert_contact`), `database/schema.sql:34-39`
- **Problem:**
  ```python
  row = await conn.fetchrow("SELECT id FROM contacts WHERE name = $1 LIMIT 1", name)
  if row: return row["id"]
  row = await conn.fetchrow("INSERT INTO contacts (name) VALUES ($1) RETURNING id", name)
  ```
  `contacts.name` has no `UNIQUE` constraint, and up to 20 audit subprocesses run concurrently. The match is also case- and whitespace-sensitive, while every consumer normalizes with `LOWER(TRIM(...))` (`app.py:446`, `:692`, `:781`).
- **Impact:** Two subprocesses scraping the same lead create two `contacts` rows with the same name. Every downstream join then multiplies, and the Python-side dedupe by lowercased name (`app.py:869-876`) masks it by silently dropping one of the two conversations — so a real audited conversation can vanish from the dashboard.
- **Recommended Fix:** Add `CREATE UNIQUE INDEX idx_contacts_name_lower ON contacts (LOWER(TRIM(name)))` after de-duplicating existing rows, and replace the check-then-act with an `INSERT ... ON CONFLICT ... RETURNING id`.
- **Priority:** Backlog
- **Regression risk:** The de-duplication migration must re-point `conversations.contact_id` at the surviving row before the unique index can be created. Do it in one transaction and count rows before and after.

---

### F30. `dashboard/templates/index.html` is 4,930 lines of dead, actively-maintained code — [CONFIRMED]
- **Severity:** Medium
- **Location:** `dashboard/templates/index.html`
- **Problem:** `app.py` serves only `static/index.html` (`GET /` at `:1356`). No Python file in the repo contains the string `templates`, there is no Jinja2 `TemplateResponse` anywhere, and `jinja2` is a dependency only for the reports path. Yet `git log` shows `dashboard/templates/index.html` was modified **32 times** — more than `static/index.html` (19). Its last edit was `6d280ba` (2026-07-30), well after it stopped being served.
- **Impact:** This is a live correctness trap, not just clutter. Someone — human or agent — searching for a UI string will find it in both files, edit the wrong one, and observe no change in the browser. Given 32 commits, that has probably already happened.
- **Recommended Fix:** Delete it. Git history preserves it. Before deleting, diff the two for any feature present only in the template version (it has 31 `fetch(` sites vs 45 in static, so it is likely a strict subset).
- **Priority:** Fix this sprint (a 5-minute change with real ongoing cost)
- **Regression risk:** None, confirmed by grep — nothing references it.

---

### F31. README and CLAUDE.md describe a Groq architecture that no longer exists — [CONFIRMED]
- **Severity:** Medium
- **Location:** `README.md:3-22`, `CLAUDE.md` ("AI Key Pool", "AI Key Pool Model (May 2026)"); actual state at `ai/analyzer.py:1-9`, `ai/prompts.py:1-9`
- **Problem:** The docs describe evaluation "using Groq AI (Llama 3.3 70B)", a "Tier 4: Groq AI fallback", and a "Shared AI Key Pool" with "up to 140 key attempts". In reality `ai/analyzer.py` opens with *"ML-only conversation analyzer. No LLM calls"*, `ai/prompts.py` records that the prompt builder *"was removed when Groq was decommissioned"*, `groq` is absent from `requirements.txt`, `/api/ai/status` returns hardcoded `{"mode": "ml-only", "total_keys": 0}` (`app.py:1935-1942`), `dream_worker._call_groq_reflect` returns `[]`, and the `api_keys` table (`schema.sql:224-233`) is dead. Tier 4 is now a deterministic rule generator, not a Groq fallback.
- **Impact:** `CLAUDE.md` is loaded as authoritative context by every AI agent working on this repo, and README is the onboarding document for humans. Both currently instruct the reader to reason about a key-rotation subsystem that does not exist and to expect an LLM safety net behind the rule engine that is not there. This review's own brief asked for an audit of "LLM key rotation/exhaustion" — a subsystem that was deleted. That is the concrete cost, already paid once.
- **Recommended Fix:** Rewrite the AI sections of both files to describe the ML-only pipeline: T1 phrase rules (live), T2 kNN and T3 classifier (built but disabled, see F9/F10), T4 deterministic generator as the terminal tier that produces essentially all output. Delete the "AI Key Pool Model" section from `CLAUDE.md`. Drop the `api_keys` table or mark it deprecated. Also correct the README's "911+ examples" — `manifest.json` says 1,576 vectors.
- **Priority:** Fix this sprint
- **Regression risk:** None.

---

### F32. The kNN index and classifier now train on the rule engine's own output — [CONFIRMED]
- **Severity:** Medium
- **Location:** `ai/prefilter/index_builder.py:73-76`
- **Problem:** The training-set filter excludes tiers 1-3 but not tier 4:
  ```sql
  AND COALESCE(cs.source, 'groq') NOT IN ('prefilter_t1','prefilter_t2','prefilter_t3')
  -- Also include T4 results (deterministic, high-quality)
  ```
  Since Groq was decommissioned, `scorer.py:650-651` writes `source = 'prefilter_t4'` for essentially every new score.
- **Impact:** The independent ground truth (Groq) is gone, so the corpus is now asymptotically 100% T4 output. Training a kNN index and a classifier to approximate the rule engine using labels produced *by that rule engine* adds no information — it can only learn to reproduce the rules, including their errors, while the historical Groq rows are progressively diluted. `semantic_learner.auto_promote` amplifies this by auto-promoting T4-scored "clean" conversations into the index with no human review (`semantic_learner.py:208-220`). Any systematic T4 false-negative becomes self-reinforcing.
- **Recommended Fix:** Restrict training to human-validated data — the mechanism already exists (`PREFILTER_REQUIRE_VALIDATION`, `index_builder.py:88+`, `validation_log`). Set it to `true` once ~50 manager validations exist, as `config/settings.py:144-148` already advises. Until then, exclude `prefilter_t4` from the training set so the corpus stays anchored on the historical Groq rows.
- **Priority:** Backlog
- **Regression risk:** Excluding T4 rows shrinks the corpus sharply (`manifest.json` shows only 660 training examples already, against the code's own ≥50 minimum at `train.py:169-173`). Verify the remaining set is large enough before rebuilding.

---

### F33. Learned rules are written to git-tracked files on an ephemeral filesystem — [CONFIRMED]
- **Severity:** Medium
- **Location:** `ai/learned_rules.py:57-88`, `ai/dream_worker.py:226`; paths at `config/settings.py:80-81`; consumer at `ai/prefilter/tier4_flag_generator.py:607`
- **Problem:** `LEARNED_RULES_PATH = PROJECT_ROOT / "ai" / "learned_rules.json"` and `DREAM_STATE_PATH` are both **tracked in git** and both written at runtime by the detached reflection process (`scripts/post_audit_reflection.py`). `tier4_flag_generator` reads the result via `get_t4_suppressed_flags` — so these files directly change scoring output. `logs/sessions.jsonl` (`ai/session_logger.py:21`) has the same problem.
- **Impact:** On Railway the container filesystem is ephemeral: every redeploy resets `learned_rules.json` to whatever is committed, silently discarding all rules learned since the last deploy. Locally, running an audit dirties the working tree and rules get committed accidentally or lost in a `git checkout`. The source data lives in Postgres (`flag_feedback`), so the derived state is the only thing on disk — and it is the part that gets lost.
- **Recommended Fix:** Move `learned_rules` and `dream_state` into Postgres tables. The write path is already atomic-rename-based (`learned_rules.py:73-81`), so swapping the storage backend is contained. Remove both JSON files from git tracking and add them to `.gitignore` as a stopgap.
- **Priority:** Backlog
- **Regression risk:** Rules currently committed in `ai/learned_rules.json` must be seeded into the new table or active suppressions silently turn back on.

---

### F34. The Detailed Dashboard preview snippet is always empty — [CONFIRMED]
- **Severity:** Low
- **Location:** `dashboard/app.py:3943-3949`
- **Problem:** The correlated subquery filters `AND m.sender = 'agent'`, but `messages.sender` never contains that literal — `api_bot._build_transcript:66` writes the agent's first-name token (`"Noah"`, `"Resva1006"`) or `"Contact"`. Same root cause as F10.
- **Impact:** Every card in the Detailed Dashboard shows "No messages" instead of a preview (`index.html:5051`), making the list far less scannable. It also runs a correlated subquery per row that can never match, on a table with no index on `(conversation_id, sender)`.
- **Recommended Fix:** Fix as part of F10's shared `is_outgoing` predicate.
- **Priority:** Backlog (bundle with F10)
- **Regression risk:** Previews will start rendering agent message text — which is exactly why the `esc()` fix in F2 must land first.

---

### F35. The nightly reset is unguarded, hardcodes its own timezone, and is not atomic — [CONFIRMED]
- **Severity:** Low
- **Location:** `dashboard/app.py:73-110` (`_scheduled_reset_all`)
- **Problem:** It hardcodes `pytz.timezone("US/Eastern")` instead of `config.settings.TIMEZONE` (the third independent hardcoding of Eastern — see also `ai/scorer.py:163` and `ai/response_time.py:101`). Its two destructive statements (`DELETE FROM audit_scores`, `UPDATE conversations SET is_archived = TRUE`) are not in a transaction. And it runs unconditionally in every app instance, with nothing guarding against multiple replicas.
- **Impact:** A failure between the two statements leaves every agent's summary deleted but conversations unarchived, so the next `/api/agents` shows blank scores against live conversations. Changing `TZ` moves the app's business day but not the reset. This function also interacts badly with F11 — after it runs, the next audit deletes all remaining `audit_scores`.
- **Recommended Fix:** Wrap in `async with conn.transaction()`. Use `TIMEZONE` from settings. Guard with a Postgres advisory lock (`pg_try_advisory_lock`) — the pattern already exists at `db.py:195`.
- **Priority:** Backlog
- **Regression risk:** None.

---

### F36. Dead code and stale contracts across the API and DB layers — [CONFIRMED]
- **Severity:** Low
- **Location:** `database/db.py:419-471` (`save_conversation_score`), `dashboard/app.py:494-525` (`/api/flags/realtime`), `:1927-1942` (`/api/ai/status`), `database/schema.sql:224-233` (`api_keys`)
- **Problem:** Several stale artifacts:
  - `Database.save_conversation_score` has zero callers (`scorer.py:669` inlines its own INSERT). It would fail if called — it binds a Python list to the `red_flags` JSONB column without a `::jsonb` cast.
  - `/api/flags/realtime` returns a bare array and, on error, `return []` — indistinguishable from "no flags". It also violates the `{"success": ..., "data": ...}` envelope `CLAUDE.md` mandates, as does `/api/agents`.
  - `/api/ai/status` returns hardcoded zeros for a key pool that no longer exists.
  - `api_keys` table and index are created on every boot for a decommissioned subsystem.
  - Migration `001` declares `conversation_scores.source ... CHECK (source IN ('groq','prefilter_t1'..'t4'))` while `schema.sql:302` declares it nullable with no constraint and its comment lists a sixth value, `'groq_override'` — so on a database where 001 ran, writing that value would fail.
- **Impact:** Individually minor; collectively they mean a reader cannot trust that code in this repo is reachable or that an endpoint's shape matches the documented convention. The `/api/flags/realtime` error-as-empty-array is the one with real behavioral cost: a database error renders as "0 flags" in the header counter.
- **Recommended Fix:** Delete `save_conversation_score`, `/api/ai/status`, and the `api_keys` DDL. Make `/api/flags/realtime` return the standard envelope and a 500 on error, updating `index.html:3365-3374` to match. Reconcile the `source` column definition between `001` and `schema.sql`.
- **Priority:** Backlog
- **Regression risk:** The `/api/flags/realtime` shape change requires a matching client edit in the same commit.

---

### F37. Config sprawl across three deploy manifests and duplicated timezone/label constants — [CONFIRMED]
- **Severity:** Low
- **Location:** `Dockerfile`, `Procfile`, `nixpacks.toml`; duplicated constants at `dashboard/app.py:1109` vs `scraper/api_bot.py:20`
- **Problem:** Three build/start manifests coexist with different behavior. Only the `Dockerfile` sets `ENV TZ="America/New_York"`; `nixpacks.toml` has a build phase and no start command, deferring to `Procfile`. Which one Railway uses determines whether `TIMEZONE_STR = os.getenv("TZ", "America/New_York")` gets its value from the environment or its default — and whether naive `datetime.now()` calls (F24) resolve to Eastern or UTC. Separately, `_ALL_LABEL_FILTER_VALUES` is defined twice with the same members in `app.py` and `api_bot.py`, alongside two near-identical `normalize_label_filter` implementations — and `api_bot.py:31` explicitly notes it is a copy.
- **Impact:** Deploy behavior depends on which manifest wins, which nobody can determine by reading the repo. The duplicated label-normalization logic is a divergence risk: a fix applied to one is silently absent from the other, and they sit on opposite sides of the dashboard-to-subprocess boundary where they must agree.
- **Recommended Fix:** Pick one deployment path (the Dockerfile is the most explicit) and delete the other two. Move `normalize_label_filter` and `_ALL_LABEL_FILTER_VALUES` into `config/settings.py` and import from both sites. Set `TZ` explicitly as a Railway service variable. Unify the three hardcoded `US/Eastern` sites on `settings.TIMEZONE`.
- **Priority:** Backlog
- **Regression risk:** Removing a manifest changes how Railway builds. Verify on a staging service first.

---

### F38. `firebase_auth` reads a required env var at import time, creating a hidden import-order dependency — [CONFIRMED]
- **Severity:** Low
- **Location:** `scraper/firebase_auth.py:15`
- **Problem:** `FIREBASE_API_KEY = os.environ["FIREBASE_API_KEY"]` executes at module import and raises `KeyError` if unset. The module does not call `load_dotenv` itself — it depends on `config.settings` having been imported first. But `gql_client.py` imports it in the wrong order: `from scraper.firebase_auth import AuthSession` (line 15) precedes `from config.settings import ...` (line 16).
- **Impact:** It works today only because `main.py:43` imports `config.settings` before `scraper.queue_manager` at line 46. Any new entry point that imports the scraper first crashes with a bare `KeyError` — no message explaining that `.env` was not loaded.
- **Recommended Fix:** Move the key into `config/settings.py` alongside `DATABASE_URL`, with the same explicit `RuntimeError` and a message naming the file to fix.
- **Priority:** Backlog
- **Regression risk:** None.

---

### F39. `find_conversations` can page through an entire inbox without bound — [PLAUSIBLE]
- **Severity:** Low
- **Location:** `scraper/gql_client.py:231-291`
- **Problem:** `while len(eligible) < limit * 2:` with three exit conditions — empty page, `hasNext` false, and a date-boundary check that only applies `if date_start:`. With `date_filter="all_time"` (`_date_range_for_filter` returns `None, None` at line 123) combined with a restrictive `include_labels` set, the date-boundary break is skipped and the loop pages 50 conversations at a time through the entire inbox until `hasNext` is false.
- **Impact:** A user selecting "All time" plus a rarely-used label triggers an unbounded sequence of GraphQL calls against SmarterContact. There is no page cap and no overall deadline; the only backstop is `_MAX_RUN_MINUTES = 45` in the dashboard (`app.py:1000`), which kills the process rather than degrading gracefully. Risk of tripping SmarterContact's own rate limiting.
- **Recommended Fix:** Add a `max_pages` guard (e.g. 200) and log a warning when hit so the truncation is visible rather than silent.
- **Priority:** Backlog
- **Regression risk:** Very large legitimate scrapes would be truncated. Set the cap generously and make the warning loud.

---

### F40. `AGENT_ROSTER` is a process-local cache used for write-path validation — [CONFIRMED]
- **Severity:** Low
- **Location:** `dashboard/app.py:1231` (module global), validation at `:2583` and `:2933`
- **Problem:** The roster is loaded once at startup (`app.py:165`) and refreshed only by `/api/roster` add/delete within the same process. It is then used as an authoritative validator on the assignment write path: `if texter not in AGENT_ROSTER: raise ValueError(...)`.
- **Impact:** If the `texters` table is modified out-of-band (a second replica, a manual SQL insert, a migration script), assignment saves reject valid texters with a confusing error until the app restarts.
- **Recommended Fix:** Validate against the database inside the same transaction as the write — `SELECT 1 FROM texters WHERE name = ANY($1)` — and keep `AGENT_ROSTER` only as a read cache for `GET /api/roster`.
- **Priority:** Backlog
- **Regression risk:** One extra query per save. Negligible.

---

## 1. Executive Summary

The engineering here is genuinely better than average in several places, and it is worth naming them before the criticism: the frontend escapes output almost everywhere via a consistent `esc()` helper (F2 is a single isolated lapse in 87 `innerHTML` sites); there is no SQL injection anywhere — every query is parameterized, and the two f-string-built queries clamp their inputs correctly; there are no bare `except:`, no mutable default arguments, no `eval`/`exec`/`shell=True`, and no secrets in tracked source; and the `assignment_periods` time-ranged ownership subsystem is careful, well-commented, properly transactional, and correctly locked. That subsystem is the strongest code in the repository.

The problems cluster in three places. **First, correctness of the product's core output.** F1 lets a single reviewer misclick permanently disable a compliance flag for every agent — in a compliance-auditing product, that is the most serious finding in this report. F6 reports failed scoring runs as successful. F18 means a documented business rule has never once fired. F28 penalizes agents daily for a single months-old lapse. Managers are currently making decisions from numbers that are wrong in ways nothing surfaces. **Second, the data layer is not defended.** Destructive endpoints are non-transactional and one of them (F12) reliably destroys an agent's history and then fails; a finishing audit deletes other agents' scores (F11); dedupe on re-ingestion has never worked (F7), so the tables grow by a full copy on every run, steadily degrading the unbounded scans in F16 and F17. **Third, and underneath all of it: there are no tests, no CI, and `tests/` is gitignored (F20).** Ten of the confirmed bugs here would have been caught by a single unit test at write time.

The single biggest systemic risk is the combination of F20 with F19: the deterministic rule engines now produce **100%** of the product's output (Groq was decommissioned — a change the README and CLAUDE.md still do not reflect, F31), the largest of them is a 1,037-line function, `git log` shows it is the most frequently edited file in the repo, and nothing verifies that any edit preserves prior behavior. Every scoring change is currently shipped on hope. Fix F1 today, then build the golden-transcript harness before touching anything else — because most of the remaining fixes change scoring output, and without a baseline you cannot tell a correction from a regression.

---

## 2. Critical Issues

| ID | Title | Location | Impact |
|----|-------|----------|--------|
| F1 | Global flag suppression via fuzzy substring match | `ai/scorer.py:67-82,119-159` | One "Not Valid" click permanently disables a compliance flag for every agent |
| F2 | Stored XSS in Detailed Dashboard cards | `dashboard/static/index.html:5065-5082` | Scraped contact name executes JS with full admin session rights |

---

## 3. High-Priority Issues

| ID | Title | Location | Impact |
|----|-------|----------|--------|
| F3 | Plaintext SmarterContact passwords | `database/schema.sql:11`, `dashboard/app.py:2059` | DB leak yields live logins to all lead SMS history |
| F4 | Firebase API key in git history | `scraper/firebase_auth.py` (history) | Key still retrievable from every clone; needs rotation |
| F5 | No authorization tiers; `require_admin` is a no-op | `dashboard/app.py:356-364` | Any allowlisted user can delete all agents and data |
| F6 | Failed scoring runs report success (unreachable code) | `main.py:215-251` | Green "Done" badge on audits that produced nothing |
| F7 | `mark_chat_audited` never called — dedupe dead | `database/db.py:497`, `scraper/api_bot.py:227` | Every re-run duplicates all rows; unbounded growth |
| F8 | Prefilter telemetry tables absent from schema | `database/schema.sql`, `ai/prefilter/pipeline.py:255` | ML evaluation and promotion gates have no data |
| F9 | Classifier artifact 1.8.0 vs pinned sklearn 1.7.2 | `ai/prefilter/artifacts/manifest.json` | T3 loads corrupt or fails; only a WARNING |
| F10 | Embeddings cannot distinguish agent from lead | `ai/prefilter/embedder.py:145-159` | T2/T3 accuracy capped; train/serve skew |
| F11 | Cleanup deletes other agents' `audit_scores` globally | `database/db.py:620-629` | Silent cross-agent data loss during parallel runs |
| F12 | Destructive endpoints non-atomic; agent delete FK-aborts | `dashboard/app.py:2131-2143` | History destroyed, account survives, 500 returned |
| F16 | `/api/agents` full-scans all conversations + scores | `dashboard/app.py:672-758` | Degrades linearly forever; compounded by F7 |
| F19 | 1,037-line `evaluate()` holds core scoring rules | `ai/prefilter/tier1_phrases_v2.py` | Most-edited, least-testable code in the repo |
| F20 | Zero tests, no CI, `tests/` gitignored | repo-wide | No regression net under any of the above fixes |
| F21 | New Postgres connection per conversation | `ai/prefilter/semantic_learner.py:111` | ~350 connections worst case vs default max 100 |

---

## 4. Medium/Low-Priority Improvements

**Timezone correctness**
- F23 — `convo_date` computed in UTC; evening conversations filed on the next day and mis-attributed.
- F24 — `trend_snapshots.audit_timestamp` written naive by the scorer, aware by the dashboard; 4-5 h skew.
- F35 — Nightly reset hardcodes `US/Eastern` (third independent hardcoding) and is not atomic.

**Query and payload bounds**
- F17 — `/api/detailed-dashboard` has no `LIMIT` and a non-sargable `TO_DATE` predicate.
- F39 — `find_conversations` pages unbounded under `all_time` + label filter.

**Correctness of scoring**
- F18 — The 30-minute unread rule has never fired; its `EASTERN` constant is frozen at import.
- F27 — `asyncio.gather` without `return_exceptions` discards a whole agent's results on one bad row.
- F28 — F17 response-time scans full history, bypassing the deliberate 30-day window.
- F32 — Index and classifier now train on the rule engine's own output; self-reinforcing.

**Data integrity**
- F29 — `contacts` check-then-act with no unique constraint; duplicates silently drop conversations from the UI.
- F13 — `schema.sql` re-seeds deleted `blacklist_labels` on every container restart.
- F33 — Learned rules written to git-tracked files on an ephemeral filesystem; lost every redeploy.

**Auth, session, and disclosure**
- F14 — Full SPA plus the owner's personal email served unauthenticated at `/static/index.html`.
- F15 — No 401 handling in the client; session expiry leaves a permanent error and a 3 s poll loop.
- F25 — Firebase token refresh has no lock; up to 10 concurrent refreshes.
- F26 — Rate limiting likely keyed on the proxy IP; unbounded bucket dict; IPs disclosed via status endpoint.

**Dead code and drift**
- F30 — `dashboard/templates/index.html`: 4,930 dead lines, edited 32 times.
- F31 — README and CLAUDE.md describe a Groq architecture that no longer exists.
- F34 — `preview_snippet` matches `sender = 'agent'`, which never occurs; always empty.
- F36 — Dead `save_conversation_score`, `/api/ai/status`, `api_keys` table; `/api/flags/realtime` returns errors as `[]`.
- F37 — Three competing deploy manifests; duplicated label-normalization logic across the process boundary.
- F38 — `firebase_auth` reads a required env var at import, creating a hidden import-order dependency.
- F40 — `AGENT_ROSTER` in-memory cache used as an authoritative write-path validator.

**Architecture**
- F22 — Embedding model, FAISS index, and classifier share the web server process.

---

## 5. Quick Wins

| Effort | Finding | Action |
|--------|---------|--------|
| 5 min | F30 | `git rm dashboard/templates/index.html` — 4,930 dead lines, actively edited by mistake |
| 10 min | F2 | Wrap five interpolations in `esc()` at `static/index.html:5065-5082` — closes the stored XSS |
| 10 min | F11 | Add `AND s.agent_id = $1` to the `DELETE FROM audit_scores` in `db.py:620` |
| 10 min | F6 | Move the `if final_status:` block inside the `finally` in `main.py:243` |
| 15 min | F12 | Wrap four handlers in `conn.transaction()`; add the `validation_log` delete |
| 15 min | F27 | `asyncio.gather(*coros, return_exceptions=True)` + filter and log |
| 15 min | F14 | Move `index.html` out of `dashboard/static/`; keep serving via authenticated `GET /` |
| 20 min | F1 | Delete `_load_invalid_flag_patterns` and its two call sites — the highest-value change in this list |
| 20 min | F15 | Add an `api()` fetch wrapper that redirects to `/login` on 401 |
| 20 min | F23 | `.astimezone(TIMEZONE)` in `api_bot.py:245` (backfill separately) |
| 30 min | F8 | Fold migration 001's DDL into `schema.sql`; delete `002_prefilter.sql` |
| 30 min | F13 | Move `blacklist_labels` seeding behind an "only if empty" guard |
| 30 min | F31 | Rewrite the AI sections of README and CLAUDE.md to describe the ML-only pipeline |
| 1 h | F16 | Add the partial index and a 10-second cache on `_compute_review_stats_bulk` |

F1, F2, F6, F11, and F12 together are roughly one focused hour and resolve two Critical and three High findings.

---

## 6. Architecture Recommendations

In dependency order.

**1. Establish a golden-transcript test harness before anything else.** Every remaining recommendation changes scoring output. Without a captured baseline you cannot distinguish a fix from a regression, and given F19's 1,037-line function, code review alone will not tell you. This is a prerequisite, not a parallel track.

**2. Introduce a single `is_outgoing` source of truth.** Right now four places independently decide whether a message came from the agent — `db.py:80`, `embedder.py:152`, `response_time.py:87`, and `index_builder.py:59` — and they disagree, because the scraper writes a human name into `messages.sender` (`api_bot.py:66`). Add a boolean `messages.is_outgoing` column populated at ingest and have all four read it. This single change resolves F10, F34, and the latent inconsistency behind F28's evidence selection. It is the highest-leverage structural fix in the codebase because it converts an inferred property into a stored fact.

**3. Fix the ingestion identity model.** `conversations` has no natural key, so `save_extraction` appends unconditionally (F7) and every read path compensates with Python-side de-duplication by lowercased contact name. That compensation is why the growth is invisible and why F29's duplicate contacts silently drop conversations. Add `UNIQUE(agent_id, contact_id, audit_date)`, convert the insert to an upsert, restore `mark_chat_audited`, and then delete the Python de-duplication. This unblocks the set-based rewrite of F16 and makes F17's pagination tractable.

**4. Replace the `X-Admin-Token` gate with session-based roles.** The current mechanism (F5) cannot be enabled without breaking the UI, so 17 mutating endpoints are effectively ungated. A `role` column on `tool_access` plus a `require_role(min_role)` dependency is a contained change that gives the reviewers-who-only-review the read-only access they should have had from the start.

**5. Move derived state out of the container filesystem.** `learned_rules.json`, `dream_state.json`, and `logs/sessions.jsonl` (F33) are the only durable outputs of the learning loop and they are erased by every redeploy. Their inputs already live in Postgres; move the outputs there too.

**6. Split the embedding service out of the web process** (F22) — but only if F16 and F17 do not already resolve the CPU pressure. Measure first.

**Be honest about what is *not* worth refactoring:**

- **The `assignment_periods` subsystem should not be touched.** It is transactional, correctly locked with stable ordering to avoid deadlocks, defensive about the `btree_gist` extension, and unusually well-commented about *why* rather than *what*. It is the best code here. Leave it alone.
- **Do not rewrite `tier1_phrases_v2.py` or `label_validator.py` wholesale.** They encode years of accumulated domain judgment that exists nowhere else — no spec, no tests, and (F31) not even accurate documentation. A rewrite would silently discard rules nobody remembers are load-bearing. Extract one rule at a time behind the harness from step 1.
- **Seriously consider deleting Tier 2 and Tier 3 rather than fixing them.** They exist to reduce Groq API cost. Groq is gone (F31), so they now save nothing. They are disabled in production, their artifacts are unloadable (F9), their embeddings are semantically broken (F10), their telemetry was never recorded (F8), and their training data has become self-referential (F32). Fixing all of that is weeks of work for a subsystem whose original justification no longer applies. The honest options are (a) delete `tier2_embedding.py`, `tier3_classifier.py`, `train.py`, `index_builder.py`, `semantic_learner.py`, and the FAISS/sklearn/torch dependencies — which would also eliminate F9, F21, F22, and F32 outright and cut both the Docker image and Railway CPU load substantially — or (b) commit to fixing F10 and F8 first and re-evaluating with real measurements. What is not defensible is the current state: paying the full maintenance and runtime cost of an ML pipeline that is switched off and whose artifacts do not load.
- **The single-worker uvicorn deployment is correct** and should stay. `running_processes`, `_snapshotted`, `AGENT_ROSTER`, and the rate-limiter buckets are all process-local mutable state; adding workers would break all four. If you ever need to scale, fix that state first — do not just raise the worker count.

---

## 7. Testing Recommendations

There is no harness at all, so start with what needs no infrastructure. Remove `tests/` from `.gitignore`, add `pytest`, `pytest-asyncio`, and `pytest-postgresql` to `requirements.txt`, and add a GitHub Actions workflow that runs `pytest` on push.

**Tier 1 — pure functions, no fixtures needed. Highest value per hour.**

`tests/test_scorer_filters.py` — `ai/scorer.py::_filter_flags`:
- `_filter_flags(["Continued texting after explicit opt-out."], {"continued texting after explicit opt-out."})` → `[]` (exact match suppresses)
- `_filter_flags(["Gave up after first no with zero rebuttal."], {"continued texting after explicit opt-out."})` → unchanged (no cross-contamination)
- `_filter_flags(["none", "N/A", "-"], set())` → `[]` (null sentinels stripped)
- `_filter_flags(["Rude"], {"Rude tone throughout the entire conversation"})` → `["Rude"]` — **this currently fails**, because the 41-char pattern contains the 4-char flag. Write it as the assertion you want and let it drive the fix.

`tests/test_response_time.py` — `ai/response_time.py`:
- `_labels_match(["FUI, WL Drip, Not Interested"])` → `False` (terminal label wins over the in-scope track)
- `_labels_match(["Lead"])` → `True`; `_labels_match(["Sold"])` → `False`
- `_business_minutes_between` across an 8 PM → 8 AM boundary → excludes overnight
- `check_response_time` with a lead message at 10:00 and an agent reply at 10:12 → `threshold_tag == "yellow"`; at 10:40 → `"critical"`
- A gap spanning 200 days → assert the result respects the audit window (F28's fix)

`tests/test_db_parsing.py` — `database/db.py`:
- `_parse_msg_datetime({"date": "Thursday, March 26, 2026", "time": "05:59 PM"})` → `2026-03-26 17:59+00:00`
- `_parse_msg_datetime({"time": "05:59 PM"})` → `None` (never assume today)
- `_parse_msg_datetime({"timestamp": "2026-03-26T17:59:00Z"})` → aware UTC
- `_resolve_texter(periods, None, "Noah")` → `("Noah", "inferred")`; timestamp in a gap → `(None, "unassigned")`
- `_is_outgoing("Noah")` → `True`; `_is_outgoing("Contact")` → `False`; `_is_outgoing("")` → `False`

`tests/test_assignments.py` — `dashboard/app.py`, pure helpers, no DB:
- `_seg_instant(day, "24:00", is_end=True)` → next midnight; `is_end=False` → `ValueError`
- `_normalize_segments` with `[("A","09:00","12:00"),("B","11:00","14:00")]` → raises on overlap
- Touching same-texter ranges merge; empty `texter_name` is dropped, not rejected
- `_editable_day_segments` collapses a sub-minute period rather than emitting `5:27 PM–5:27 PM`

**Tier 2 — golden transcripts. This is the harness that unblocks F19.**

Export 200 real conversations spanning every flag class into `tests/fixtures/transcripts/*.json` (scrub contact names and phone numbers). Then:

```python
@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_scoring_is_stable(fixture, snapshot):
    result = analyze_conversation(fixture["messages"], fixture["agent"],
                                  assigned_labels=fixture["labels"],
                                  funnel_tier=fixture["tier"])
    assert {"red_flags": sorted(result["red_flags"]),
            "label_should_be": result["label_should_be"],
            "funnel_stage_reached": result["funnel_stage_reached"]} == snapshot
```

Capture the snapshots **before** making any of the fixes in this report. Include specific regression cases drawn from `git log`, since bugs recur where they happened before: the kid-DNC-beats-Wrong-Number rule (`cb9c189`), the bluffer full-value-stance guard (`9857445`), the F14/F16 metadata mixup (`5a559a7`), the F17 push-stage label match (`7830e4d`), and the sold-pivot reversal (`245eede`).

**Tier 3 — database integration, one `pytest-postgresql` fixture that applies `schema.sql`.**
- `save_extraction` twice with identical input → asserts one conversation row, not two (F7)
- `cleanup_failed_audits(agent_id=1)` → agent 2's `audit_scores` row survives (F11)
- `api_delete_agent` on an agent with a `validation_log` row → either fully succeeds or fully rolls back (F12)
- Boot `schema.sql`, delete a `blacklist_labels` row, re-run `schema.sql` → the row stays deleted (F13)
- Concurrent `_upsert_contact("John Smith")` from two connections → one contact row (F29)

**Untestable as written, and the minimum refactor to fix it:**
- `tier1_phrases_v2.evaluate` (1,037 lines) — extract one rule at a time into `(messages, labels, tier) -> Flag | None` predicates; each becomes independently testable the moment it is extracted.
- `score_agent_conversations` mixes analysis, aggregation, and three separate DB writes in one 600-line function, and reads `DATABASE_URL` from module scope. Split into `compute_agent_scores(conversations, config) -> AuditResult` (pure) and `persist_audit_result(conn, result)` (I/O). The pure half then covers the merge/weighted-average logic at `scorer.py:536-615`, currently entirely unverified.
- `_check_overdue_unreads` depends on import-time `EASTERN` and wall-clock `now`. Inject `now` as a parameter (F18).

---

## 8. Developer Experience Improvements

**Setup friction.** There is no documented local setup path beyond "copy `.env.example`". A new developer must discover for themselves that `DATABASE_URL` raises at import if unset, that `FIREBASE_API_KEY` raises a bare `KeyError` at a different import (F38), that `SESSION_SECRET_KEY` silently generates an ephemeral key that logs them out on every reload, and that the local port must be 8000 to match Google's registered OAuth redirect URI. Add a `make dev` target or a `docs/SETUP.md` that walks through Postgres, `.env`, and the OAuth console registration in order, and convert every required-variable failure into an explicit `RuntimeError` naming the variable and the file — the pattern `config/settings.py:56-60` already uses for `DATABASE_URL`.

**Missing tooling.** No linter, no formatter, no type checker, no pre-commit hooks, no CI (F20). `ruff` alone would catch the unused `field` import in `firebase_auth.py:11`, the shadowed `timedelta` re-import in `app.py:3774`, and the repeated `from datetime import date as _date` inside twelve separate function bodies. `mypy` in non-strict mode would likely have caught F6's unreachable code. Add `ruff` + `gitleaks` as pre-commit hooks and a CI job that runs both plus `pytest`.

**Local/prod parity.** Three deploy manifests with divergent behavior (F37) mean nobody can reproduce production locally with confidence — particularly around `TZ`, which silently changes the meaning of every naive datetime in the codebase. Pick one manifest. Add a `docker-compose.yml` with Postgres so local runs match production's database version and extension set (`btree_gist` availability materially changes whether the `assignment_periods` overlap constraint exists).

**Debuggability.** Logs have no correlation ID, so with up to 20 concurrent audit subprocesses writing into separate files (`main.py:54-57`), tracing one conversation across the scraper, scorer, and prefilter means manually correlating timestamps. Add a run UUID to `extra_env` in `app.py:1807` and include it in every log format string. Several genuinely important failures are logged below `INFO` and are therefore invisible in production: `pipeline.py:274` (prefilter decision recording — the reason F8 went unnoticed), `api_bot.py:231` (dedup check failure), and the seven `logger.debug("swallowed: %r", _e)` sites. Raise the ones that indicate a broken invariant to `WARNING`.

**Documentation.** Beyond F31's Groq drift: `docs/` contains five design specs under `docs/superpowers/` dated April 2026 that describe systems since rewritten, with nothing marking them historical. `CLAUDE.md` still points the knowledge base at `C:\Users\vos\Desktop\obsidian_brain` — a Windows path, on a macOS machine. Add a "Status: superseded" header to stale specs and fix the vault path.

---

## 9. Suggested Next Steps

**Week 1 — Stop the bleeding.** Ship the Quick Wins that need no test coverage because they are provably-correct local changes: F1 (delete global flag suppression), F2 (escape five interpolations), F11 (add the agent scope), F6 (move the status write), F12 (add transactions and the missing delete), F27 (`return_exceptions=True`). Rotate the Firebase key (F4) — an operations task, do it in parallel. Delete `dashboard/templates/index.html` (F30). Then immediately export 200 real conversations and capture golden-transcript snapshots **from the code as it stands after these fixes** — this is the baseline everything later is measured against. Communicate to the team that flag counts will rise once F1 lands, and why.

**Week 2 — Build the net.** Stand up `pytest` + CI. Write the Tier 1 pure-function tests. Wire in the golden-transcript parametrized test from week 1's snapshots. Add `ruff` and `gitleaks` as pre-commit hooks. Nothing in this week changes behavior — that is deliberate, and it is what makes weeks 3+ safe.

**Week 3 — Authorization and session correctness.** F5 (roles on `tool_access`, `require_role` dependency, delete the dead `X-Admin-Token` path), F14 (move `index.html` out of `/static/`, remove the hardcoded owner email from the client), F15 (401 handling in the fetch wrapper). These are user-visible and need coordination. Ship early in the week so problems surface with the team available.

**Week 4 — The ingestion identity model.** F10's shared `is_outgoing` column first (it is the foundation), then F7 (restore `mark_chat_audited`, add the unique constraint, convert to upsert, delete the Python de-duplication), then F29 (unique index on contacts, de-duplicate existing rows), then F34 (which falls out of F10 for free). This is the riskiest week — which is exactly why it lands behind two weeks of tests. Verify against the golden snapshots after each step, not at the end.

**Week 5 — Timezone and windowing correctness.** F23 (`convo_date` in local time, plus the historical backfill and a `_reattribute_day` replay over the affected range), F28 (window the response-time input), F24 (aware datetimes in the scorer), F35 (transactional nightly reset on `settings.TIMEZONE` with an advisory lock). Each of these moves numbers on reports the team looks at daily. Announce the backfill before running it.

**Week 6 — Performance, now that row growth is under control.** F16 (set-based rewrite of `_compute_review_stats_bulk` plus the partial index), F17 (pagination and the date expression index), F21 (pool or batch the semantic-learner connections, and reduce `MAX_PARALLEL_WORKERS` to fit the real `max_connections`). Measure before and after — with F7 fixed, some of this pressure may already have resolved itself.

**Week 7 — Decide the ML pipeline's future, then act.** Make the explicit call from the Architecture section: delete Tier 2/Tier 3 or commit to repairing them. If deleting, remove the modules, artifacts, and the FAISS/sklearn/torch dependencies — that closes F9, F21's remaining surface, F22, and F32 in one stroke and materially shrinks the image and CPU footprint. If repairing, F8 (schema for the telemetry tables) and F10's rebuild come first, then retrain on 1.7.2 (F9), then run `scripts/eval_prefilter.py` against a real corpus and hold the FALSE-CLEAN ≤ 5% gate honestly. Either way, update README and CLAUDE.md (F31) to match reality.

**Ongoing, starting week 2.** Extract one rule at a time from `tier1_phrases_v2.evaluate` (F19) behind the golden-transcript harness — roughly one rule per working day, verified individually. Prioritize the rules with regression history in `git log`: F14, F16, F17, and the DNC/Wrong-Number precedence.

---

## Coverage Statement

**Reviewed deeply** — read in full and traced: `dashboard/app.py` (all 4,268 lines, every route), `database/db.py`, `database/schema.sql`, all six files in `database/migrations/`, `main.py`, `ai/scorer.py`, `ai/analyzer.py`, `ai/prompts.py`, `ai/response_time.py`, `ai/prefilter/pipeline.py`, `ai/prefilter/embedder.py`, `ai/prefilter/embedding_service.py`, `ai/prefilter/tier3_classifier.py`, `ai/prefilter/semantic_learner.py`, `config/settings.py`, `config/rate_limiter.py`, all four files in `scraper/`, `scripts/post_audit_reflection.py`, `Dockerfile`, `Procfile`, `nixpacks.toml`, `requirements.txt`, `.gitignore`, `.env.example`, `README.md`, `CLAUDE.md`.

**Reviewed selectively** — structural analysis (AST-measured function sizes, regex placement, import graph) plus targeted reads of the sections that findings depend on: `dashboard/static/index.html` (all 87 `innerHTML` sites and all 45 `fetch` sites examined for escaping and error handling; polling, agent-load, chat-render, AI-analysis, detailed-dashboard, and trends renderers read in full — roughly 1,500 of 8,125 lines read directly), `ai/prefilter/index_builder.py`, `ai/prefilter/train.py`, `ai/dream_worker.py`, `ai/session_logger.py`, `ai/learned_rules.py`, `scripts/*`.

**Reviewed shallowly — the significant gap in this report.** The scoring rule logic itself inside `ai/prefilter/tier1_phrases_v2.py` (2,201 lines), `ai/prefilter/label_validator.py` (1,427), `ai/prefilter/summary_builder.py` (1,369), `ai/prefilter/_guards.py` (925), `ai/prefilter/tier4_flag_generator.py` (737), `ai/prefilter/tier2_embedding.py`, `ai/prefilter/pillar_detection.py`, `ai/prefilter/flag_triggers.py`, `ai/prefilter/label_vetoes.py`, `ai/prefilter/shadow_harness.py`, and `ai/transcript_parser.py` was **not** audited — about 7,000 lines, and now the source of **100% of the product's output**. Their structure, entry points, contracts, and shared-state hygiene were verified (F19, F10), but the individual rules were not read and no attempt was made to find false positives or false negatives in them. Doing so credibly requires domain knowledge of the sales playbook and real transcripts to test against — which is precisely the golden-transcript harness recommended in section 7. **Treat the correctness of the scoring rules as unaudited.** Given that `git log` shows these files are the most frequently patched in the repo and the recent history is dominated by fix-a-false-positive commits, a rules-focused review is the single highest-yield follow-up once the test harness exists.

**Not reviewed:** `frontend/` (a scaffolded Next.js app, unreferenced by the running system — likely abandoned, worth confirming and deleting), `docs/*.html`, `dashboard/static/labels-guide.html`, `dashboard/static/login.html` beyond a secrets scan, and `ai/prefilter/artifacts/*.csv`.

**Not verifiable without production access:** F26 (which IP the rate limiter actually sees), F8's exact production state (which migrations ran against the Railway database), and all row-count-dependent performance claims (F16, F17), which were argued from query shape and growth characteristics rather than measurement.
