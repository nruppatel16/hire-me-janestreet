# Decisions

A running log. Each entry records the decision, the alternatives, and the reason.

---

### D-001: Vectors in Postgres with pgvector, behind an interface (2026-11-24)
- **Decision:** store embeddings in Postgres with `pgvector`. Code reaches vectors only through a small `VectorStore` interface (added in M2).
- **Alternatives:** Pinecone, as in the reference build.
- **Reason:** about 90k vectors is small. pgvector removes a service, keeps vectors in the same transaction and backup as markets, and avoids a second API key. The interface keeps a later switch to Pinecone cheap. Caveat for M2: `text-embedding-3-large` is 3072-dimensional, and the HNSW index on the `vector` type supports at most 2000 dimensions. M2 must use `halfvec`, reduce dimensions, or use exact search.

### D-002: Local Postgres cluster instead of Docker (2026-11-24)
- **Decision:** run a project-local cluster (`scripts/db.sh`, data in `.pgdata/`, port 5433) using the Homebrew `postgresql@15` binaries.
- **Alternatives:** docker compose, as first planned; the Homebrew launchd service.
- **Reason:** the user asked for no Docker. A project-local cluster doesn't touch the system service, and `rm -rf .pgdata` resets it. pgvector isn't installed for pg15 yet, so the `vector` extension migration is deferred to M2.

### D-003: Plain SQL migrations with a tiny runner (2026-11-24)
- **Decision:** numbered `.sql` files in `migrations/`, applied in order by `xmatch migrate`, with each file in a transaction and recorded in `schema_migrations`.
- **Alternatives:** Alembic, or yoyo-migrations.
- **Reason:** there is no ORM, so the schema is easiest to read as plain SQL. Forty lines of runner is less than either tool's configuration.

### D-004: Kalshi volume gate uses notional dollars (2026-11-24)
- **Decision:** `volume_usd = volume_fp × notional_value_dollars`, which is contracts × $1 for binary markets today. The raw value and unit are stored alongside it.
- **Alternatives:** gate on the contract count directly; contracts × average traded price (needs trade history the listing doesn't provide).
- **Reason:** the PRD gate is in dollars, and Kalshi reports only contracts. Notional is the dollar value at stake and is computable from the listing. Approved by the user.

### D-005: Exclude Kalshi multivariate (combo) markets (2026-11-24)
- **Decision:** set `mve_filter=exclude` on GET /markets.
- **Alternatives:** ingest them and gate them later.
- **Reason:** combo markets are parlays of several legs and cannot equal a single Polymarket market. Including them would swamp the store. Approved by the user.

### D-006: Gate 1 count reference is an independent second enumeration (2026-11-24)
- **Decision:** compare stored counts against a second listing path. For Kalshi, that is open markets nested in GET /events (combo markets excluded by that endpoint). For Polymarket, it is open (`closed=false`) markets nested in GET /events/keyset. Yash's figures (~44,400 and ~45,000) are reported for context only.
- **Alternatives:** scrape the website's counts; trust the primary listing's own length.
- **Reason:** neither API exposes a total count. A second endpoint is the closest thing to "the platform's own listing" that is reproducible. Approved by the user.

### D-007: Raw payload retention (2026-11-24)
- **Decision:** store one raw payload row per market per run. Prune payloads from runs older than the last N (config, default 5), except any payload a `markets` row still points to.
- **Alternatives:** keep everything; keep payloads only when the payload changes.
- **Reason:** Kalshi payloads include prices, so nearly every market changes every run. Keeping everything grows by about 90k rows per run. The PRD only needs every normalized row to be traceable, which the pointer guarantees. Approved by the user.

### D-008: Kalshi title is event title plus market title (2026-11-24)
- **Decision:** store `event_title` from GET /events. The normalized `title` used for hashing (and later embedding) is `event_title — market title` when the two differ.
- **Alternatives:** the market title alone; `yes_sub_title`.
- **Reason:** market titles often omit the event context ("Will over 6.5 goals be scored?"). Approved by the user.

### D-009: Lifecycle in M1 is a `listed` flag only (2026-11-24)
- **Decision:** after a successful full run, markets absent from that run get `listed = false`. Their status is left as is. Fetching closed or settled status is deferred to a later lifecycle pass.
- **Alternatives:** infer `closed` from absence.
- **Reason:** absence doesn't say whether a market closed, settled, or was voided, and guessing would corrupt the history used for evals. Approved by the user.

### D-010: Atomicity is per page (2026-11-24)
- **Decision:** raw payloads and market upserts for one page commit in one transaction. The `ingest_runs` row and error log commit separately, so they survive a failure. The `listed` sweep and payload pruning run only after a successful run.
- **Alternatives:** the whole run in one transaction.
- **Reason:** a single transaction would also roll back the raw payloads and error context needed to debug a failure. Per-page commits never leave a half-written row. Because upserts are idempotent, a failed run is fully repaired by the next successful one.

### D-011: Kalshi raw payload is a market+event envelope (2026-11-24)
- **Decision:** each Kalshi raw payload row stores `{"market": <untouched market>, "event": <untouched event, nested markets removed>}`.
- **Alternatives:** store only the market object; store events in a separate table.
- **Reason:** the normalized title depends on the event (D-008). A market-only payload could not reproduce its row, which breaks DP-6 traceability. The envelope keeps both objects unmodified and lets Gate 1 replay a run exactly.

### D-012: Gate 1 proves idempotency by replay, and reports the live delta (2026-11-24)
- **Decision:** the replay check re-ingests the stored raw payloads of the latest run, and the markets fingerprint must be byte-identical. The count delta between two consecutive live runs is reported, along with how many new markets explain it.
- **Alternatives:** require two live runs to give exactly equal counts.
- **Reason:** both platforms list new markets every few minutes, so two live runs can't be expected to match exactly. Replay isolates the pipeline's own idempotency from market churn.

### D-013: Polymarket "open" means `closed=false`, including markets past `endDate` (2026-11-24)
- **Decision:** keep every `closed=false` market with status `open`, even when `endDate` has passed. In the first run, about 32k of 124k sampled markets were past their end date and awaiting resolution.
- **Alternatives:** treat past-`endDate` markets as closed during normalization.
- **Reason:** it matches the platform's own definition and the Gate 1 reference count.
- **Update 2026-11-24 (user decision):** no end-date filter downstream either. The user said "we will be executing in the present so end date shouldn't matter". A market counts while the platform lists it as open.

### D-014: Polymarket reference count requires `active=true` (2026-11-24)
- **Decision:** in the Gate 1 reference enumeration, a Polymarket market counts as open when `closed == false AND active == true`.
- **Alternatives:** `closed == false` only (gap 14.90%); ingest the inactive markets through the events endpoint.
- **Reason:** open events nest about 26.7k inactive placeholder outcomes (for example "Person AC") and archived markets. The markets listing never returns them, and they can't be traded, so they can never be an arbitrage. With matching definitions the two listings agree within 0.06%. Chosen by the user (option A).

### D-015: Vectors as float4[] in Postgres with exact numpy search; supersedes D-001 (2026-11-24)
- **Decision:** embeddings live in `embeddings.vector real[]`. Top-K uses exact cosine search in numpy (block matrix multiply), behind the `VectorStore` interface in `src/xmatch/ai/vectors.py`.
- **Alternatives:** pgvector (D-001); Pinecone.
- **Reason:** Homebrew's pgvector only builds for Postgres 18 and 19, and the local cluster is 15. Only about 14k markets pass the gate, so exact search is milliseconds and has no recall loss from approximate indexes. Moving to pgvector or Pinecone means writing one more `VectorStore`. Chosen with the user's go-ahead.

### D-016: Gemini as the AI provider (2026-11-24)
- **Decision:** embeddings use `gemini-embedding-2` at 768 dimensions. The model, dimensions, provider and price are all config values. The key is read from `XMATCH_GEMINI_API_KEY`.
- **Alternatives:** OpenAI `text-embedding-3-large`, as in the PRD and the reference build.
- **Reason:**
  - The user chose Gemini and supplied the key.
  - 768 dimensions come back already normalized (observed), and they keep vectors small.
  - The variable name is project-specific because the user's shell exports a different `GEMINI_API_KEY`, which was shadowing `.env`.

### D-017: Embedded text is title plus short description, not the rules (2026-11-24)
- **Decision:** text recipe v1 is the title plus the description (Kalshi: event subtitle and `yes_sub_title`; Polymarket: `groupItemTitle`). Every embedding row stores the exact `input_text`, and bumping `text_version` forces a re-embed.
- **Alternatives:** include the full resolution rules.
- **Reason:** this follows PRD section 6. Rules are long, boilerplate-heavy and template-similar across unrelated markets, which would inflate similarity. Rules go to the M3 classifier instead.

### D-018: Gate 2 criteria (2026-11-24)
- **Decision:** Gate 2 passes when:
  - every eligible market is embedded;
  - re-running embed makes 0 provider calls;
  - the candidate set is current;
  - recall is at least 95% on at least 10 dev must-confirm pairs at the configured K and floor.
  The thresholds are config TARGETs.
- **Alternatives:** the PRD's milestone table, which didn't survive the markdown export.
- **Reason:** these follow from the PRD's stated intent: tune the floor from data, use the cache, keep the model as config. Approved by the user.

### D-019: Free-tier Gemini pacing (2026-11-24)
- **Decision:** the Gemini client paces itself to 0.016 requests/s, which is one batch of 100 texts every 62 seconds. It honors Google's `RetryInfo.retryDelay` on 429.
- **Alternatives:** keep 2 req/s and rely on retries.
- **Reason:** observed limit for this key: about 100 embedded texts per minute. The first full embed of about 13.6k markets therefore takes about 2.3 hours once. After that the content-hash cache means only new or changed markets are embedded. Raise the rate if billing is enabled.

### D-020: Milestone cadence and paper trading (2026-11-24)
- **Decision:** build M2 → M3 → M4 back to back, stopping at each gate for review. Paper trading arrives in M4 as a simulated ledger: both legs at the ask, size limited by book depth, net of fees, held to resolution. No order code exists anywhere.
- **Alternatives:** a separate plan approval for each milestone.
- **Reason:** the user wants to test with paper money soon. Gates still protect correctness, especially M3's zero-false-positive gate.

### D-021: Share the dashboard as a static snapshot on Vercel's free tier (2026-11-24)
- **Decision:**
  - `xmatch publish` exports a snapshot (`xmatch export` writes `site/`) and runs `vercel deploy site --prod`.
  - The same `index.html` runs in static mode, reading `data/*.json`.
  - The snapshot covers the funnel, volume, hold-outs, runs, error messages, the top 3,000 candidate pairs, and the eligible plus held-out markets with their rules, about 30 MB.
- **Left out on purpose:**
  - error tracebacks, which contain local paths;
  - raw API payloads (about 55 MB);
  - the roughly 300k long-tail markets below the gate.
- **Alternatives:**
  - Tailscale to the live local dashboard;
  - a hosted Postgres behind Vercel functions;
  - each collaborator running their own copy.
- **Reason:**
  - The user asked for Vercel's free tier so a collaborator can view the data.
  - Vercel can't reach the local database, and the free hosted databases are smaller than the store (about 330k markets plus payloads).
  - A static snapshot exposes no database and no secrets and costs nothing. It's as fresh as the last `publish`.
- **Caveats:**
  - This deliberately departs from the Phase 1 "no cloud deploy" rule, at the user's request.
  - On the Hobby plan, production URLs are public; password protection is a paid feature. The data is public market data.
