from pathlib import Path
from uuid import uuid4

from news_summary.models import PressRelease
from news_summary.storage import INDEX_STATEMENTS, POSTGRES_SCHEMA_INIT_LOCK_ID, Store


def _index_name(statement: str) -> str:
    marker = "CREATE INDEX IF NOT EXISTS "
    assert statement.startswith(marker)
    return statement.removeprefix(marker).split(" ", 1)[0]


def test_add_press_release_is_idempotent_on_duplicate_url():
    db_path = Path(f"data/.test_add_pr_dup_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()

    url = "https://www.boseong.go.kr/www/open_administration/city_news/press_release?idx=1158850&mode=view"

    def make(title: str, content: str) -> PressRelease:
        return PressRelease(
            source_id="boseong",
            source_name="보성군",
            region="전남",
            title=title,
            url=url,
            content=content,
            published_at="2026-05-20",
        )

    first = store.add_press_release(make("첫 제목", "첫 번째 본문 내용입니다."))
    assert first is not None

    # 같은 url을 다시 넣어도 유니크 제약 위반 없이 None(새 추가 아님)을 반환하고 내용은 갱신된다.
    second = store.add_press_release(make("갱신 제목", "갱신된 본문 내용입니다."))
    assert second is None

    rows = store.press_releases(limit=10)
    matching = [row for row in rows if row["url"] == url]
    assert len(matching) == 1
    assert matching[0]["title"] == "갱신 제목"


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


def test_operation_events_are_recorded_newest_first():
    db_path = Path(f"data/.test_operation_events_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()

    store.record_operation_event(
        "backup_created",
        actor="admin",
        masked_ip="10.0.*.*",
        target="older.zip",
        detail="이전 백업",
        created_at="2026-07-10T00:00:00+00:00",
    )
    store.record_operation_event(
        "manual_recrawl_requested",
        actor="admin",
        masked_ip="10.0.*.*",
        target="manual_recrawl",
        detail="collect_limit=30",
        created_at="2026-07-10T01:00:00+00:00",
    )

    rows = store.operation_events(limit=2)

    assert [row["event_type"] for row in rows] == ["manual_recrawl_requested", "backup_created"]
    assert rows[0]["detail"] == "collect_limit=30"


def test_prune_operation_events_removes_old_rows_only():
    db_path = Path(f"data/.test_operation_events_prune_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()

    store.record_operation_event("backup_created", created_at="2026-01-01T00:00:00+00:00")
    store.record_operation_event("backup_restored", created_at="2026-07-10T00:00:00+00:00")

    deleted = store.prune_operation_events("2026-07-01T00:00:00+00:00")
    rows = store.operation_events(limit=10)

    assert deleted == 1
    assert [row["event_type"] for row in rows] == ["backup_restored"]


def test_postgres_schema_init_uses_transaction_advisory_lock():
    store = Store("postgresql://user:password@example.com/neondb")
    calls = []

    class FakeConnection:
        def execute(self, sql, params=()):
            calls.append((sql, params))

    store._acquire_schema_init_lock(FakeConnection())

    assert calls == [("SELECT pg_advisory_xact_lock(?)", (POSTGRES_SCHEMA_INIT_LOCK_ID,))]
