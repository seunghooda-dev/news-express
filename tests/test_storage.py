from pathlib import Path
from uuid import uuid4

from news_summary.storage import INDEX_STATEMENTS, POSTGRES_SCHEMA_INIT_LOCK_ID, Store


def _index_name(statement: str) -> str:
    marker = "CREATE INDEX IF NOT EXISTS "
    assert statement.startswith(marker)
    return statement.removeprefix(marker).split(" ", 1)[0]


def test_app_metadata_cache_scope_reuses_prefetched_values():
    db_path = Path(f"data/.test_metadata_cache_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    store.set_app_metadata("sample", "before")

    with store.app_metadata_cache_scope():
        assert store.get_app_metadata("sample") == "before"
        with store.connect() as conn:
            conn.execute(
                """
                UPDATE app_metadata
                SET value = ?
                WHERE key = ?
                """,
                ("direct-update", "sample"),
            )
        assert store.get_app_metadata("sample") == "before"
        store.set_app_metadata("sample", "from-cache")
        assert store.get_app_metadata("sample") == "from-cache"

    assert store.get_app_metadata("sample") == "from-cache"


def test_init_db_creates_operational_indexes():
    db_path = Path(f"data/.test_operational_indexes_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)

    store.init_db()

    with store.connect() as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()

    created_names = {str(row["name"]) for row in rows}
    expected_names = {_index_name(statement) for statement in INDEX_STATEMENTS}
    assert expected_names <= created_names


def test_postgres_schema_init_uses_transaction_advisory_lock():
    store = Store("postgresql://user:password@example.com/neondb")
    calls = []

    class FakeConnection:
        def execute(self, sql, params=()):
            calls.append((sql, params))

    store._acquire_schema_init_lock(FakeConnection())

    assert calls == [("SELECT pg_advisory_xact_lock(?)", (POSTGRES_SCHEMA_INIT_LOCK_ID,))]
