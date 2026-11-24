-- Per-run error log (PRD DP-5). Committed independently of page transactions so it survives failures.
CREATE TABLE ingest_errors (
    id          bigserial PRIMARY KEY,
    run_id      bigint      NOT NULL REFERENCES ingest_runs (run_id),
    at          timestamptz NOT NULL DEFAULT now(),
    platform    text        NOT NULL,
    native_id   text,                       -- null for page/request-level errors
    stage       text        NOT NULL,       -- fetch | normalize | write | run
    message     text        NOT NULL,
    detail      jsonb
);
CREATE INDEX ingest_errors_run ON ingest_errors (run_id);
