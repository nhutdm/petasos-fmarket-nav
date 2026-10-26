#!/usr/bin/env python3
"""funds_sync.py - daily NAV sync for Fmarket funds into an Obsidian vault.

Architecture (v1.2.0, Fmarket-API-only per owner directive 26/10):
  DAILY  - POST /res/products/filter gives the freshest NAV (published the same
           evening) but carries NO trade date. The row is appended only when
           productNavChange.navToPrevious matches the series tail's change to
           within rounding; the date is then inferred as the next weekday after
           the tail (VN public holidays are NOT modelled). Any mismatch = skip
           and wait for the monthly reconcile - never guess a value or date.
  MONTHLY- POST /res/product/get-nav-history (auto on FUNDS_MONTHLY_DAY, or
           --monthly) fetches authoritative dated rows for the trailing ~45
           days and RECONCILES the file: history values win on every overlapping
           date, so any mis-inferred daily row self-heals. While the history
           feed is frozen it returns nothing and the daily inferred rows carry
           the series until the API heals.

Writes ONLY the per-fund series files and the generated Funds.md note.
Exit codes: 0 ok (alerts print to stdout, silence = nothing to report),
1 data/chain error, 2 config error, 3 network failure.
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

TZ = dt.timezone(dt.timedelta(hours=7))  # Asia/Bangkok


def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        print(f"FATAL: missing env {name}", file=sys.stderr)
        sys.exit(2)
    return v


def vault_paths():
    vault = env("FUNDS_VAULT", required=True)
    ledger = os.path.join(vault, env("FUNDS_LEDGER_DIR", "2-Areas/Finance/_Ledger"))
    return vault, ledger


def resolve_registry(ledger):
    """$FUNDS_REGISTRY -> <ledger>/funds.yaml -> bundled template. Loud on miss."""
    explicit = os.environ.get("FUNDS_REGISTRY")
    candidates = ([("env $FUNDS_REGISTRY", explicit)] if explicit else []) + [
        ("vault", os.path.join(ledger, "funds.yaml")),
        ("bundled", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "templates", "funds.yaml")),
    ]
    for label, path in candidates:
        if path and os.path.exists(path):
            return path, label
    searched = "\n".join(f"  {lbl}: {p}" for lbl, p in candidates)
    print(f"FATAL: funds registry not found. Searched:\n{searched}", file=sys.stderr)
    sys.exit(2)


def load_registry(path):
    """Strict registry loader (PyYAML + validation).

    Duplicate keys, unknown top-level keys, missing required fund fields, and
    non-dict structure are all FATAL (exit 2) - never silently tolerated.
    """
    try:
        import yaml
    except ImportError:
        print("FATAL: PyYAML required - pip install pyyaml", file=sys.stderr)
        sys.exit(2)

    def no_dupes(pairs):
        d = {}
        for k, v in pairs:
            if k in d:
                print("FATAL: " + path + " duplicate key '" + str(k) + "'", file=sys.stderr)
                sys.exit(2)
            d[k] = v
        return d

    class SafeLoaderNoDup(yaml.SafeLoader):
        pass
    SafeLoaderNoDup.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
        lambda loader, node: no_dupes(loader.construct_pairs(node, deep=True)))
    try:
        with open(path, encoding="utf-8") as fh:
            doc = yaml.load(fh, Loader=SafeLoaderNoDup)
    except yaml.YAMLError as e:
        print("FATAL: " + path + " is not valid YAML: " + str(e), file=sys.stderr)
        sys.exit(2)
    if not isinstance(doc, dict) or set(doc) != {"funds"}:
        print("FATAL: " + path + " must contain exactly a 'funds:' mapping", file=sys.stderr)
        sys.exit(2)
    if not isinstance(doc["funds"], list) or not doc["funds"]:
        print("FATAL: " + path + " 'funds:' must be a non-empty list", file=sys.stderr)
        sys.exit(2)
    for f in doc["funds"]:
        if not isinstance(f, dict):
            print(f"FATAL: {path} fund entry is not a mapping: " + repr(f), file=sys.stderr)
            sys.exit(2)
        for reqf in ("code", "product_id", "asset_type"):
            if reqf not in f:
                print(f"FATAL: {path} fund missing '{reqf}': " + repr(f), file=sys.stderr)
                sys.exit(2)
        if not isinstance(f["product_id"], int):
            print(f"FATAL: {path} fund '{f['code']}' product_id must be an int", file=sys.stderr)
            sys.exit(2)
    return doc


def http_json(url, body, retries=3):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": "petasos-fmarket-nav/1.2.0"})
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(2 * (attempt + 1))
    print(f"FATAL: Fmarket fetch failed after {retries} attempts: {last}", file=sys.stderr)
    sys.exit(3)


def fetch_nav_history(api_base, product_id, from_date, to_date):
    """Authoritative dated rows. Returns [] when the feed serves nothing new
    (e.g. the 24/10+ frozen snapshot) - callers must tolerate that."""
    doc = http_json(f"{api_base}/product/get-nav-history",
                    {"productId": int(product_id), "fromDate": from_date, "toDate": to_date})
    if doc.get("status") != 200:
        print(f"FATAL: API status {doc.get('status')} for product {product_id}", file=sys.stderr)
        sys.exit(3)
    return [{"date": r["navDate"], "nav": float(r["nav"])}
            for r in (doc.get("data") or []) if r.get("navDate")]


def fetch_latest_product(api_base, code):
    """Freshest NAV + navToPrevious from /res/products/filter.
    Returns {"nav": float, "nav_to_previous": float} or None if not found."""
    doc = http_json(f"{api_base}/products/filter",
                    {"types": ["NEW_FUND", "TRADING_FUND"], "searchField": code,
                     "page": 1, "pageSize": 10})
    rows = (doc.get("data") or {}).get("rows") or []
    for r in rows:
        if (r.get("shortName") or "").upper() == code.upper():
            ch = r.get("productNavChange") or {}
            if r.get("nav") is None or ch.get("navToPrevious") is None:
                return None
            return {"nav": float(r["nav"]), "nav_to_previous": float(ch["navToPrevious"])}
    return None


def next_weekday_after(date_iso):
    """Inferred trade date for a NAV published after date_iso: the next Mon-Fri
    strictly after it. VN public holidays are NOT modelled - the monthly
    get-nav-history reconcile corrects any wrong inference."""
    d = dt.date.fromisoformat(date_iso) + dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


def infer_daily_row(latest, tail, today):
    """Return (row_or_None, reason).

    Accepts the freshest products/filter nav ONLY when navToPrevious matches
    the change from the series tail to within rounding (2dp percent). The date
    is then the next weekday after the tail. Anything else is skipped - never
    guessed.
    """
    tail_nav = float(tail["nav"])
    nav = latest["nav"]
    if abs(nav - tail_nav) < 0.005:
        return None, "nav unchanged since tail"
    if nav < tail_nav * 0.5:  # split/currency nonsense guard
        return None, "nav implausibly far from tail"
    implied = (nav / tail_nav - 1) * 100.0
    if abs(implied - latest["nav_to_previous"]) > 0.05:
        return None, (f"navToPrevious mismatch: stated {latest['nav_to_previous']:+.2f}% "
                      f"vs implied {implied:+.2f}% from tail - probable missed session; "
                      f"waiting for monthly reconcile")
    d = next_weekday_after(tail["date"])
    if d > today:
        return None, "inferred date would be in the future"
    return {"date": d.isoformat(), "nav": nav}, "navToPrevious cross-check ok"


def reconcile_history(path, existing, hist_rows, dry):
    """Merge authoritative dated rows over the series: on every overlapping
    date the history value WINS (repairs mis-inferred daily rows); history-only
    dates are added; series-only dates outside the history window are kept.
    Returns (merged_rows, n_corrected, n_added)."""
    hist = {r["date"]: r["nav"] for r in hist_rows}
    merged, corrected, added = [], 0, 0
    seen = set()
    for r in existing:
        if r["date"] in hist:
            if abs(hist[r["date"]] - r["nav"]) >= 0.005:
                corrected += 1
            merged.append({"date": r["date"], "nav": hist[r["date"]],
                           "fetched_at": r.get("fetched_at")})
        else:
            merged.append(dict(r))
        seen.add(r["date"])
    for d in sorted(hist):
        if d not in seen:
            merged.append({"date": d, "nav": hist[d]})
            added += 1
    merged.sort(key=lambda r: r["date"])
    if not dry and (corrected or added):
        stamp = dt.datetime.now(TZ).strftime("%Y-%m-%dT%H:%M:%S+07:00")
        for r in merged:
            r.setdefault("fetched_at", stamp)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for r in merged:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.chmod(tmp, 0o666)
        os.replace(tmp, path)
    return merged, corrected, added


def load_series(path):
    if not os.path.exists(path):
        return []
    rows, seen = [], set()
    for i, line in enumerate(open(path, encoding="utf-8"), 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            print(f"FATAL: {path}:{i} is not valid JSON", file=sys.stderr)
            sys.exit(1)
        if r["date"] in seen:
            print(f"FATAL: {path}:{i} duplicate date {r['date']}", file=sys.stderr)
            sys.exit(1)
        seen.add(r["date"])
        rows.append(r)
    if rows != sorted(rows, key=lambda r: r["date"]):
        print(f"FATAL: {path} not sorted by date", file=sys.stderr)
        sys.exit(1)
    return rows


def append_rows(path, existing, new_rows, dry):
    dates = {r["date"] for r in existing}
    fresh = sorted([r for r in new_rows if r["date"] not in dates], key=lambda r: r["date"])
    if not fresh:
        return []
    if dry:
        return fresh
    rows_out = existing + fresh
    try:
        with open(path, "a", encoding="utf-8") as fh:
            for r in fresh:
                r["fetched_at"] = dt.datetime.now(TZ).strftime("%Y-%m-%dT%H:%M:%S+07:00")
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    except PermissionError:
        # Vault files are shared with an external sync process running as a
        # different UID; it can rewrite series files as 0644. The directory
        # stays writable, so rewrite-in-place (existing + fresh, 0666) works.
        tmp = path + ".tmp"
        stamp = dt.datetime.now(TZ).strftime("%Y-%m-%dT%H:%M:%S+07:00")
        with open(tmp, "w", encoding="utf-8") as fh:
            for r in rows_out:
                r.setdefault("fetched_at", stamp)
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.chmod(tmp, 0o666)
        os.replace(tmp, path)
    return fresh


def evaluate_signals(cfg, series, today, alerted_this_month):
    """Return alert strings. alerted_this_month: set of (code, tier) already sent."""
    alerts = []
    if not series:
        return alerts
    nav = series[-1]["nav"]
    avg = cfg.get("avg_cost_nav")
    units = cfg.get("units")
    code = cfg["code"]
    if avg and units:
        value = nav * units
        cap = cfg.get("cap_value")
        if cap and value >= cap:
            alerts.append(f"[{code}] CAP WATCH: value {value:,.0f} >= cap {cap:,.0f} - diversification decision due (rule: fund cap)")
    if avg:
        for tier in cfg.get("buy_more_thresholds", []):
            drop = tier["drop_pct"]
            thr = avg * (1 - drop / 100.0)
            if nav <= thr and (code, f"buy{drop}") not in alerted_this_month:
                alerts.append(f"[{code}] BUY-MORE signal: NAV {nav:,.2f} <= avg -{drop}% ({thr:,.2f}) - add {tier['amount']:,.0f} VND")
    dca = cfg.get("dca") or {}
    if dca.get("amount") and today.day in dca.get("days", []):
        alerts.append(f"[{code}] DCA window (day {today.day}): order {dca['amount']:,.0f} VND before 14:30 cut-off")
    return alerts


def regen_views(repo_scripts, dry):
    if dry:
        return
    rc = subprocess.call([sys.executable, os.path.join(repo_scripts, "funds_views.py")])
    if rc != 0:
        print(f"FATAL: view regeneration failed rc={rc}", file=sys.stderr)
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="Sync Fmarket NAV series into the vault")
    ap.add_argument("--dry", action="store_true", help="fetch + evaluate, write NOTHING")
    ap.add_argument("--fund", help="limit to one fund code")
    ap.add_argument("--monthly", action="store_true",
                    help="force the get-nav-history reconcile pass")
    ap.add_argument("--quiet", action="store_true", help="print ONLY signals (watchdog mode)")
    args = ap.parse_args()

    vault, ledger = vault_paths()
    reg_path, reg_src = resolve_registry(ledger)
    reg = load_registry(reg_path)
    api_base = env("FUNDS_API_BASE", "https://api.fmarket.vn/res")
    monthly_day = int(env("FUNDS_MONTHLY_DAY", "1"))
    print(f"registry: {reg_path} ({reg_src}); funds: {len(reg['funds'])}; api: {api_base}")

    today = dt.datetime.now(TZ).date()
    month_key = today.strftime("%Y-%m")
    all_alerts = []
    for cfg in reg["funds"]:
        code = cfg["code"]
        if args.fund and code != args.fund:
            continue
        series_dir = os.path.join(ledger, env("FUNDS_SERIES_DIR", "funds-series"))
        os.makedirs(series_dir, exist_ok=True)
        path = os.path.join(series_dir, f"{code}.jsonl")
        existing = load_series(path)
        if not existing:
            # migration: seed from the legacy <ledger>/funds/ location (owned by the
            # vault-side UID; its dir is often non-writable, so copy - never move)
            legacy = os.path.join(ledger, "funds", f"{code}.jsonl")
            if os.path.exists(legacy):
                try:
                    legacy_rows = load_series(legacy)
                    append_rows(path, [], legacy_rows, args.dry)
                    existing = load_series(path)
                    print(f"{code}: seeded {len(legacy_rows)} rows from legacy funds/{code}.jsonl")
                except (OSError, ValueError) as e:
                    print(f"WARN: could not seed from {legacy}: {e}", file=sys.stderr)

        # ---- MONTHLY: authoritative dated rows + reconcile (API-heal pass).
        # Optional by design: a hard API failure must not kill the daily pass.
        if args.monthly or today.day == monthly_day:
            tail_iso = existing[-1]["date"] if existing else today.isoformat()
            frm = (dt.date.fromisoformat(tail_iso) - dt.timedelta(days=45)).isoformat()
            try:
                hist = fetch_nav_history(api_base, cfg["product_id"], frm, today.isoformat())
            except SystemExit:
                hist = []
                print(f"WARN: {code} monthly get-nav-history failed; "
                      f"series left as-is", file=sys.stderr)
            if hist:
                merged, corrected, added = reconcile_history(path, existing, hist, args.dry)
                if corrected or added:
                    print(f"{code}: monthly reconcile - {corrected} corrected, "
                          f"{added} added from get-nav-history")
                else:
                    print(f"{code}: monthly reconcile - history agrees with series")
                existing = merged  # dry reconcile writes nothing but previews truth
            else:
                print(f"{code}: monthly reconcile - get-nav-history returned nothing "
                      f"(API frozen?); series left as-is")

        # ---- DAILY: freshest products/filter NAV, cross-checked, date inferred
        fresh = []
        latest = fetch_latest_product(api_base, code)
        if latest is None:
            print(f"WARN: {code} not found in products/filter", file=sys.stderr)
        elif existing:
            row, why = infer_daily_row(latest, existing[-1], today)
            if row:
                fresh = append_rows(path, existing, [row], args.dry)
            if not args.quiet:
                print(f"{code}: daily products/filter nav {latest['nav']:,.2f} - {why}"
                      + (f"; appended {row['date']}" if fresh else ""))
        else:
            # empty series: seed from history (products/filter cannot start a
            # series - its value carries no date)
            rows = fetch_nav_history(api_base, cfg["product_id"],
                                     (today - dt.timedelta(days=14)).isoformat(),
                                     today.isoformat())
            fresh = append_rows(path, existing, rows, args.dry)
            if not args.quiet:
                print(f"{code}: seeded {len(fresh)} rows from get-nav-history")

        # signal month-memory: marker file per fund+month to avoid re-alerting
        marker = os.path.join(series_dir, f".alerts-{code}")
        alerted = set()
        if os.path.exists(marker):
            for line in open(marker, encoding="utf-8"):
                mk, tier = line.strip().split("|")
                if mk == month_key:
                    alerted.add((code, tier))
        series_now = existing + fresh if args.dry else load_series(path)
        alerts = evaluate_signals(cfg, series_now, today, alerted)
        if not args.dry:
            for a in alerts:
                tier = "buy" + a.split("avg -")[1].split("%")[0] if "avg -" in a else "other"
                if (code, tier) not in alerted:
                    with open(marker, "a", encoding="utf-8") as fh:
                        fh.write(f"{month_key}|{tier}\n")
        if not args.quiet:
            print(f"[{code}] {'(dry) ' if args.dry else ''}new rows: {len(fresh)}"
                  + (f" -> {fresh[-1]['date']} nav {fresh[-1]['nav']:,.2f}" if fresh else " (series current)"))
        all_alerts.extend(alerts)

    regen_views(os.path.dirname(os.path.abspath(__file__)), args.dry)
    if all_alerts:
        print("\n".join(all_alerts))
    if not args.quiet:
        print(f"{'DRY RUN - nothing written' if args.dry else 'sync ok'}")


if __name__ == "__main__":
    main()
