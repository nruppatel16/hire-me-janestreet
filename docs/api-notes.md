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
- **Nested inactive markets (observed 2026-11-24, Gate 1):** open events also nest about 26.7k markets with `closed=false` and `active=false`. These are inactive outcome slots in multi-outcome events (for example placeholder candidates "Person AC", "Person AD" in "Democratic Presidential Nominee 2028") plus archived markets. `GET /markets/keyset?closed=false` does **not** return them: 0 of 151,964 stored markets are inactive. Counts from the two endpoints agree only when the events count also requires `active == true` (0.06% gap). Not documented.
- **Id filter (observed):** `GET /markets?id=<a>&id=<b>` returned an empty array for those inactive ids, even with `closed=any`. Unexplained.

### Rate limits
- **UNVERIFIED:** I found no documented Gamma rate-limit numbers. The only related page I found (https://docs.polymarket.com/programs/builders/tiers.md) says only "Standard", with no figures. Config uses 2 req/s, plus backoff on 429.
- **Caching:** observed `cache-control: public, max-age=300` on events/keyset.

### Measured counts (2026-11-24)
- Kalshi open markets, combo markets excluded: 174,975. 7,760 pass the $10K gate (first run). Yash's June 2026 figure was ~44,400.
- Polymarket `closed=false` markets: 151,964. About 32k are past `endDate` and awaiting resolution, and 228 have no `endDate`. Yash's figure was ~45,000.
- A full Polymarket ingest takes about 13 minutes at the unverified 2 req/s (100 rows per page).

### Counts
- Neither platform exposes a total count. Gate 1 compares the primary listing against an independent second enumeration (see DECISIONS D-006).

---

## Gemini (Google AI)

Checked 2026-11-24.

### Docs and auth
- Docs live at aistudio.google.com/docs (ai.google.dev redirects there): embeddings, rate-limits, pricing, structured-output, models.
- Base URL: `https://generativelanguage.googleapis.com/v1beta`. The key goes in the `x-goog-api-key` header.

### Embedding models (observed via `GET /models` with this key)
- Available: `gemini-embedding-001`, `gemini-embedding-2`, `gemini-embedding-2-preview`.
- The docs call `gemini-embedding-2` "the latest model". Its default dimension is 3072, and 768, 1536 and 3072 are recommended.
- `gemini-embedding-2` doesn't accept `task_type`; the docs suggest prompt prefixes instead. Recipe v1 uses no prefix, because both sides of a pair are treated the same.

### batchEmbedContents (observed)
- **Batch size:** at most 100 requests per call. 101 returns 400: "at most 100 requests can be in one batch".
- **Vectors:** 768-dimensional vectors come back with unit norm (1.0000).
- **Usage:** the response includes `usageMetadata.promptTokenCount`.

### Rate limits (observed on this key)
- **Embedding:** about 100 embedded texts per minute. A second batch of 100 within a minute returns 429 "You exceeded your current quota". After roughly a minute it succeeds again. The docs only show tier limits inside AI Studio, behind sign-in.
- **Daily embedding cap (observed 2026-11-24):** the free tier has a **daily** cap of 1,000 embed requests per model, and each text in a batch counts as one request. The 429 names the quota: `embed_content_free_tier_requests, limit: 1000, model: gemini-embedding-2`. It resets at midnight Pacific (rate-limits docs).
- **Text models:** limits are not stated in the public docs.

### Pricing (docs)
- `gemini-embedding-2`: $0.20 per 1M input tokens, and free of charge on the free tier.

### Structured output (observed)
- `generateContent` with `generationConfig.responseMimeType = "application/json"` plus `responseJsonSchema` (or `responseSchema`) returns schema-conforming JSON on `gemini-3.5-flash-lite`.
- The docs page shows only the newer Interactions API (`/v1beta/interactions`).

### Deprecations (docs)
- `gemini-2.5-flash` routes to `gemini-3.5-flash-lite` from 2026-12-01.
- `gemini-2.5-pro` routes to `gemini-3.8-flash` from 2026-12-01.

---

## Pricing endpoints (checked 2026-11-24)

### Kalshi (https://docs.kalshi.com)
- **Batch order books:** `GET /markets/orderbooks?tickers=A&tickers=B` takes up to 100 tickers (docs: api-reference/market/get-multiple-market-orderbooks). It returns `{"orderbooks":[{"ticker","orderbook_fp":{"yes_dollars":[[price,size]],"no_dollars":[...]}}]}`.
  - **Bids only:** a YES ask equals 1 minus the best NO bid (docs: get-market-orderbook).
  - **Sort:** ascending, best last.
  - **Observed:** a live call returns 200 without auth, with prices as 4-decimal dollar strings.
- **Series fees:** `GET /series/{ticker}` returns `fee_type` (`quadratic`, `quadratic_with_maker_fees`, ...) and `fee_multiplier` (observed: KXNBAGAME 1, KXFED 1, KXCITRINI **0**). The per-value meanings of `fee_type` are not stated.
- **Fee coefficient:** the base fee coefficient is **UNVERIFIED**. kalshi.com/docs/kalshi-fee-schedule.pdf returns 429 to automated clients. The fee rounding rule is verified: "rounded up to the nearest $0.000001" (getting_started/fee_rounding).
- **Settlement:** `GET /markets/{ticker}` returns `status` (`finalized`), `result` (`yes`/`no`/`scalar`/''), and `settlement_value_dollars` ("settlement value of the YES/LONG side ... Only filled after determination").

### Polymarket (https://docs.polymarket.com)
- **Order book:** `GET https://clob.polymarket.com/book?token_id=` needs no auth (api-reference/market-data/get-order-book). It returns `bids`/`asks` as `{price,size}` strings.
  - **Sort conflict:** the OpenAPI spec says bids are descending; the prices page says bids ascending and asks descending with the best price last. **Observed:** bids ascending, asks descending, best last. The code takes the min or max, so order doesn't matter.
  - **Missing book:** 404 "No orderbook exists" for tokens without one.
- **Batch:** `POST /books` (500 items) exists but is not used (D-026).
- **YES/NO tokens:** outcome index 0 = YES, 1 = NO, matching the `clobTokenIds` order (market-data/market-details).
- **Fees:** `fee = C x feeRate x p x (1-p)`, takers only, rounded to 5 decimal places (trading/fees). Market fields: `feesEnabled` and `feeSchedule.rate`. The meaning of `takerBaseFee` is not stated, so it isn't used.
- **Resolution:** `GET https://data-api.polymarket.com/v2/resolutions?condition=<conditionId>` returns `status` (`posed` ... `resolved`) and `payouts` ("micro-USDC per share, [outcome0, outcome1]").
- **Rate limits:** `/book` 1,500 req / 10s, IP-based, throttled rather than rejected (api-reference/rate-limits).
