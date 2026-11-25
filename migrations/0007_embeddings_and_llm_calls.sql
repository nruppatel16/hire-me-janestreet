-- Milestone 2: embeddings, provider call log (AI-4), retrieval metadata, eval case structure.

-- Run log now covers non-ingest jobs (embed, retrieve) that span both platforms.
ALTER TABLE ingest_runs DROP CONSTRAINT ingest_runs_platform_check;
ALTER TABLE ingest_runs ADD CONSTRAINT ingest_runs_platform_check CHECK (platform IN ('kalshi', 'polymarket', 'all'));

-- Vectors live here as float4[] (D-015). The VectorStore interface hides this from callers.
CREATE TABLE embeddings (
    market_id     bigint      NOT NULL REFERENCES markets (id),
    model         text        NOT NULL,
    dims          integer     NOT NULL,
    text_version  integer     NOT NULL,
    content_hash  text        NOT NULL,   -- markets.content_hash at embed time: stale when they differ
    input_text    text        NOT NULL,   -- exactly what was sent to the model
    vector        real[]      NOT NULL CHECK (array_length(vector, 1) = dims),
    embedded_at   timestamptz NOT NULL DEFAULT now(),
    run_id        bigint      REFERENCES ingest_runs (run_id),
    PRIMARY KEY (market_id, model, dims, text_version)
);

-- Every provider call: embeddings now, classifier calls in M3 (PRD AI-4).
CREATE TABLE llm_calls (
    id           bigserial PRIMARY KEY,
    run_id       bigint      REFERENCES ingest_runs (run_id),
    kind         text        NOT NULL CHECK (kind IN ('embed', 'generate')),
    provider     text        NOT NULL,
    model        text        NOT NULL,
    items        integer     NOT NULL DEFAULT 1,
    tokens_in    integer,
    tokens_out   integer,
    latency_ms   integer     NOT NULL,
    cost_usd     numeric,
    price_version text,
    status       text        NOT NULL CHECK (status IN ('ok', 'error')),
    error        text,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX llm_calls_run ON llm_calls (run_id);

-- Where each side ranked the other, so recall at any K can be computed later.
ALTER TABLE candidate_pairs ADD COLUMN rank_k2p integer;  -- rank of the Polymarket market in the Kalshi market's list
ALTER TABLE candidate_pairs ADD COLUMN rank_p2k integer;  -- and the reverse
CREATE INDEX candidate_pairs_version ON candidate_pairs (retrieval_version, similarity DESC);

-- Eval cases reference markets by stable native ids (internal ids could change on a rebuild).
ALTER TABLE eval_cases ADD COLUMN kalshi_native_id text;
ALTER TABLE eval_cases ADD COLUMN polymarket_native_id text;
ALTER TABLE eval_cases ADD COLUMN labeled_by text;
