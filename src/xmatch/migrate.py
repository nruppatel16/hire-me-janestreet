"""Apply numbered SQL migrations in order. Each file runs in its own transaction."""

from __future__ import annotations

from pathlib import Path

from xmatch.config import ROOT
from xmatch.db import connect

MIGRATIONS_DIR = ROOT / "migrations"


def pending(conn, migrations_dir: Path = MIGRATIONS_DIR) -> list[Path]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    conn.commit()
    done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
    return [p for p in sorted(migrations_dir.glob("*.sql")) if p.name not in done]


def migrate(dsn: str, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    applied = []
    with connect(dsn) as conn:
        for path in pending(conn, migrations_dir):
            with conn.transaction():
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
            applied.append(path.name)
    return applied
