-- markets.raw_payload_id references raw_payloads(id). Without this index, every raw_payloads DELETE (pruning, D-007)
-- runs an FK check that seq-scans markets once per deleted row: quadratic, and it hung Gate 1 for 30+ minutes.
CREATE INDEX markets_raw_payload_id ON markets (raw_payload_id);
