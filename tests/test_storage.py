from pathlib import Path
from uuid import uuid4

from news_summary.storage import POSTGRES_SCHEMA_INIT_LOCK_ID, Store


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


def test_postgres_schema_init_uses_transaction_advisory_lock():
    store = Store("postgresql://user:password@example.com/neondb")
    calls = []

    class FakeConnection:
        def execute(self, sql, params=()):
            calls.append((sql, params))

    store._acquire_schema_init_lock(FakeConnection())

    assert calls == [("SELECT pg_advisory_xact_lock(?)", (POSTGRES_SCHEMA_INIT_LOCK_ID,))]
