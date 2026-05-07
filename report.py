"""
Full Audit Report — reads audit_scores from DB and prints a ranked summary.
Usage: python report.py
"""
import asyncio
import json
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

import aiosqlite
from config.settings import DB_PATH


async def run():
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row

        # One row per agent — latest score only
        cur = await db.execute("""
            SELECT
                a.name,
                s.overall_score,
                s.compliance_score,
                s.sentiment_score,
                s.professionalism_score,
                s.script_adherence_score,
                s.red_flags,
                s.details,
                s.audit_date
            FROM audit_scores s
            JOIN agents a ON s.agent_id = a.id
            WHERE s.id IN (
                SELECT MAX(id) FROM audit_scores GROUP BY agent_id
            )
            ORDER BY s.overall_score DESC NULLS LAST
        """)
        rows = await cur.fetchall()

    if not rows:
        print("No audit scores found. Run 'python main.py' first.")
        return

    W = 72
    print()
    print("=" * W)
    print("  FULL AUDIT REPORT — ALL AGENTS")
    print("=" * W)

    # ── Summary table ────────────────────────────────────────────────────────
    print(f"\n  {'AGENT':<22} {'OVERALL':>7} {'COMPLY':>7} {'SENT':>6} "
          f"{'PROF':>6} {'SCRIPT':>7} {'LABEL%':>7} {'UNREAD':>7}")
    print(f"  {'-'*22} {'-'*7} {'-'*7} {'-'*6} {'-'*6} {'-'*7} {'-'*7} {'-'*7}")

    for r in rows:
        det = json.loads(r["details"] or "{}")
        label_acc = det.get("label_accuracy")
        unread    = det.get("unread_messages_left", 0)
        label_str = f"{label_acc:.0f}%" if label_acc is not None else "n/a"
        overall   = f"{r['overall_score']:.1f}" if r["overall_score"] is not None else "n/a"
        comply    = f"{r['compliance_score']:.1f}" if r["compliance_score"] is not None else "n/a"
        sent      = f"{r['sentiment_score']:.1f}" if r["sentiment_score"] is not None else "n/a"
        prof      = f"{r['professionalism_score']:.1f}" if r["professionalism_score"] is not None else "n/a"
        script    = f"{r['script_adherence_score']:.1f}" if r["script_adherence_score"] is not None else "n/a"

        print(f"  {r['name']:<22} {overall:>7} {comply:>7} {sent:>6} "
              f"{prof:>6} {script:>7} {label_str:>7} {unread:>7}")

    # ── Per-agent detail ─────────────────────────────────────────────────────
    print()
    print("=" * W)
    print("  PER-AGENT DETAIL")
    print("=" * W)

    for r in rows:
        det        = json.loads(r["details"] or "{}")
        per_convo  = det.get("per_conversation", [])
        all_flags  = json.loads(r["red_flags"] or "[]")
        unread     = det.get("unread_messages_left", 0)
        label_acc  = det.get("label_accuracy")
        wrong_lbl  = det.get("wrong_label_count", 0)
        n_convos   = det.get("conversations_analyzed", 0)

        print(f"\n  ── {r['name']} ──────────────────────────────────────────")
        print(f"  Conversations audited : {n_convos}")
        print(f"  Unread left           : {unread}")
        overall = r["overall_score"]
        print(f"  Overall score         : {f'{overall:.1f}' if overall is not None else 'n/a'} / 100")
        print(f"  Label accuracy        : {f'{label_acc:.1f}%' if label_acc is not None else 'n/a'}"
              f"  ({wrong_lbl} wrong)")

        if all_flags:
            print("  Red flags:")
            for f in all_flags:
                print(f"    - {f}")

        if per_convo:
            print(f"  {'Contact':<28} {'Avg':>5}  {'Funnel':<8}  Label audit")
            for c in per_convo:
                vals = [v for v in [
                    c.get("compliance"), c.get("sentiment"),
                    c.get("professionalism"), c.get("script_adherence"),
                ] if v is not None]
                avg    = f"{sum(vals)/len(vals):.1f}" if vals else "n/a"
                funnel = (c.get("funnel_stage_reached") or "n/a")[:8]
                lbl_ok = "OK" if c.get("label_correct") else \
                         f"WRONG -> {c.get('label_should_be', '?')}"
                name   = (c.get("contact") or "?")[:28]
                print(f"  {name:<28} {avg:>5}  {funnel:<8}  {lbl_ok}")

    # ── Team-wide summary ────────────────────────────────────────────────────
    print()
    print("=" * W)
    print("  TEAM SUMMARY")
    print("=" * W)

    scored = [r for r in rows if r["overall_score"] is not None]
    if scored:
        avg_overall = sum(r["overall_score"] for r in scored) / len(scored)
        avg_comply  = sum(r["compliance_score"] for r in scored if r["compliance_score"]) / len(scored)
        total_unread = sum(json.loads(r["details"] or "{}").get("unread_messages_left", 0) for r in rows)

        print(f"  Agents audited        : {len(rows)}")
        print(f"  Avg overall score     : {avg_overall:.1f} / 100")
        print(f"  Avg compliance score  : {avg_comply:.1f}")
        print(f"  Total unread (team)   : {total_unread}")

        below_80 = [r["name"] for r in scored if r["overall_score"] < 80]
        if below_80:
            print(f"\n  NEEDS ATTENTION (score < 80):")
            for name in below_80:
                print(f"    - {name}")

        high_unread = [
            (r["name"], json.loads(r["details"] or "{}").get("unread_messages_left", 0))
            for r in rows
            if json.loads(r["details"] or "{}").get("unread_messages_left", 0) >= 5
        ]
        if high_unread:
            print(f"\n  HIGH UNREAD (5+ messages sitting):")
            for name, cnt in sorted(high_unread, key=lambda x: -x[1]):
                print(f"    - {name}: {cnt} unread")
    print()


asyncio.run(run())
