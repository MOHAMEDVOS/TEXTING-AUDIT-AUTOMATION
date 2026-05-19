"""
Scorer — Phase 2/3 bridge.

Runs the local model analyzer on every conversation for an agent,
aggregates the scores, and writes the result to the audit_scores table.
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timezone, timedelta

import psycopg2

from config.settings import DATABASE_URL, get_now
from ai.analyzer import analyze_conversation
from ai.prefilter.label_validator import _label_key as _lk

logger = logging.getLogger(__name__)

# Label whitelist for wrong-label injection.
# If AI suggests a label outside this set, we ignore that wrong-label claim.
_ALLOWED_LABELS = {
    "potential",
    "warm",
    "hot",
    "lead",
    "lead pushed",
    "investor",
    "bluffer",
    "abv mv",
    "fup",
    "fui",
    "wf",
    "mf",
    "nf",
    "wl",
    "ap",
    "hl",
    "fui, wl drip",
    "fui, wl drip, no response",
    "fui, wl drip, not interested",
    "fui, wl drip, dnc",
    "fui, wl drip, wrong number",
    "fui, wl drip, sold",
    "fui, wl drip, under contract",
    "do not call",
    "do not call, remove",
    "do not call, remove me",
    "do not call, unsubscribe",
    "not interested",
    "wrong number",
    "sold",
    "under contract",
    "voicemail",
    "no answer",
    "stopped responding",
    "undefined",
}

# ── Invalid flag filter ───────────────────────────────────────────────────────

def _load_invalid_flag_patterns(dsn: str) -> set[str]:
    """
    Load all red_flag strings from flag_feedback as a set of lowercase patterns.
    Used to suppress known-invalid flags from new audit results.
    Returns empty set on any error so scoring always continues.
    """
    try:
        with psycopg2.connect(dsn) as con:
            with con.cursor() as cur:
                cur.execute("SELECT red_flag FROM flag_feedback")
                patterns = {row[0].lower().strip() for row in cur.fetchall() if row[0]}
        logger.debug(f"[Scorer] Loaded {len(patterns)} invalid flag patterns from flag_feedback")
        return patterns
    except Exception as e:
        logger.warning(f"[Scorer] Could not load invalid flag patterns: {e}")
        return set()


def _load_invalid_flags_by_conversation(dsn: str) -> dict[int, set[str]]:
    """
    Load reviewer-rejected flags keyed by their exact source conversation.

    Conversation-scoped counterpart to _load_invalid_flag_patterns: when a
    reviewer marks a flag "Not Valid" on a specific conversation, that flag
    must never re-appear on a re-audit of THAT conversation — even if the same
    flag text is legitimate elsewhere. Precise, no global over-suppression.

    Returns {conversation_id: {lowercased flag text, ...}}.
    Returns empty dict on any error so scoring always continues.
    """
    try:
        with psycopg2.connect(dsn) as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT conversation_id, red_flag FROM flag_feedback "
                    "WHERE status = 'invalid' AND conversation_id IS NOT NULL"
                )
                by_conv: dict[int, set[str]] = {}
                for cid, flag in cur.fetchall():
                    if cid is None or not flag:
                        continue
                    by_conv.setdefault(int(cid), set()).add(flag.lower().strip())
        logger.debug(
            f"[Scorer] Loaded conversation-scoped rejected flags for "
            f"{len(by_conv)} conversation(s)"
        )
        return by_conv
    except Exception as e:
        logger.warning(f"[Scorer] Could not load conversation-scoped flags: {e}")
        return {}


def _filter_flags(flags: list[str], patterns: set[str]) -> list[str]:
    """
    Remove any flag whose text fuzzy-matches a known-invalid pattern.

    Match logic (both sides lowercased):
      - flag is a substring of a pattern, OR
      - pattern is a substring of the flag, OR
      - pattern contains '...' (truncation marker): all non-empty segments split
        by '...' must each be a substring of the flag (wildcard/truncation match)
    Either direction catches truncated DB entries and slight wording variations.
    """
    if not patterns:
        return flags

    def _matches(f: str, p: str) -> bool:
        # Guard: very short patterns (< 15 chars) only match exactly to avoid
        # accidentally suppressing legitimate flags (e.g. a bare word like "continued").
        if len(p) < 15:
            return f == p
        if f in p or p in f:
            return True
        # Handle truncated patterns stored with '...' as a wildcard
        if "..." in p:
            segments = [s for s in p.split("...") if s]
            return all(seg in f for seg in segments)
        return False

    _NULL_FLAGS = {"none", "n/a", "na", "no flags", "no red flags", "-", ""}

    clean = []
    for flag in flags:
        f = flag.lower().strip()
        if f in _NULL_FLAGS:
            logger.debug(f"[Scorer] Stripped null-sentinel flag: {flag!r}")
            continue
        suppressed = any(_matches(f, p) for p in patterns)
        if suppressed:
            logger.debug(f"[Scorer] Suppressed known-invalid flag: {flag!r}")
        else:
            clean.append(flag)
    return clean


# UTC-4 during EDT (summer), UTC-5 during EST (winter) — detected at runtime
EASTERN = timezone(timedelta(hours=-4 if time.daylight and time.localtime().tm_isdst else -5))
BUSINESS_START = 9   # 9 AM
BUSINESS_END   = 17  # 5 PM
OVERDUE_MINUTES = 30


def _check_overdue_unreads(unread_conversations: list[dict]) -> list[str]:
    """
    Return a red-flag string for every unread conversation whose last message
    timestamp is older than OVERDUE_MINUTES during business hours (Eastern).
    """
    flags: list[str] = []
    now_et = datetime.now(EASTERN)

    # Only run during business hours Mon–Fri
    if not (BUSINESS_START <= now_et.hour < BUSINESS_END and now_et.weekday() < 5):
        return flags

    for uc in unread_conversations:
        date_str = (uc.get("date") or "").strip()
        time_str = (uc.get("time") or "").strip()
        contact  = uc.get("contact_name") or "Unknown contact"

        if not date_str or not time_str:
            continue

        try:
            # SmarterContact rows show  "MM/DD/YYYY"  and  "HH:MM AM/PM"
            msg_dt = datetime.strptime(f"{date_str} {time_str}", "%m/%d/%Y %I:%M %p")
            msg_et = msg_dt.replace(tzinfo=EASTERN)
            elapsed = (now_et - msg_et).total_seconds() / 60

            if elapsed > OVERDUE_MINUTES:
                flags.append(
                    f"Unread message from {contact} has been waiting "
                    f"{int(elapsed)} minutes with no response (30-min rule)"
                )
        except Exception:
            continue

    return flags


def _is_allowed_label_name(label: str) -> bool:
    return (label or "").strip().lower() in _ALLOWED_LABELS


async def score_agent_conversations(
    agent_id: int,
    agent_name: str,
    conversations: list[dict],
    unread_count: int = 0,
    unread_conversations: list[dict] | None = None,
    pool=None,
    pinned_key=None,
) -> dict:
    """
    Analyze all conversations for one agent and persist aggregate audit scores.

    Args:
        agent_id:      FK into the agents table.
        agent_name:    Display name (e.g. "Noah Mallen") passed to the AI prompt.
        conversations: List of conversation dicts, each must contain "parsed_messages"
                       (output of parse_transcript) and optionally "contact_name".

    Returns:
        Aggregate score dict (or {} if nothing could be scored).
    """
    if not conversations:
        logger.info(f"[Scorer] {agent_name} — no conversations, skipping")
        return {}

    # Loaded once per run — mid-run DB changes are fine; they take effect on the next full run.
    invalid_patterns = _load_invalid_flag_patterns(DATABASE_URL)
    # Conversation-scoped rejections: a flag a reviewer killed on conversation X
    # must not re-appear when X is re-audited (one query, applied per conversation).
    conv_rejected = _load_invalid_flags_by_conversation(DATABASE_URL)

    # ── Per-account audit config (funnel tier + guidelines) ────────────────
    # Fetched once per scoring run; same config applies to every conversation
    # for this account in this run.
    funnel_tier: str | None = None
    guidelines: str | None = None
    try:
        import asyncpg
        _conn = await asyncpg.connect(DATABASE_URL)
        try:
            _row = await _conn.fetchrow(
                "SELECT email, funnel_tier, guidelines FROM accounts WHERE id = $1",
                agent_id,
            )
            if _row:
                funnel_tier = _row["funnel_tier"]
                guidelines = _row["guidelines"]
                if funnel_tier or guidelines:
                    logger.info(
                        f"[Scorer] {agent_name} — account config loaded: "
                        f"tier={funnel_tier or 'none'}, "
                        f"guidelines={'yes' if guidelines else 'no'}"
                    )
                else:
                    logger.info(
                        f"[Scorer] {agent_name} — no per-account audit config "
                        f"(falling back to global prompt)"
                    )
        finally:
            await _conn.close()
    except Exception as e:
        logger.warning(
            f"[Scorer] {agent_name} — failed to load audit config: {e} "
            f"(falling back to global prompt)"
        )

    logger.info(f"[Scorer] ── {agent_name} — scoring {len(conversations)} conversation(s) in parallel")
    per_convo: list[dict] = []
    total = len(conversations)

    # Limit concurrent analysis to avoid overwhelming local models or threads
    semaphore = asyncio.Semaphore(15)

    async def _process_convo(idx: int, convo: dict) -> dict | None:
        parsed = convo.get("parsed_messages") or []
        contact = convo.get("contact_name") or "Contact"
        labels = convo.get("assigned_labels") or []

        if not parsed:
            logger.info(f"[Scorer] {agent_name} [{idx}/{total}] {contact} — no messages, skipping")
            return None

        async with semaphore:
            logger.info(
                f"[Scorer] {agent_name} [{idx}/{total}] {contact} — "
                f"scoring ({len(parsed)} msgs, labels={labels or 'none'})"
            )

            result = await asyncio.to_thread(
                analyze_conversation,
                parsed,
                agent_name,
                contact,
                assigned_labels=labels,
                funnel_tier=funnel_tier,
                guidelines=guidelines,
                pinned_key=pinned_key,
                conversation_id=convo.get("conversation_id"),
                db_pool=pool,
            )

            if convo.get("conversation_id") and not result.get("conversation_id"):
                result["conversation_id"] = convo["conversation_id"]

            result["red_flags"] = _filter_flags(result.get("red_flags") or [], invalid_patterns)
            # Conversation-scoped: drop flags a reviewer rejected on THIS exact conversation.
            _cid = convo.get("conversation_id")
            if _cid is not None:
                _cr = conv_rejected.get(int(_cid)) if str(_cid).isdigit() else None
                if _cr:
                    result["red_flags"] = _filter_flags(result["red_flags"], _cr)

            score = result.get("compliance_score")
            flags = len(result.get("red_flags") or [])
            funnel = result.get("funnel_stage_reached") or "?"
            logger.info(
                f"[Scorer] {agent_name} [{idx}/{total}] {contact} — "
                f"adherence={score} funnel={funnel} flags={flags}"
            )
            return result

    coros = [_process_convo(idx, convo) for idx, convo in enumerate(conversations, 1)]
    results = await asyncio.gather(*coros)

    for r in results:
        if r is not None:
            per_convo.append(r)

    if not per_convo:
        logger.warning(f"[Scorer] {agent_name}: all conversations had empty parsed_messages")
        return {}

    # ── Aggregate numeric scores ─────────────────────────────────────────────
    def _avg(key: str) -> float | None:
        vals = [r[key] for r in per_convo if r.get(key) is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    compliance   = _avg("compliance_score")
    sentiment    = _avg("sentiment_score")
    professional = _avg("professionalism_score")
    script       = _avg("script_adherence_score")

    scored_vals = [x for x in [compliance, sentiment, professional, script] if x is not None]
    overall = round(sum(scored_vals) / len(scored_vals), 1) if scored_vals else None

    # ── Inject wrong-label as a red flag on each conversation ───────────────
    for r in per_convo:
        if r.get("label_correct") is False:
            wrong  = (r.get("label_assigned") or "").strip()
            should = (r.get("label_should_be") or "").strip()
            # AI sometimes returns label_correct=false but identical values — ignore it
            # Also treat semantically equivalent labels (Decision Maker, Verified, etc.) as correct
            if not wrong or not should or _lk(wrong) == _lk(should):
                r["label_correct"] = True
                continue
            # Routing hints are not real labels — never show as a flag
            if "groq" in should.lower() or "needs groq" in should.lower():
                r["label_correct"] = True
                continue
            # AI can hallucinate non-existent labels; ignore those wrong-label claims.
            if not _is_allowed_label_name(should):
                r["label_correct"] = True
                continue
            flag  = f"Wrong label: assigned '{wrong}' but should be '{should}'"
            flags = list(r.get("red_flags") or [])
            if flag not in flags:
                flags.insert(0, flag)
            r["red_flags"] = flags

    # Strip any injected wrong-label flags that are known-invalid
    for r in per_convo:
        r["red_flags"] = _filter_flags(r.get("red_flags") or [], invalid_patterns)
        _cid = r.get("conversation_id")
        if _cid is not None and str(_cid).isdigit():
            _cr = conv_rejected.get(int(_cid))
            if _cr:
                r["red_flags"] = _filter_flags(r["red_flags"], _cr)

    # ── Red flags: count conversations with ≥1 flag (not total mistakes) ────
    # Each flagged conversation counts as 1, regardless of how many issues it has.
    # Per-conversation details still preserve all individual flags for drill-down.
    flagged_convos: list[str] = []   # one entry per flagged conversation (contact name)
    for r in per_convo:
        flags = r.get("red_flags") or []
        if flags:
            contact_label = r.get("contact_name") or "Contact"
            flagged_convos.append(contact_label)

    # ── 30-minute unread rule (business hours, Eastern) ──────────────────────
    # Overdue unreads are rule-based (not AI-generated) so they bypass _filter_flags.
    # Each counts as one flagged conversation.
    overdue = _check_overdue_unreads(unread_conversations or [])
    for flag in overdue:
        flagged_convos.append(flag)   # each overdue unread = one flagged conversation
        logger.warning(f"[Scorer] {agent_name} — OVERDUE UNREAD: {flag}")

    # all_flags kept for backward compatibility with the DB column (stores the list)
    all_flags = flagged_convos

    # ── Per-conversation breakdown for the details column ────────────────────
    # Label accuracy stats
    label_audits = [r for r in per_convo if "label_correct" in r]
    wrong_labels = [r for r in label_audits if r.get("label_correct") is False]

    details = {
        "agent_name": agent_name,
        "conversations_analyzed": len(per_convo),
        "unread_messages_left": unread_count,
        "label_accuracy": (
            round((len(label_audits) - len(wrong_labels)) / len(label_audits) * 100, 1)
            if label_audits else None
        ),
        "wrong_label_count": len(wrong_labels),
        "per_conversation": [
            {
                "contact": r.get("contact_name", "Contact"),
                "compliance": r.get("compliance_score"),
                "sentiment": r.get("sentiment_score"),
                "professionalism": r.get("professionalism_score"),
                "script_adherence": r.get("script_adherence_score"),
                "funnel_stage_reached": r.get("funnel_stage_reached"),
                "pillars_gathered": r.get("pillars_gathered", []),
                "rebuttals_used": r.get("rebuttals_used", []),
                "label_assigned": r.get("label_assigned"),
                "label_correct": r.get("label_correct"),
                "label_should_be": r.get("label_should_be"),
                "label_reason": r.get("label_reason"),
                "red_flags": r.get("red_flags", []),
                "summary": r.get("summary", ""),
                "model_used": r.get("model_used"),
            }
            for r in per_convo
        ],
    }

    # Keep the raw, current-run conversations (with conversation_id) for score writes.
    scored_this_run = list(per_convo)

    # ── Write to audit_scores ────────────────────────────────────────────────
    # EST date — must match conversations.audit_date and the dashboard, which
    # all use get_now(). Naive date.today() is UTC on Railway and rolls a day
    # ahead after ~8 PM EST, breaking the dashboard convo-count JOIN.
    audit_date = get_now().date()
    if pool:
        async with pool.acquire() as conn:
            # Fetch any existing row for this agent+date so we can merge
            existing_row = await conn.fetchrow(
                "SELECT id, details FROM audit_scores "
                "WHERE agent_id = $1 AND audit_date = $2",
                agent_id, audit_date,
            )

            if existing_row:
                # Merge: combine per_conversation lists, recompute weighted averages
                try:
                    prev_details = existing_row["details"] or {}
                    if isinstance(prev_details, str):
                        prev_details = json.loads(prev_details)
                except Exception:
                    prev_details = {}

                prev_pc = prev_details.get("per_conversation", [])
                # New run's data wins for duplicates — filter old entries for contacts in new run
                new_contacts = {(pc.get("contact") or "").lower().strip() for pc in details["per_conversation"]}
                prev_pc_kept = [pc for pc in prev_pc if (pc.get("contact") or "").lower().strip() not in new_contacts]
                merged_pc = prev_pc_kept + details["per_conversation"]
                merged_count = len(merged_pc)

                # Weighted average of scores across all conversations
                def _wavg(key: str) -> float | None:
                    vals = [pc[key] for pc in merged_pc if pc.get(key) is not None]
                    return round(sum(vals) / len(vals), 2) if vals else None

                merged_compliance   = _wavg("compliance")
                merged_sentiment    = _wavg("sentiment")
                merged_professional = _wavg("professionalism")
                merged_script       = _wavg("script_adherence")
                merged_overall = round(
                    sum(v for v in [merged_compliance, merged_sentiment,
                                    merged_professional, merged_script]
                        if v is not None) /
                    max(1, sum(1 for v in [merged_compliance, merged_sentiment,
                                           merged_professional, merged_script]
                               if v is not None)),
                    2,
                )

                merged_flags = [pc.get("contact") or "Contact"
                                for pc in merged_pc if pc.get("red_flags")]

                merged_details = dict(prev_details)
                merged_details.update({
                    "conversations_analyzed": merged_count,
                    "per_conversation": merged_pc,
                    "wrong_label_count": sum(
                        1 for pc in merged_pc if pc.get("label_correct") is False
                    ),
                })
                label_audits_merged = [pc for pc in merged_pc if "label_correct" in pc]
                wrong_merged = [pc for pc in label_audits_merged if pc.get("label_correct") is False]
                merged_details["label_accuracy"] = (
                    round((len(label_audits_merged) - len(wrong_merged)) / len(label_audits_merged) * 100, 1)
                    if label_audits_merged else None
                )

                await conn.execute(
                    """UPDATE audit_scores
                       SET overall_score          = $1,
                           compliance_score       = $2,
                           sentiment_score        = $3,
                           professionalism_score  = $4,
                           script_adherence_score = $5,
                           red_flags              = $6::jsonb,
                           details                = $7::jsonb
                       WHERE id = $8""",
                    merged_overall,
                    merged_compliance,
                    merged_sentiment,
                    merged_professional,
                    merged_script,
                    json.dumps(merged_flags),
                    json.dumps(merged_details),
                    existing_row["id"],
                )
                # Update local vars so trend snapshot uses merged totals
                overall      = merged_overall
                compliance   = merged_compliance
                sentiment    = merged_sentiment
                professional = merged_professional
                script       = merged_script
                all_flags    = merged_flags
                details      = merged_details
                per_convo    = merged_pc
            else:
                await conn.execute(
                    """
                    INSERT INTO audit_scores
                        (agent_id, audit_date, overall_score, compliance_score, sentiment_score,
                         professionalism_score, response_time_score, script_adherence_score,
                         red_flags, details)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10::jsonb)
                    """,
                    agent_id,
                    audit_date,
                    overall,
                    compliance,
                    sentiment,
                    professional,
                    None,
                    script,
                    json.dumps(all_flags),
                    json.dumps(details),
                )

            # ── Write per-conversation scores for CURRENT run only ────────
            # per_convo may be replaced by merged historical details, which does not
            # always carry conversation_id. Use scored_this_run to avoid ghost rows.
            for r in scored_this_run:
                conv_id = r.get("conversation_id")
                if conv_id:
                    model_used = r.get("model_used") or ""
                    if model_used.startswith("prefilter_t1"):
                        source = "prefilter_t1"
                    elif model_used.startswith("prefilter_t2"):
                        source = "prefilter_t2"
                    elif model_used.startswith("prefilter_t3"):
                        source = "prefilter_t3"
                    elif model_used.startswith("prefilter_t4"):
                        source = "prefilter_t4"
                    else:
                        source = "groq"
                    try:
                        await conn.execute(
                            """INSERT INTO conversation_scores
                                   (conversation_id, compliance_score, sentiment_score,
                                    professionalism_score, script_adherence_score,
                                    funnel_stage, pillars_gathered, rebuttals_used,
                                    label_assigned, label_correct, label_should_be, label_reason,
                                    red_flags, actions_triggered, summary, model_used, source)
                               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,$14,$15,$16,$17)""",
                            conv_id,
                            r.get("compliance_score"),
                            r.get("sentiment_score"),
                            r.get("professionalism_score"),
                            r.get("script_adherence_score"),
                            r.get("funnel_stage_reached"),
                            r.get("pillars_gathered") or [],
                            r.get("rebuttals_used") or [],
                            r.get("label_assigned"),
                            r.get("label_correct"),
                            r.get("label_should_be"),
                            r.get("label_reason"),
                            json.dumps(r.get("red_flags") or []),
                            r.get("actions_triggered") or [],
                            r.get("summary"),
                            r.get("model_used"),
                            source,
                        )
                    except Exception as _e:
                        logger.error(f"[Scorer] Failed to write conversation_scores for conv_id={conv_id}: {_e}")

            # ── Write trend snapshot ──────────────────────────────────────
            # Resolve the assigned texter name from account_assignments
            agent_email_row = await conn.fetchrow(
                "SELECT email FROM accounts WHERE id = $1", agent_id
            )
            agent_email = agent_email_row["email"] if agent_email_row else ""
            assign_row = await conn.fetchrow(
                """SELECT agent_name AS texter_name, account_email
                   FROM account_assignments
                   WHERE LOWER(account_email) = LOWER($1) AND assigned_date = $2""",
                agent_email, audit_date,
            )
            snapshot_texter = assign_row["texter_name"] if assign_row else agent_name
            snapshot_email  = assign_row["account_email"] if assign_row else agent_email
            total_flag_count = sum(len(r.get("red_flags") or []) for r in per_convo)
            from datetime import datetime as _dt
            await conn.execute(
                """INSERT INTO trend_snapshots
                   (agent_name, audit_date, audit_timestamp, account_email,
                    total_issues, overall_score, compliance_score, sentiment_score,
                    professionalism_score, script_adherence_score, conversations_analyzed)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                   ON CONFLICT (agent_name, audit_date, account_email) DO UPDATE
                     SET overall_score           = EXCLUDED.overall_score,
                         compliance_score        = EXCLUDED.compliance_score,
                         sentiment_score         = EXCLUDED.sentiment_score,
                         professionalism_score   = EXCLUDED.professionalism_score,
                         script_adherence_score  = EXCLUDED.script_adherence_score,
                         total_issues            = EXCLUDED.total_issues,
                         conversations_analyzed  = EXCLUDED.conversations_analyzed,
                         audit_timestamp         = EXCLUDED.audit_timestamp
""",
                snapshot_texter,
                audit_date,
                _dt.now(),  # asyncpg needs a datetime object, not an isoformat string
                snapshot_email,
                total_flag_count,
                overall,
                compliance,
                sentiment,
                professional,
                script,
                len(per_convo),
            )
            logger.info(f"[Scorer] Trend snapshot saved for '{snapshot_texter}' on {audit_date}")

    wrong_label_count = details["wrong_label_count"]
    label_accuracy    = details["label_accuracy"]

    logger.info(
        f"[Scorer] {agent_name} — overall={overall} | "
        f"adherence={compliance} | attitude={sentiment} | "
        f"professionalism={professional} | script={script} | "
        f"flagged_chats={len(all_flags)} | convos={len(per_convo)} | "
        f"unread_left={unread_count} | "
        f"label_accuracy={label_accuracy}% ({wrong_label_count} wrong)"
    )

    # ── Session capture + dream worker (self-learning) ──────────────────────
    try:
        from ai.session_logger import log_session
        total_flags = sum(len(r.get("red_flags") or []) for r in per_convo)
        model = next((r.get("model_used") for r in per_convo if r.get("model_used")), None)
        await asyncio.to_thread(
            log_session,
            agent_id=agent_id,
            agent_name=agent_name,
            conversations_scored=len(per_convo),
            flags_generated=total_flags,
            model_used=model,
        )
    except Exception as _e:
        logger.warning(f"[Scorer] session_logger failed (non-fatal): {_e}")

    # ── Post-audit reflection (detached child process) ──────────────────────
    # Dream-worker rule learning + semantic kNN rebuild can take minutes.
    # Running them via run_in_executor does NOT detach — main.py's asyncio.run()
    # calls loop.shutdown_default_executor() on exit, which blocks until those
    # threads finish, keeping the dashboard result stuck on "pending".
    # Spawning a fully detached process lets this audit run exit immediately.
    try:
        import subprocess
        import sys
        from pathlib import Path

        proj_root = Path(__file__).resolve().parent.parent
        reflection_script = proj_root / "scripts" / "post_audit_reflection.py"
        log_dir = proj_root / "logs"
        log_dir.mkdir(exist_ok=True)
        refl_log = open(log_dir / "reflection.log", "a", encoding="utf-8")

        popen_kwargs = dict(
            cwd=str(proj_root),
            stdin=subprocess.DEVNULL,
            stdout=refl_log,
            stderr=refl_log,
        )
        if sys.platform == "win32":
            # CREATE_NO_WINDOW → background console process with no visible
            # window. CREATE_NEW_PROCESS_GROUP → survives the parent exiting.
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            popen_kwargs["start_new_session"] = True

        subprocess.Popen([sys.executable, str(reflection_script)], **popen_kwargs)
        logger.info(
            "[Scorer] Post-audit reflection spawned (detached) — result returned now."
        )
    except Exception as _e:
        logger.warning(f"[Scorer] could not spawn reflection process (non-fatal): {_e}")

    return {
        "overall_score": overall,
        "compliance_score": compliance,
        "sentiment_score": sentiment,
        "professionalism_score": professional,
        "script_adherence_score": script,
        "red_flags": all_flags,
        "conversations_analyzed": len(per_convo),
        "unread_count": unread_count,
        "label_accuracy": label_accuracy,
        "wrong_label_count": wrong_label_count,
    }
