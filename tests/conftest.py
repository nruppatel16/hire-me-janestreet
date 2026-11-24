import os

import psycopg
import pytest
from dotenv import load_dotenv

from xmatch.config import ROOT, load_settings

load_dotenv(ROOT / ".env", override=False)


@pytest.fixture
def test_dsn():
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL not set")
    try:
        psycopg.connect(dsn, connect_timeout=2).close()
    except psycopg.OperationalError:
        pytest.skip("local Postgres not running (scripts/db.sh start)")
    return dsn


@pytest.fixture
def clean_db(test_dsn):
    """Fresh schema for each test: drop everything, then migrate."""
    from xmatch.migrate import migrate

    with psycopg.connect(test_dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    migrate(test_dsn)
    return test_dsn


@pytest.fixture
def cfg(clean_db):
    return load_settings(database_url=clean_db)
