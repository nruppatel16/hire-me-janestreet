-- Untouched API responses, one row per market per run (PRD DP-6). Pruned per D-007.
CREATE TABLE raw_payloads (
    id            bigserial PRIMARY KEY,
    run_id        bigint      NOT NULL REFERENCES ingest_runs (run_id),
    platform      text        NOT NULL,
    native_id     text        NOT NULL,
    payload_json  jsonb       NOT NULL,
    fetched_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX raw_payloads_run ON raw_payloads (run_id);
CREATE INDEX raw_payloads_market ON raw_payloads (platform, native_id);
