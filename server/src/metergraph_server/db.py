import os
from pathlib import Path

from psycopg_pool import ConnectionPool

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_pool: ConnectionPool | None = None


def dsn() -> str:
    return os.environ.get(
        "DATABASE_URL", "postgresql://metergraph:metergraph@localhost:5432/metergraph"
    )


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(dsn(), min_size=1, max_size=8, open=True)
    return _pool


def close() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def migrate() -> None:
    with pool().connection() as con:
        con.execute(
            "create table if not exists schema_migrations ("
            " name text primary key, applied_at timestamptz not null default now())"
        )
        applied = {
            row[0] for row in con.execute("select name from schema_migrations")
        }
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            con.execute(path.read_text())
            con.execute(
                "insert into schema_migrations (name) values (%s)", (path.name,)
            )
