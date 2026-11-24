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
