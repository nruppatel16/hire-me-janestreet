-- Normalized markets from both platforms (PRD DP-2, section 10).
CREATE TABLE markets (
    id                  bigserial PRIMARY KEY,
    platform            text        NOT NULL CHECK (platform IN ('kalshi', 'polymarket')),
    native_id           text        NOT NULL,
    event_id            text,
    event_title         text,
    title               text,
    description         text,
    resolution_text     text,
    close_time_utc      timestamptz,
    source_tz           text        NOT NULL,
    outcome_labels      jsonb       NOT NULL DEFAULT '[]'::jsonb,
    volume_raw          numeric,
    volume_unit         text        NOT NULL CHECK (volume_unit IN ('contracts', 'usd')),
    volume_usd          numeric,
    status              text        NOT NULL CHECK (status IN ('open', 'closed', 'resolved', 'unknown')),
    source_status       text,                   -- platform's own status string
    passes_volume_gate  boolean     NOT NULL DEFAULT false,
    matchable           boolean     NOT NULL DEFAULT false,   -- false = held out of matching
    holdout_reason      text,
    content_hash        text        NOT NULL,
    hash_version        integer     NOT NULL,
    needs_embedding     boolean     NOT NULL DEFAULT true,
    listed              boolean     NOT NULL DEFAULT true,    -- present in the latest successful run
    first_seen_run      bigint      NOT NULL REFERENCES ingest_runs (run_id),
    last_seen_run       bigint      NOT NULL REFERENCES ingest_runs (run_id),
    raw_payload_id      bigint      NOT NULL REFERENCES raw_payloads (id),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT markets_platform_native_id UNIQUE (platform, native_id),
    -- PRD DP-2: no null title / resolution text / close time on rows eligible for matching.
    CONSTRAINT matchable_rows_complete CHECK (
        NOT matchable OR (
            title IS NOT NULL AND btrim(title) <> '' AND
            resolution_text IS NOT NULL AND btrim(resolution_text) <> '' AND
            close_time_utc IS NOT NULL
        )
    ),
    CONSTRAINT holdout_has_reason CHECK (matchable OR holdout_reason IS NOT NULL)
);
CREATE INDEX markets_live ON markets (platform) WHERE listed AND status = 'open' AND matchable AND passes_volume_gate;
CREATE INDEX markets_needs_embedding ON markets (platform) WHERE needs_embedding;
