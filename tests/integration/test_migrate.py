import psycopg
import pytest

from xmatch.migrate import migrate

pytestmark = pytest.mark.integration


def test_migrations_apply_and_are_idempotent(clean_db):
    assert migrate(clean_db) == []  # already applied by fixture
    with psycopg.connect(clean_db) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'")}
    assert {"markets", "raw_payloads", "ingest_runs", "ingest_errors", "verdicts",
            "candidate_pairs", "eval_cases", "quotes", "arb_observations"} <= tables


def test_eval_cases_are_append_only(clean_db):
    with psycopg.connect(clean_db) as conn:
        conn.execute("INSERT INTO eval_cases (pair_ref,label,reason,deciding_clause,split)"
                     " VALUES ('k:p','must_reject','r','c','dev')")
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("UPDATE eval_cases SET reason='x'")
