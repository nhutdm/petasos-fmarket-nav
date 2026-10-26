#!/usr/bin/env python3
"""funds_views.py - regenerate 2-Areas/Finance/Funds.md from the registry + series.

Also verifies the series invariants (sorted, unique dates, positive NAV).
Exit 0 iff every check passes AND the note was written.
"""
import datetime as dt
import json
import os
import sys

TZ = dt.timezone(dt.timedelta(hours=7))


def load_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows
    for i, line in enumerate(open(path, encoding="utf-8"), 1):
        if line.strip():
            rows.append(json.loads(line))
    return rows


def check_series(code, rows):
    dates = [r["date"] for r in rows]
    if dates != sorted(dates):
        print(f"GATE FAIL: {code} series not sorted", file=sys.stderr)
        return False
    if len(dates) != len(set(dates)):
        print(f"GATE FAIL: {code} duplicate dates", file=sys.stderr)
        return False
    if any(float(r["nav"]) <= 0 for r in rows):
        print(f"GATE FAIL: {code} non-positive NAV", file=sys.stderr)
        return False
    return True


def pct(now, then):
    return (now / then - 1) * 100


def main():
    vault = os.environ.get("FUNDS_VAULT")
    if not vault:
        print("FATAL: FUNDS_VAULT not set", file=sys.stderr)
        sys.exit(2)
    ledger = os.path.join(vault, os.environ.get("FUNDS_LEDGER_DIR", "2-Areas/Finance/_Ledger"))
    series_dir = os.path.join(ledger, os.environ.get("FUNDS_SERIES_DIR", "funds-series"))
    reg_path = os.environ.get("FUNDS_REGISTRY") or os.path.join(ledger, "funds.yaml")
    if not os.path.exists(reg_path):
        print(f"FATAL: registry missing: {reg_path}", file=sys.stderr)
        sys.exit(2)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from funds_sync import load_registry  # reuse the strict parser
    reg = load_registry(reg_path)

    today = dt.datetime.now(TZ).date()
    lines = [
        "---",
        "tags: [finance, funds, nav]",
        "generated: " + today.isoformat(),
        "generator: petasos-fmarket-nav/scripts/funds_views.py",
        "---",
        "",
        "# Funds",
        "",
        "> Auto-generated from `_Ledger/funds-series/<CODE>.jsonl` - do not hand-edit.",
        "Lot detail and strategy live in [[Assets Detail]].",
        "",
    ]
    ok = True
    for cfg in reg["funds"]:
        code = cfg["code"]
        rows = load_jsonl(os.path.join(series_dir, f"{code}.jsonl"))
        ok = check_series(code, rows) and ok
        lines.append(f"## {code} - {cfg.get('short_name', code)} ({cfg.get('asset_type', '?')})")
        if not rows:
            lines.append("- (no series yet)")
            lines.append("")
            continue
        navs = {r["date"]: float(r["nav"]) for r in rows}
        latest_d = max(navs)
        latest = navs[latest_d]
        lines.append(f"- Latest NAV: **{latest:,.2f}** ({latest_d})")
        for label, days in (("1m", 30), ("3m", 91), ("12m", 365)):
            ref = next((navs[d] for d in sorted(navs, reverse=True)
                        if (dt.date.fromisoformat(latest_d) - dt.date.fromisoformat(d)).days >= days), None)
            if ref:
                lines.append(f"- {label}: {pct(latest, ref):+.2f}%")
        avg = cfg.get("avg_cost_nav")
        units = cfg.get("units")
        if avg and units:
            value = latest * units
            invested = avg * units
            pl = value - invested
            lines.append(f"- Value now: {value:,.0f} (invested {invested:,.0f} @ avg {avg:,.2f} x {units} units)")
            lines.append(f"- Unrealized P&L: {pl:+,.0f} ({pct(latest, avg):+.2f}%)")
        lines.append("")
    out = os.path.join(vault, "2-Areas/Finance/Funds.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    content = "\n".join(lines) + "\n"
    try:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(content)
    except PermissionError:
        # Vault convention: files are world-writable (the vault-side UID differs).
        # External editors sometimes rewrite Funds.md as 0644; the directory
        # stays writable, so unlink-and-recreate is always possible.
        os.remove(out)
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(content)
    try:
        os.chmod(out, 0o666)
    except OSError:
        pass
    print(f"Funds.md regenerated: {out}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
