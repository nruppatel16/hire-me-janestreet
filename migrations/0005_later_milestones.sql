-- Tables for Milestones 2-4, created empty so the schema is visible early (PRD section 10).
-- `embeddings` is deferred to M2: its vector type/dimension depend on D-001 and the pgvector install.

CREATE TABLE candidate_pairs (
    pair_id            bigserial PRIMARY KEY,
    kalshi_id          bigint  NOT NULL REFERENCES markets (id),
    polymarket_id      bigint  NOT NULL REFERENCES markets (id),
    similarity         real    NOT NULL,
    retrieval_version  text    NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kalshi_id, polymarket_id, retrieval_version)
);

CREATE TABLE verdicts (
    id                 bigserial PRIMARY KEY,
    pair_id            bigint  NOT NULL REFERENCES candidate_pairs (pair_id),
    pair_hash          text    NOT NULL,
    prompt_version     text    NOT NULL,
    model_id           text    NOT NULL,
    verdict            text    NOT NULL CHECK (verdict IN ('equivalent', 'not_equivalent', 'uncertain')),
    direction_safe     boolean NOT NULL,
    differences        jsonb   NOT NULL DEFAULT '[]'::jsonb,
    kalshi_clause      text,
    polymarket_clause  text,
    rationale          text,
    tokens_in          integer,
    tokens_out         integer,
    cost_usd           numeric,
    latency_ms         integer,
    created_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (pair_hash, prompt_version, model_id)
);

CREATE TABLE eval_cases (
    id               bigserial PRIMARY KEY,
    pair_ref         text    NOT NULL,
    label            text    NOT NULL CHECK (label IN ('must_reject', 'must_confirm')),
    reason           text    NOT NULL,
    deciding_clause  text    NOT NULL,
    failure_mode     text,
    split            text    NOT NULL CHECK (split IN ('dev', 'holdout')),
    added_on         timestamptz NOT NULL DEFAULT now(),
    supersedes       bigint  REFERENCES eval_cases (id)   -- edits create a new row
);
-- append-only: block UPDATE and DELETE
CREATE FUNCTION eval_cases_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'eval_cases is append-only; insert a new row with supersedes'; END $$;
CREATE TRIGGER eval_cases_no_update BEFORE UPDATE OR DELETE ON eval_cases
    FOR EACH ROW EXECUTE FUNCTION eval_cases_append_only();

CREATE TABLE quotes (
    id           bigserial PRIMARY KEY,
    pair_id      bigint  NOT NULL REFERENCES candidate_pairs (pair_id),
    direction    smallint NOT NULL CHECK (direction IN (1, 2)),
    ask_yes      numeric,
    ask_no       numeric,
    depth        jsonb,
    fee_version  text,
    net_margin   numeric,
    quoted_at    timestamptz NOT NULL
);

CREATE TABLE arb_observations (
    id           bigserial PRIMARY KEY,
    pair_id      bigint  NOT NULL REFERENCES candidate_pairs (pair_id),
    direction    smallint NOT NULL CHECK (direction IN (1, 2)),
    net_margin   numeric,
    fee_version  text,
    first_seen   timestamptz NOT NULL,
    last_seen    timestamptz NOT NULL
);
