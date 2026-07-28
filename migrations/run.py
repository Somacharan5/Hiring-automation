"""Apply migrations/*.sql to the database in $DATABASE_URL (read from .env).

Idempotent — every migration uses IF NOT EXISTS, so re-running is safe.
Runs each statement with autocommit and prepared statements disabled, so it
works through Neon's PgBouncer pooler as well as a direct connection.

    python migrations/run.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def split_statements(sql: str) -> list[str]:
    """Split a plain-DDL migration into individual statements.

    Our migrations contain no ';' inside string literals or comment bodies, so a
    naive split on ';' is correct and keeps the runner dependency-free.
    """
    return [s.strip() for s in sql.split(";") if s.strip()]


def main() -> None:
    load_env()
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL not set — add it to .env first")

    files = sorted((ROOT / "migrations").glob("*.sql"))
    if not files:
        sys.exit("No migration files found in migrations/")

    with psycopg.connect(url, autocommit=True, prepare_threshold=None) as conn:
        for f in files:
            stmts = split_statements(f.read_text())
            for stmt in stmts:
                conn.execute(stmt)
            print(f"✔ applied {f.name} ({len(stmts)} statements)")

        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' ORDER BY table_name"
        ).fetchall()

    print("\nTables now live:", ", ".join(r[0] for r in rows) or "(none)")


if __name__ == "__main__":
    main()
