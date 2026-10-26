# NAV data sources & feed-failure diagnosis

## Fmarket REST (primary)

- `POST https://api.fmarket.vn/res/product/get-nav-history` with
  `{"productId": N, "fromDate": "YYYY-MM-DD", "toDate": "YYYY-MM-DD"}`.
  ISO dates only — dd/MM/yyyy or unknown keys return HTTP 400. UA
  `petasos-fmarket-nav/1.0` plus JSON content/accept headers.
- `POST https://api.fmarket.vn/res/products/filter` with
  `{"types": ["NEW_FUND", "TRADING_FUND"], "searchField": "<CODE>",
  "page": 1, "pageSize": 10}` — product lookup; live even while
  `get-nav-history` is frozen. Validated on DCDS (id 28; the record's `code` is
  the legacy fund code
  "VFMVF1", the ticker is `shortName`): the record carries the FRESHEST `nav`
  (updates at publication, same evening) plus `productNavChange.navToPrevious`,
  which cross-checks it against the series tail to the basis point — that pair
  proves WHICH date the nav belongs to (tail + 1 trading day) without a date
  field. But `navDate` is null and `updateAt` is an unrelated record-edit
  timestamp (April while the NAV was published in October), so the value still
  must NOT be appended to the series — use it for signal evaluation and
  freshness checks only. `lastYearNav` = prior Dec-31 NAV (independent anchor).
- To confirm an endpoint is still the one the vendor's own app calls, pull the
  SPA's JS chunks (script-src list from the page HTML, fetch each) and grep for
  the path — the bundle's API constants are the contract of record. Cheaper
  than hooking XHR in a browser and works when the payload builder lives in a
  lazy chunk you cannot trigger headlessly.

## Diagnosing a frozen feed: platform regression vs wrong ID

Replay the sync payload against 2-3 OTHER productIds:

- Every product that returns data is frozen at the SAME `navDate` (with
  matching `createdAt` timestamps) → the platform's dataset is stale. No
  payload, date-format, or endpoint change fixes it; stop churning the request
  shape. Keep the primary configured — a healed upstream backfills silently.
- Other products return current rows while ours is stale → then suspect the
  product ID and re-resolve it.

## Fallback: stockbiz.vn fund page (PRIMARY fallback; wired into funds_sync)

- `GET https://stockbiz.vn/en/personal-finance/mutual-funds/<code>` — plain
  GET, no auth. Carries the fund's FULL daily NAV series (a trailing 1-year
  array plus a current-month array) in ESCAPED Next.js flight data. A full
  desktop Chrome UA is required — a bare UA gets a JS shell with no data.
- Do NOT hand-roll the parser each time: use `scripts/stockbiz_nav_series.py
  <CODE> --overlap <date>=<nav> ...` — it handles the escaping, splits the
  arrays (M/D labels, no year; naive whole-page regexing mixes last-year and
  this-year points under identical labels), resolves years with a backward
  walk per array (a 1-year array holds the same M/D label in two consecutive
  years), and refuses output unless the overlaps validate.
- `funds_sync.py` (>= 1.1.0) calls this automatically when the API is stale
  and REJECTS the series unless >=3 overlapping rows match the vault exactly.
  For one-off manual use, validating the vault's tail row is enough.
- Validate before trusting anything you append by hand: an overlapping date
  must equal the vault series EXACTLY. If values match only after shifting
  every date by one trading day, the VAULT rows are mis-dated (e.g. seeded
  from a regressed API dataset whose navDates were a trading day early —
  Sunday dates in the series prove it); re-seed from stockbiz rather than
  backfilling onto shifted dates.

### Vendor API availability research (applies to any VN finance site)

- Check whether the page has a real API before writing a scraper: hook
  `fetch`/`XMLHttpRequest` in a live browser and read
  `performance.getEntriesByType('resource')`. If the only XHR is an auth
  session call, the data is server-rendered — the SSR page IS the source;
  there is nothing cleaner to switch to.
- A vendor API root often exposes its contract: fetch `https://api.<vendor>/`
  and look for a swagger link (`/swagger/docs/v1` worked for FireAnt), then
  grep the spec paths for the resource (`fund`, `nav`). Grep the SPA's JS
  bundles for `api`/domain strings to find which base URL the app actually
  uses.
- A documented endpoint that returns 401 without credentials (FireAnt's
  `/fmarket/static/get-nav-history`) is a dead end unless the owner wants to
  create an account — check whether a free SSR page carries the same data
  first; same upstream, so the authed API buys nothing.

## Last resort: fmarket.vn fund page (validated)

- `GET https://fmarket.vn/quy/<code>` server-renders the latest NAV as
  `class="nav">X VND` followed by `Cập nhật ngày dd/mm/yyyy` — but ONLY for a
  full desktop UA; a bare `Mozilla/5.0` UA receives a ~22KB JS shell with no
  data, which is why the page was once misjudged as "not a fallback source".
  Always send a complete Chrome UA string.
- Gives only the LATEST point (no series): last resort when stockbiz is
  unreachable, not a gap-filler — use stockbiz for that. Same upstream
  (Fmarket), so a frozen platform can stale it too; re-check on every use.
- `funds_sync.py` (>= 1.0.1) uses this automatically as the final fallback:
  API stale AND stockbiz rejected/unreachable → scrape the page, append if
  the row is newer (dedupe and sort handled by `append_rows`).
