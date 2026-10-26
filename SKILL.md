---
name: petasos-fmarket-nav
description: Sync Fmarket fund NAVs into an Obsidian vault with alerts.
version: 1.0.0
author: Nhut Duong (nhutdm), Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [Fmarket, NAV, Funds, Obsidian, Finance]
    related_skills: [petasos-finance-vault]
    config:
      - key: petasos.vault_path
        description: Absolute path to the Obsidian vault holding the finance area
        default: ""
        prompt: Obsidian vault path (e.g. ~/Obsidian/MyVault)
    blueprint:
      schedule: "0 20 * * *"
      deliver: telegram
      prompt: "Fmarket NAV sync: fetch daily NAVs, append per-fund series, evaluate buy-more/DCA/cap signals, regenerate Funds.md. Silent unless a signal fires."
---

# Fmarket Fund NAV

Daily NAV sync from Fmarket.vn into per-fund JSONL series in an Obsidian vault,
with signal evaluation (buy-more thresholds vs avg cost, DCA windows, portfolio
cap) and a generated `Funds.md` note. Generic and multi-fund: every fund is one
registry row. Depends on PyYAML and network access to api.fmarket.vn.

## When to Use

- Daily NAV sync for any fund tracked in the vault registry
- Adding a fund (one registry row + backfill)
- Backfilling a series from a legacy file or from the API
- Regenerating the `Funds.md` view

Don't use for: transaction ledger work (use `petasos-finance-vault`); placing
actual buy orders (alerts are reminders only).

## Requirements & Install

- Python 3.10+ and PyYAML (`pip install pyyaml`; missing = LOUD exit 2)
- `FUNDS_VAULT` (or `skills.config.petasos.vault_path`) = abs vault path

## How to Run

```bash
export FUNDS_VAULT=<abs vault path>
python3 {SKILL_DIR}/scripts/funds_sync.py --dry    # SAFE: fetch + evaluate, writes NOTHING
python3 {SKILL_DIR}/scripts/funds_sync.py          # daily sync; signals print to stdout
python3 {SKILL_DIR}/scripts/funds_sync.py --quiet  # watchdog cron mode: print ONLY signal lines
python3 {SKILL_DIR}/scripts/funds_views.py         # regenerate Funds.md + series gates (exit 0 iff pass)
python3 {SKILL_DIR}/scripts/funds_backfill.py --code EXMP \
    --from-jsonl <old series> --fetch --force      # one-shot seed
```

## Registry (data, never code)

Resolution: `$FUNDS_REGISTRY` -> `<ledger>/funds.yaml` -> bundled synthetic
template (first hit wins; a missing explicit path is a LOUD error, exit 2).
Per fund: `code`, `product_id` (int), `short_name`, `asset_type`, `avg_cost_nav`
(update after every purchase - thresholds recompute from it), `units`,
`cap_value`, `dca` (amount + days), `buy_more_thresholds` (drop_pct + amount),
`no_sell_before`. Adding fund #2 = one row + `funds_backfill.py --fetch`.

## Data layout

- `<ledger>/funds-series/<CODE>.jsonl` - one `{"date", "nav", "fetched_at"}` per line,
  sorted, unique dates, append-only. (`funds/` is the legacy location: funds_sync
  auto-seeds from it when the new file is empty. A nested data dir can sit 0755
  owned by the vault-side UID, which defeats unlink/replace writes - the series
  store lives in `funds-series/` because the agent creates and owns it.)
- `<ledger>/funds-series/.alerts-<CODE>` - month|tier markers (one alert per tier/month)
- `2-Areas/Finance/Funds.md` - GENERATED; never hand-edit

## Sync architecture (Fmarket-API-only)

- **DAILY (every cron run):** `POST /res/products/filter` returns the freshest
  NAV (published the same evening) but NO trade date. The row is appended only
  when `productNavChange.navToPrevious` matches the series tail's change to
  within rounding; the date is then inferred as the next weekday after the tail
  (VN holidays NOT modelled). Any mismatch = skip and wait for the monthly
  reconcile - never guess a value or date. A frozen products/filter therefore
  pauses the series instead of corrupting it.
- **MONTHLY (auto on `FUNDS_MONTHLY_DAY`, default day 1; `--monthly` forces):**
  `get-nav-history` fetches authoritative dated rows for the trailing ~45 days
  and RECONCILES: history values win on every overlapping date, so any
  mis-inferred daily row (holiday, missed cron) self-heals. While the history
  feed is frozen it returns nothing and daily rows carry the series.
- **stockbiz.vn is OUT of the sync path.** `stockbiz_nav_series.py` remains as
  an offline repair utility only: when local data is stale/mismatched, extract
  its ~13-month series and re-seed via `funds_backfill.py --from-jsonl ...
  --force` (validated against >=2 vault anchors first) - e.g. to repair a block
  that a regressed API had seeded with shifted navDates.
- `--from-date` is gone from funds_sync: fetch windows derive from the series
  tail, never from flags.

## Signals

Evaluated after each sync; print to stdout (cron watchdog delivers to Telegram;
silence = nothing to report):

- `BUY-MORE [code] NAV <= avg -N%` - add the tier amount (deduped per month)
- `DCA window (day 7/8)` - order reminder before the 14:30 cut-off
- `CAP WATCH` - current value reached the registry cap

## Procedure

1. `funds_sync.py --dry` - confirm the plan (new rows, signals); writes nothing
2. `funds_sync.py` - live write; require exit 0
3. After ANY registry edit, run `--dry` - the registry feeds the live engine and
   a malformed file fails loudly naming the problem

## Pitfalls

- Fmarket REST under `/rest/` needs auth; `/res/` does not. NAV history:
  `POST /res/product/get-nav-history` `{"productId", "fromDate", "toDate"}`
  (returns rows `{nav, navDate}`, weekday cadence, ~1-year windows -
  sync incrementally from the series' last date). Product lookup:
  `POST /res/products/filter` with `{"types": [...], "fundAssetTypes": [...],
  "searchField": "...", "page": 1, "pageSize": N}`.
- Send ISO date strings only (`YYYY-MM-DD`). Epoch-milliseconds return a
  misleading HTTP 500 "system maintenance" - that message is a bad request
  shape, not an outage; re-check the payload before treating it as downtime.
- The `/res/products/filter` product record carries a fresher `nav` than the
  `get-nav-history` series (the record updates at publication, dated rows land
  later). NEVER hand-append a record NAV into the series: it has no trade date,
  and a guessed date conflicts with the official rows when they land. Let the
  sync self-heal - it fetches incrementally from the series' last date.
- To discover undocumented request shapes, do NOT guess bodies - hook the
  vendor SPA's `XMLHttpRequest`/`fetch` in a real browser, trigger the page
  action client-side (hook survives SPA routing, dies on full reload), and read
  the captured request bodies; `performance.getEntriesByType('resource')`
  reveals endpoint paths cheaply.
- Windows older than ~1 year silently return nothing - backfill in year chunks
  or from a legacy file.
- Hand-editing series files is disallowed; use `funds_backfill.py --force` to
  re-seed (it dedupes and sorts).
- A series with weekend dates (Sat/Sun rows) is corrupt: NAVs exist only on
  trading days, so the block was seeded with shifted navDates. Prove it by
  validating values against a trade-date source (values match exactly after a
  ±1-trading-day shift), then re-seed the whole block — do not append new rows
  around shifted ones, the offset rows poison every gap-fill and threshold
  check. See `references/nav-data-sources.md` for the validated workflow.
- A signal that fired inside a data gap is invisible to the sync: the engine
  only evaluates rows it has. When reviewing a fund after a gap (frozen feed,
  missed crons), pull the missing window from a fallback source FIRST and check
  buy-more/DCA thresholds across it before concluding no signal fired.
- NAV publishes in the evening (VN); run the cron after ~19:30 or you re-fetch
  yesterday.
- When a sync appends nothing, prove whether the FEED is broken before suspecting
  the sync: replay `get-nav-history` with the sync's exact payload and UA, then
  check each returned row's `navDate` against the REQUESTED window — the API can
  answer status 200 with rows dated far outside it (a year-old navDate for a
  current window = the upstream dataset is regressed). Never hand-patch the
  series with a value from a broken feed; the series stays the source of truth,
  and the next healthy sync backfills from the last real date. Validated
  fallbacks (stockbiz.vn and the fmarket.vn fund page, both matched the vault to
  the satong) and the multi-product freeze probe live in
  `references/nav-data-sources.md`. `funds_sync.py` (>= 1.1.0) already wires
  the fallback chain: API stale → stockbiz full series (overlap-guarded) →
  fmarket page latest-NAV scrape.

## Verification

- `python3 -m unittest discover tests` from the repo root: fully green (includes
  the privacy sweep - the repo must never contain user-specific figures)
- After any data-layout or script-interface change, grep `tests/` for the old
  path/name and update those tests IN THE SAME change - tests lag refactors, and
  a stale suite fails (or passes vacuously) exactly when you need it as a gate
- `funds_views.py` exit 0 = series gates pass (sorted, unique, positive NAV)
- After the daily sync, `Funds.md` mtime updates and the last row matches the API

## Watchdog cron: `last_status: ok` does NOT mean the sync succeeded

The cron wrapper always exits 0 and only PRINTS on failure, so the engine logs
`last_status: ok` even for a failed sync. After any run (or when checking status),
read the delivered output file under `<cron output dir>/<job_id>/<latest>.md` and
grep for `FAILED rc=` - that is the only reliable success signal. The failure
notice still goes to Telegram, but if it was missed, the cron list looks healthy
while the view is stale. To reproduce a reported failure, run the sync manually
with the wrapper's env (`FUNDS_VAULT`, `FUNDS_LEDGER_DIR`, `TZ=Asia/Bangkok`) and
read the traceback - `--quiet` swallows it in cron.

## Vault file permissions drift

Vault files are world-writable (0666) because the external sync/editor UID differs
from the agent UID. External editors sometimes rewrite generated files as 0644,
which makes the next write fail with PermissionError. `funds_views.py` (>= 1.0.1)
self-heals: on PermissionError it unlinks and rewrites, then forces 0666; the
sync's `append_rows` (>= 1.0.1) does the same for series files (temp file +
`os.replace`). Any new script that writes into the vault needs the same EACCES
recovery - never assume a plain `open(path, "w")` succeeds on a file an external
app may have recreated. TWO LIMITS to check before trusting a self-heal: the
file must be writable, AND the containing directory must be writable by the
agent UID - `_Ledger/` is world-writable, but a nested data directory can sit
at 0755 owned by the vault-side UID, which defeats unlink/replace entirely (no
agent-side code fixes it; the owner must `chmod 666 <file>` / `chmod 777 <dir>`,
or the sync tool must be set to ignore permissions so files stop landing as
0644). This is exactly why the series store now lives in `_Ledger/funds-series/`,
which the agent creates and owns.

## Repo commit/push convention

The repo has no bundled git identity - configure it locally before committing:
`user.name=hermes-researcher`, `user.email=hello@nhutduong.com`, SSH signing
(`user.signingkey=<home>/.ssh/nhutdm_signing`, `gpg.format=ssh`,
`commit.gpgsign=true`, `gpg.ssh.allowedsignersfile=<home>/.ssh/allowed_signers`) -
mirror an existing workspace repo's local config. Push needs `gh auth setup-git`
after sourcing `$HERMES_HOME/.env`. Pushing to the private remote is an external
action - confirm with the owner before pushing.
