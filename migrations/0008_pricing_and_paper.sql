-- Milestone 4 (pulled forward, D-024): live pricing, arb lifetimes and a paper-trading ledger.
-- The placeholder quotes / arb_observations tables from 0005 keyed on candidate_pairs. Tradable pairs can also come
-- from hand labels, so they are rebuilt keyed on the two market ids. They were empty.
DROP TABLE quotes;
DROP TABLE arb_observations;

-- Pairs allowed to be priced: hand-confirmed now, classifier-confirmed later (UNION in M3).
-- Latest label per pair wins (eval_cases is append-only).
CREATE VIEW confirmed_pairs AS
SELECT k.id AS kalshi_id, p.id AS polymarket_id, 'human'::text AS source, e.id AS evidence_id
FROM (SELECT DISTINCT ON (kalshi_native_id, polymarket_native_id) *
      FROM eval_cases WHERE kalshi_native_id IS NOT NULL
      ORDER BY kalshi_native_id, polymarket_native_id, id DESC) e
JOIN markets k ON k.platform = 'kalshi' AND k.native_id = e.kalshi_native_id
JOIN markets p ON p.platform = 'polymarket' AND p.native_id = e.polymarket_native_id
WHERE e.label = 'must_confirm';

-- One row per pair x direction x pricing run. Direction 1 = Kalshi YES + Polymarket NO; 2 = Polymarket YES + Kalshi NO.
CREATE TABLE quotes (
    id               bigserial PRIMARY KEY,
    run_id           bigint      NOT NULL REFERENCES ingest_runs (run_id),
    kalshi_id        bigint      NOT NULL REFERENCES markets (id),
    polymarket_id    bigint      NOT NULL REFERENCES markets (id),
    direction        smallint    NOT NULL CHECK (direction IN (1, 2)),
    k_ask            numeric,     -- best ask on the Kalshi leg (dollars)
    p_ask            numeric,     -- best ask on the Polymarket leg
    top_cost         numeric,     -- k_ask + p_ask + fees for one contract at top of book
    contracts        numeric     NOT NULL DEFAULT 0,  -- size fillable while every marginal contract clears min margin
    cost_usd         numeric,     -- total cost of `contracts` including fees
    fees_usd         numeric,
    net_margin       numeric,     -- (contracts - cost) / cost : return per dollar staked, after fees
    k_levels         integer,     -- book levels consumed on each leg
    p_levels         integer,
    fee_version      text        NOT NULL,
    status           text        NOT NULL,  -- arb | no_arb | no_book | fee_unverified | direction_unverified | closing
    quoted_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX quotes_pair ON quotes (kalshi_id, polymarket_id, direction, quoted_at DESC);

-- PR-8: every observed arb window, so arb lifetime is queryable.
CREATE TABLE arb_observations (
    id               bigserial PRIMARY KEY,
    kalshi_id        bigint      NOT NULL REFERENCES markets (id),
    polymarket_id    bigint      NOT NULL REFERENCES markets (id),
    direction        smallint    NOT NULL CHECK (direction IN (1, 2)),
    first_seen       timestamptz NOT NULL,
    last_seen        timestamptz NOT NULL,
    best_net_margin  numeric     NOT NULL,
    last_net_margin  numeric     NOT NULL,
    open             boolean     NOT NULL DEFAULT true,
    fee_version      text        NOT NULL
);
CREATE UNIQUE INDEX arb_observations_one_open ON arb_observations (kalshi_id, polymarket_id, direction) WHERE open;

-- Paper ledger. Simulated only: nothing here is ever sent to a platform.
CREATE TABLE paper_positions (
    id               bigserial PRIMARY KEY,
    opened_at        timestamptz NOT NULL DEFAULT now(),
    quote_id         bigint      NOT NULL REFERENCES quotes (id),
    kalshi_id        bigint      NOT NULL REFERENCES markets (id),
    polymarket_id    bigint      NOT NULL REFERENCES markets (id),
    direction        smallint    NOT NULL CHECK (direction IN (1, 2)),
    pair_source      text        NOT NULL,          -- human | classifier
    contracts        numeric     NOT NULL CHECK (contracts > 0),
    k_cost_usd       numeric     NOT NULL,
    p_cost_usd       numeric     NOT NULL,
    fees_usd         numeric     NOT NULL,
    cost_usd         numeric     NOT NULL,           -- k + p + fees
    expected_net_margin numeric  NOT NULL,
    fee_version      text        NOT NULL,
    status           text        NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'settled', 'void')),
    k_payout_per     numeric,                         -- per-contract payout of each leg at settlement
    p_payout_per     numeric,
    payout_usd       numeric,
    pnl_usd          numeric,
    settled_at       timestamptz,
    settle_note      text
);
CREATE UNIQUE INDEX paper_one_open_per_pair ON paper_positions (kalshi_id, polymarket_id, direction) WHERE status = 'open';

ALTER TABLE ingest_runs DROP CONSTRAINT IF EXISTS ingest_runs_status_check;
ALTER TABLE ingest_runs ADD CONSTRAINT ingest_runs_status_check CHECK (status IN ('running', 'succeeded', 'failed'));
