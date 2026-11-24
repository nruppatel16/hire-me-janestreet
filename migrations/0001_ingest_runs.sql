-- One row per job run (PRD DP-7).
CREATE TABLE ingest_runs (
    run_id          bigserial PRIMARY KEY,
    job             text        NOT NULL,              -- e.g. 'ingest'
    platform        text        NOT NULL CHECK (platform IN ('kalshi', 'polymarket')),
    started_at      timestamptz NOT NULL DEFAULT now(),
    ended_at        timestamptz,
    rows_read       integer     NOT NULL DEFAULT 0,    -- markets received from the API (after dedupe)
    rows_written    integer     NOT NULL DEFAULT 0,    -- markets inserted or updated
    rows_new        integer     NOT NULL DEFAULT 0,
    rows_changed    integer     NOT NULL DEFAULT 0,    -- content_hash changed
    rows_held_out   integer     NOT NULL DEFAULT 0,    -- missing title / resolution text / close time
    error_count     integer     NOT NULL DEFAULT 0,
    status          text        NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'succeeded', 'failed')),
    config_snapshot jsonb       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX ingest_runs_platform_started ON ingest_runs (platform, started_at DESC);
