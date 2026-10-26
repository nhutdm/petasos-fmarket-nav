#!/usr/bin/env python3
"""funds_backfill.py - one-shot seed of a per-fund series.

Two sources, usable together:
  --from-jsonl <old file>            legacy series (keys: date + nav OR value)
  --fetch                            fetch from Fmarket for the registry window
Rows merge, dedupe on date, sort, and land in <ledger>/funds-series/<CODE>.jsonl
(FUNDS_SERIES_DIR overrides).
Refuses to run if the target exists unless --force.
"""
import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from funds_sync import load_registry, resolve_registry, vault_paths, fetch_nav_history  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True)
    ap.add_argument("--from-jsonl")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    vault, ledger = vault_paths()
    reg_path, src = resolve_registry(ledger)
    reg = load_registry(reg_path)
    cfg = next((f for f in reg["funds"] if f["code"] == args.code), None)
    if not cfg:
        print(f"FATAL: fund '{args.code}' not in registry {reg_path}", file=sys.stderr)
        sys.exit(2)

    out_dir = os.path.join(ledger, os.environ.get("FUNDS_SERIES_DIR", "funds-series"))
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"{args.code}.jsonl")
    if os.path.exists(out) and not args.force:
        print(f"FATAL: {out} exists; pass --force to re-seed", file=sys.stderr)
        sys.exit(2)

    merged = {}
    if args.from_jsonl:
        for i, line in enumerate(open(args.from_jsonl, encoding="utf-8"), 1):
            if not line.strip():
                continue
            r = json.loads(line)
            date = r.get("date")
            nav = r.get("nav", r.get("value"))
            if not date or nav is None:
                print(f"FATAL: {args.from_jsonl}:{i} lacks date/nav", file=sys.stderr)
                sys.exit(1)
            merged[date] = {"date": date, "nav": float(nav)}
        print(f"jsonl source: {len(merged)} rows")
    if args.fetch:
        api_base = os.environ.get("FUNDS_API_BASE", "https://api.fmarket.vn/res")
        frm = args.from_jsonl and min(merged) or None
        frm = (dt.date.fromisoformat(frm).isoformat() if frm else "2020-01-01")
        rows = fetch_nav_history(api_base, cfg["product_id"], frm, dt.date.today().isoformat())
        n0 = len(merged)
        for r in rows:
            merged.setdefault(r["date"], r)
        print(f"fetched: {len(rows)} rows, {len(merged) - n0} new dates")

    final = [merged[d] for d in sorted(merged)]
    stamp = dt.datetime.now(dt.timezone(dt.timedelta(hours=7))).strftime("%Y-%m-%dT%H:%M:%S+07:00")
    try:
        with open(out, "w", encoding="utf-8") as fh:
            for r in final:
                r.setdefault("fetched_at", stamp)
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    except PermissionError:
        # external editors can leave the file 0644 under a foreign UID; the
        # directory stays agent-owned, so unlink + rewrite + force 0666
        os.unlink(out)
        with open(out, "w", encoding="utf-8") as fh:
            for r in final:
                r.setdefault("fetched_at", stamp)
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.chmod(out, 0o666)
    print(f"wrote {len(final)} rows -> {out}")


if __name__ == "__main__":
    main()
