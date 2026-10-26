#!/usr/bin/env python3
"""Extract the daily NAV series from a stockbiz.vn fund page (fallback source).

Usage:
  python3 stockbiz_nav_series.py <CODE> [--overlap DATE=NAV ...] [--year N]

Prints rows {"date": "YYYY-MM-DD", "nav": N} as JSONL (sorted, deduped).

--overlap validates pairs against the vault before the series may be trusted
(e.g. --overlap 2026-10-23=94142.61 using the vault's tail row). With any
--overlap given, exit 1 unless every pair matches within 0.005.

Page format notes (validated against the live page):
- The series lives in ESCAPED Next.js flight data: \\"label\\":\\"M/D\\",\\"value\\":N.
  A plain-quote regex finds nothing.
- The page embeds a trailing 1Y array AND a current-month array; naive
  whole-page regexing mixes them. This script splits arrays at any calendar
  decrease that is not a Dec->Jan wrap (chart arrays never step back), then
  resolves years per array with a backward walk.
- Labels are M/D with no year. Chart arrays are chronological and end at the
  latest NAV, so years come from a backward walk per array (handles a 1Y array
  holding the same M/D label for two consecutive years).
  Edge case (documented, not fully solved): around Jan-Mar the break-at-first-
  repeat split can land mid-array. Re-check output around New Year.
Only stdlib is used; a full desktop Chrome UA is required or the page is a JS
shell with no data.
"""

import argparse
import datetime
import json
import re
import sys
import urllib.request

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def fetch_html(code: str) -> str:
    url = f"https://stockbiz.vn/en/personal-finance/mutual-funds/{code.lower()}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def unescape(seg: str) -> str:
    for old, new in (("\\\\\\\"", '"'), ("\\\\\"", '"'), ('\\\"', '"'), ("\\\\", "")):
        seg = seg.replace(old, new)
    return seg


def extract_series(html: str) -> list[tuple[str, float]]:
    """Return sorted unique (ISO date, nav) points across all chart arrays.

    Each chart array is chronological and ends at the latest published NAV, so
    years are resolved by walking each break-separated segment BACKWARD from
    the segment's last label: start at the current year (or the year before if
    that label lies ahead of today in the calendar), and decrement the year
    every time the label jumps forward (wrap past Jan 1). This handles 1Y
    arrays that contain the same M/D label for two consecutive years.
    """
    positions = [m.start() for m in re.finditer("label", html)]
    if not positions:
        raise SystemExit("no flight-data labels found - page is likely a JS shell (UA too bare?)")
    runs, cur = [], [positions[0]]
    for p in positions[1:]:
        if p - cur[-1] < 150:
            cur.append(p)
        else:
            runs.append(cur)
            cur = [p]
    runs.append(cur)

    today = datetime.date.today()
    rows: set[tuple[str, float]] = set()
    for run in runs:
        if len(run) <= 5:  # aria-labels and nav chrome, not chart data
            continue
        seg = unescape(html[max(0, run[0] - 30): run[-1] + 80])
        pts = re.findall(r'"label":"(\d{1,2}/\d{1,2})","value":([\d.]+)', seg)
        segment: list[tuple[str, str]] = []
        prev_md = None
        for lab, val in pts:
            mo, day = (int(x) for x in lab.split("/"))
            md = (mo, day)
            # array boundary: a label seen before, OR a calendar decrease that
            # is not a Dec->Jan year wrap (a chart array never steps back)
            is_wrap = prev_md is not None and prev_md[0] == 12 and mo == 1
            is_repeat = md in {(int(l.split("/")[0]), int(l.split("/")[1]))
                               for l, _ in segment}
            if segment and prev_md is not None and (is_repeat or (md < prev_md and not is_wrap)):
                rows.update(_resolve_years(segment, today))
                segment = []
            segment.append((lab, val))
            prev_md = md
        if segment:
            rows.update(_resolve_years(segment, today))
    return sorted(rows)


def _resolve_years(segment: list[tuple[str, str]], today: datetime.date) -> set[tuple[str, float]]:
    """Assign calendar years to one chronological M/D segment (backward walk)."""
    out: set[tuple[str, float]] = set()
    year = None
    prev_md = None
    for lab, val in reversed(segment):
        mo, day = (int(x) for x in lab.split("/"))
        if year is None:
            year = today.year if (mo, day) <= (today.month, today.day) else today.year - 1
        elif prev_md is not None and (mo, day) > prev_md:
            year -= 1  # walked back across a Dec->Jan boundary
        prev_md = (mo, day)
        try:
            iso = datetime.date(year, mo, day).isoformat()
        except ValueError:  # e.g. 2/29 in a non-leap year - drop the point
            continue
        out.add((iso, float(val)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("code")
    ap.add_argument("--overlap", action="append", default=[],
                    metavar="DATE=NAV", help="vault row to validate against, repeatable")
    ap.add_argument("--year", type=int, default=None)
    args = ap.parse_args()

    html = fetch_html(args.code)
    series = extract_series(html)
    if args.year:
        series = [(d.replace(str(datetime.date.today().year), str(args.year), 1), v)
                  for d, v in series]

    ok = True
    lookup = dict(series)
    for pair in args.overlap:
        date, _, nav = pair.partition("=")
        got = lookup.get(date)
        match = got is not None and abs(got - float(nav)) < 0.005
        print(f"overlap {date}: stockbiz={got} vault={nav} {'MATCH' if match else 'MISMATCH'}")
        ok &= match
    if args.overlap and not ok:
        print("VALIDATION FAILED - do not trust this series", file=sys.stderr)
        return 1

    for d, v in series:
        print(json.dumps({"date": d, "nav": v}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
