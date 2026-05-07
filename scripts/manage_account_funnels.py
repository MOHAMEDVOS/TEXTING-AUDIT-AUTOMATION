"""
CLI for managing per-account audit configuration.

Commands:
  list                        Show all accounts with tier + guideline preview
  show <email>                Full tier + guidelines for one account
  set-tier <email> <NF|MF|WF|none>
  set-guidelines <email> --file <path>
  seed <csv_path>             Bulk-upsert tier + guidelines from CSV
  audit                       List accounts with no tier OR no guidelines

CSV format (header: Emails,Funnels,Guidlines):
  Emails,Funnels,Guidlines
  Noah@goccs.net,NF,"(Ask about) ..."
"""
import argparse
import asyncio
import csv
import sys
from pathlib import Path

# Make project root importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.db import Database


def _truncate(text: str | None, n: int = 60) -> str:
    if not text:
        return ""
    t = text.replace("\n", " / ").replace("\r", "")
    return (t[:n] + "…") if len(t) > n else t


async def cmd_list(db: Database) -> int:
    rows = await db.list_accounts_with_audit_config()
    if not rows:
        print("(no accounts)")
        return 0
    print(f"{'EMAIL':<45} {'TIER':<5} GUIDELINES")
    print("-" * 100)
    for r in rows:
        tier = r["funnel_tier"] or "-"
        print(f"{r['email']:<45} {tier:<5} {_truncate(r['guidelines'], 60)}")
    print(f"\n{len(rows)} accounts.")
    return 0


async def cmd_show(db: Database, email: str) -> int:
    cfg = await db.get_account_audit_config(email)
    if not cfg:
        print(f"Account not found: {email}")
        return 1
    print(f"Email:       {email}")
    print(f"Funnel tier: {cfg.get('funnel_tier') or '(none)'}")
    print(f"Guidelines:")
    g = cfg.get("guidelines")
    if not g:
        print("  (none)")
    else:
        for line in g.splitlines():
            print(f"  {line}")
    return 0


async def cmd_set_tier(db: Database, email: str, tier: str) -> int:
    tier = tier.upper()
    if tier == "NONE":
        tier = None
    elif tier not in ("NF", "MF", "WF"):
        print(f"Invalid tier: {tier}. Must be NF, MF, WF, or none.")
        return 2
    try:
        ok = await db.set_account_funnel_tier(email, tier)
    except ValueError as e:
        print(f"Error: {e}")
        return 2
    if not ok:
        print(f"Account not found: {email}")
        return 1
    print(f"OK — {email} tier set to {tier or 'NULL'}")
    return 0


async def cmd_set_guidelines(db: Database, email: str, file_path: str) -> int:
    path = Path(file_path)
    if not path.exists():
        print(f"File not found: {file_path}")
        return 2
    text = path.read_text(encoding="utf-8").strip()
    ok = await db.set_account_guidelines(email, text or None)
    if not ok:
        print(f"Account not found: {email}")
        return 1
    print(f"OK — {email} guidelines set ({len(text)} chars)")
    return 0


async def cmd_seed(db: Database, csv_path: str) -> int:
    path = Path(csv_path)
    if not path.exists():
        print(f"CSV not found: {csv_path}")
        return 2

    # Tolerate either "Guidlines" (manager's spelling in source file) or "Guidelines".
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = [fn.strip() for fn in (reader.fieldnames or [])]
        email_col = next((fn for fn in fieldnames if fn.lower() in ("emails", "email")), None)
        tier_col = next((fn for fn in fieldnames if fn.lower() in ("funnels", "funnel", "tier")), None)
        guide_col = next(
            (fn for fn in fieldnames if fn.lower() in ("guidlines", "guidelines", "guideline")),
            None,
        )
        if not email_col:
            print(f"CSV missing Email column (found: {fieldnames})")
            return 2

        rows = list(reader)

    updated, skipped_no_account = 0, []
    for row in rows:
        email = (row.get(email_col) or "").strip()
        if not email:
            continue
        tier_raw = (row.get(tier_col) or "").strip().upper() if tier_col else ""
        tier = tier_raw if tier_raw in ("NF", "MF", "WF") else None
        guidelines = (row.get(guide_col) or "").strip() if guide_col else ""
        guidelines = guidelines or None

        try:
            tier_ok = await db.set_account_funnel_tier(email, tier)
        except ValueError as e:
            print(f"  SKIP {email}: {e}")
            continue
        guide_ok = await db.set_account_guidelines(email, guidelines)
        if tier_ok and guide_ok:
            updated += 1
        else:
            skipped_no_account.append(email)

    print(f"Seeded {updated} account(s) from {csv_path}")
    if skipped_no_account:
        print(f"\nAccounts in CSV but NOT in DB ({len(skipped_no_account)}):")
        for e in skipped_no_account:
            print(f"  - {e}")
        print(
            "\nThese accounts haven't been added to the DB yet (via extraction). "
            "Their config will be applied once they appear."
        )
    return 0


async def cmd_audit(db: Database) -> int:
    rows = await db.list_accounts_with_audit_config()
    missing = [r for r in rows if not r["funnel_tier"] or not r["guidelines"]]
    if not missing:
        print("All accounts have tier + guidelines configured.")
        return 0
    print(f"{len(missing)} account(s) missing tier or guidelines:\n")
    print(f"{'EMAIL':<45} {'TIER':<5} {'GUIDELINES'}")
    print("-" * 60)
    for r in missing:
        tier = r["funnel_tier"] or "MISSING"
        g_status = "set" if r["guidelines"] else "MISSING"
        print(f"{r['email']:<45} {tier:<5} {g_status}")
    return 0


async def main_async(args: argparse.Namespace) -> int:
    db = Database()
    await db.initialize()
    try:
        if args.cmd == "list":
            return await cmd_list(db)
        if args.cmd == "show":
            return await cmd_show(db, args.email)
        if args.cmd == "set-tier":
            return await cmd_set_tier(db, args.email, args.tier)
        if args.cmd == "set-guidelines":
            return await cmd_set_guidelines(db, args.email, args.file)
        if args.cmd == "seed":
            return await cmd_seed(db, args.csv_path)
        if args.cmd == "audit":
            return await cmd_audit(db)
        print(f"Unknown command: {args.cmd}")
        return 2
    finally:
        await db.close()


def main() -> int:
    p = argparse.ArgumentParser(
        prog="manage_account_funnels",
        description="Manage per-account audit configuration (funnel tier + guidelines).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="Show all accounts with tier + guideline preview")

    p_show = sub.add_parser("show", help="Full tier + guidelines for one account")
    p_show.add_argument("email")

    p_tier = sub.add_parser("set-tier", help="Assign or clear the funnel tier")
    p_tier.add_argument("email")
    p_tier.add_argument("tier", help="NF | MF | WF | none")

    p_guide = sub.add_parser("set-guidelines", help="Set guidelines from a text file")
    p_guide.add_argument("email")
    p_guide.add_argument("--file", required=True, help="Path to text file containing guidelines")

    p_seed = sub.add_parser("seed", help="Bulk upsert from CSV")
    p_seed.add_argument("csv_path")

    sub.add_parser("audit", help="Accounts with missing tier or guidelines")

    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
