# API notes (verified)

Everything the ingest relies on, with the source and the date it was checked. "Docs" means the official API documentation. "Observed" means a live, unauthenticated, read-only GET made by me on that date. Anything that is neither is marked **UNVERIFIED**.

This system only calls public market-data endpoints. It uses no trading endpoints and no credentials.

---

## Kalshi

Checked 2026-11-24.

### Base URL and auth
- `https://external-api.kalshi.com/trade-api/v2` is the "Production Trade API server". `https://api.elections.kalshi.com/trade-api/v2` is "also supported". Sources: the OpenAPI `servers` block on https://docs.kalshi.com/api-reference/market/get-markets, and the changelog entry of 2026-05-07 at https://docs.kalshi.com/changelog.
- Auth: the spec has `security: []` for GET /markets and GET /events. The docs do not say in plain words that these endpoints need no auth. Observed: both return 200 without credentials.

### GET /markets
Source: https://docs.kalshi.com/api-reference/market/get-markets
- **`limit`:** default 100, max 1000.
- **`cursor`:** pass it back unchanged. An empty cursor means the end (https://docs.kalshi.com/getting_started/pagination). A cursor that can't be decoded is silently ignored and the first page is returned.
- **`status`:** query values are `unopened`, `open`, `paused`, `closed`, `settled`. Only one status at a time.
- **`mve_filter`:** `only` or `exclude`. I use `exclude` (see DECISIONS).
- **Snapshot caveat:** "Pages are not a snapshot", so the ingest dedupes by `ticker`.
- **Market fields used:** `ticker`, `event_ticker`, `title` (marked deprecated but still returned), `yes_sub_title`, `rules_primary`, `rules_secondary`, `close_time`, `status`, `volume_fp`, `notional_value_dollars`, `market_type`.
- **Response `status` values:** `initialized`, `inactive`, `active`, `closed`, `determined`, `disputed`, `amended`, `finalized`. Querying `status=open` returns markets whose response status is `active` (observed: all 1000 rows on one page were `active`).
- **Volume:** `volume_fp` is "String representation of the market volume in contracts". There is no dollar volume field on markets.
- **Contract value:** `notional_value_dollars` is "The total value of a single contract at settlement in dollars". Observed: `"1.0000"`.
- **Prices:** `*_dollars` fixed-point strings with sub-cent precision. The cent integer fields were removed (changelog, 2026-01-06 onward).
- **Removed field:** `expiration_time` was removed on 2026-11-19 (changelog).

### GET /events
Source: https://docs.kalshi.com/api-reference/events/get-events
- **`limit`:** max 200.
- **`with_nested_markets=true`:** each event includes a `markets` array.
- **Combo markets:** the endpoint excludes multivariate (combo) events.
- **Event fields used:** `event_ticker`, `title`, `sub_title`, `mutually_exclusive`.

### Rate limits
Source: https://docs.kalshi.com/getting_started/rate_limits
- Limits are token based. The Basic Read budget is 200 tokens/s, and most requests cost 10 tokens.
- **Unauthenticated public requests:** the docs say these are "subject to separate limits" but give no number. **UNVERIFIED**, so config uses 5 req/s.
- **429 handling:** a 429 carries no `Retry-After` header. The docs recommend exponential backoff.
- **Caching:** observed `cache-control: public, max-age=15` on GET /events.

### Observations
- A market title can lack its event's context. Example: `title = "Will over 6.5 goals be scored?"`, where only the rules name the match. This is why the ingest stores `event_title`.
- On one page of 1000 open markets (excluding combo markets), 24 had any volume and 0 had empty `rules_primary`.

---

## Polymarket (Gamma API)

Checked 2026-11-24.

### Base URL and auth
- `https://gamma-api.polymarket.com`. Source: https://docs.polymarket.com/getting-started/api.md
- The docs say "Public market data is available without credentials." Observed: 200 without credentials.

### GET /markets/keyset
Source: https://docs.polymarket.com/market-data/discover-markets.md
- **Shape:** returns `{"markets": [...], "next_cursor": "..."}`. Page forward with `after_cursor=<next_cursor>`.
- **Not in the spec:** this endpoint is not in the OpenAPI spec. Its parameters come only from the prose and examples.
- **Open markets:** `closed=false` returns open markets. The same semantics are documented for GET /markets at https://docs.polymarket.com/api-reference/markets/list-markets.
- **Page cap (observed):** `limit` is silently capped at 100. Requests with limit=500, 1000 and 2000 each returned 100 rows. The docs give no maximum.
- **End of listing:** no `next_cursor` marks the last page. The docs don't spell this out. **UNVERIFIED**, so the client stops when `next_cursor` is missing, empty, or the page is empty.

### Market fields used
Source: https://docs.polymarket.com/api-reference/markets/list-markets
- **Core fields:** `id`, `question`, `description`, `endDate`, `outcomes`, `volumeNum`, `active`, `closed`, `archived`, `events`.
- **Rules location:** the docs do not say where the resolution rules live. Observed: `description` holds the full rules text ("This market will resolve to "Yes" if ...").
- **JSON in strings:** `outcomes` and `clobTokenIds` are JSON arrays encoded as strings. Observed: `outcomes = '["Yes", "No"]'`.
- **Volume:** `volumeNum` is a number in USD.
- **Undocumented fields:** the live market object has fields missing from the docs' Market schema: `negRisk`, `feeSchedule`, `feesEnabled`, `takerBaseFee`, `makerBaseFee`, `bestBid`, `bestAsk`. They are not used yet; they are relevant to M4.

### GET /events/keyset
Source: https://docs.polymarket.com/market-data/discover-markets.md
- **Shape:** returns `{"events": [...], "next_cursor": "..."}`.
- **Nested closed markets (observed):** an open event's nested `markets` includes closed markets. Counting from events therefore needs a filter on `closed == false`.

### Rate limits
- **UNVERIFIED:** I found no documented Gamma rate-limit numbers. The only related page I found (https://docs.polymarket.com/programs/builders/tiers.md) says only "Standard", with no figures. Config uses 2 req/s, plus backoff on 429.
- **Caching:** observed `cache-control: public, max-age=300` on events/keyset.

### Counts
- Neither platform exposes a total count. Gate 1 compares the primary listing against an independent second enumeration (see DECISIONS D-006).
