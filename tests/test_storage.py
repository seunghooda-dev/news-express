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


def test_postgres_connection_enables_keepalives_and_connect_timeout(monkeypatch):
    """스레드마다 연결을 오래 붙들기 때문에 유휴 중 끊기면 OperationalError가 난다.

    TCP keepalive로 끊김을 줄이고, 연결 수립도 무한정 기다리지 않게 한다.
    """
    from news_summary import storage

    store = Store("postgresql://user:password@example.com/postgres")
    captured: dict[str, object] = {}

    class FakePsycopg:
        @staticmethod
        def connect(dsn, **kwargs):
            captured["dsn"] = dsn
            captured.update(kwargs)
            return object()

    monkeypatch.setattr(storage, "psycopg", FakePsycopg)

    store._new_connection()

    assert captured["keepalives"] == 1
    assert captured["keepalives_idle"] == 30
    assert captured["connect_timeout"] == storage.POSTGRES_CONNECT_TIMEOUT_SECONDS


def test_postgres_schema_init_uses_transaction_advisory_lock():
    store = Store("postgresql://user:password@example.com/postgres")
    calls = []

    class FakeConnection:
        def execute(self, sql, params=()):
            calls.append((sql, params))

    store._acquire_schema_init_lock(FakeConnection())

    assert calls == [("SELECT pg_advisory_xact_lock(?)", (POSTGRES_SCHEMA_INIT_LOCK_ID,))]


def test_recent_source_run_statuses_is_bounded_per_source():
    """연속 실패 판정에 필요한 만큼만 가져온다 — 전량 스캔이 전송량을 먹고 있었다.

    `source_collection_runs`는 활성 27개 × 매시라 하루 650건 넘게 늘고
    **지우는 코드가 없다.** 그런데 네 곳이 각자 WHERE도 LIMIT도 없이 전량을
    끌어왔고, 그중 하나는 공개 첫 화면이 매 요청마다 지난다(2026-08-11 감사).
    """
    db_path = Path(f"data/.test_recent_runs_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()

    for index in range(40):
        store.record_source_collection_status("damyang", "담양군청", "ok", f"{index}회차")
    for index in range(40):
        store.record_source_collection_status("gangjin", "강진군청", "ok", f"{index}회차")

    rows = store.recent_source_run_statuses(per_source=10)

    assert len(rows) == 20, f"소스별 10건씩이어야 하는데 {len(rows)}건을 가져왔다"
    assert {str(row["source_id"]) for row in rows} == {"damyang", "gangjin"}


def test_recent_source_run_statuses_keeps_consecutive_failure_detection():
    """경계값 확인 — 문턱이 3회이므로 최근 실패가 그 안에서 다 보여야 한다."""
    from news_summary.web import _consecutive_failure_counts

    db_path = Path(f"data/.test_recent_runs_fail_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()

    # 옛 성공이 잔뜩 쌓인 뒤 최근에 연속 실패한 소스.
    for index in range(30):
        store.record_source_collection_status("damyang", "담양군청", "ok", f"성공 {index}")
    for index in range(4):
        store.record_source_collection_status("damyang", "담양군청", "failed", f"실패 {index}")
    # 대조군 — 실패 뒤에 성공했으므로 연속 실패가 아니다.
    store.record_source_collection_status("gangjin", "강진군청", "failed", "실패")
    store.record_source_collection_status("gangjin", "강진군청", "ok", "성공")

    counts = _consecutive_failure_counts(store.recent_source_run_statuses())

    assert counts.get("damyang") == 4, "최근 연속 실패를 놓쳤다"
    assert "gangjin" not in counts, "성공으로 끊긴 소스를 연속 실패로 셌다"
