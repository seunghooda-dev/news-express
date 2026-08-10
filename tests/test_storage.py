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


def test_recent_source_run_statuses_keeps_enough_history_to_rank_outages():
    """연속 실패 횟수는 판정(문턱 3)뿐 아니라 **화면 문구와 복구 우선순위**에도 쓰인다.

    상한이 낮으면 3일 죽은 소스와 10시간 죽은 소스가 같은 값으로 붙어, 27곳 중
    어디를 먼저 고칠지가 흐려진다(2026-08-11 지적). 매시 수집이므로 기본값 50은
    이틀치다.
    """
    from news_summary.web import _consecutive_failure_counts

    db_path = Path(f"data/.test_recent_runs_depth_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()

    for index in range(30):
        store.record_source_collection_status("damyang", "담양군청", "failed", f"실패 {index}")
    for index in range(12):
        store.record_source_collection_status("gangjin", "강진군청", "failed", f"실패 {index}")

    counts = _consecutive_failure_counts(store.recent_source_run_statuses())

    assert counts["damyang"] == 30, f"오래 죽은 소스가 {counts['damyang']}회로 잘렸다"
    assert counts["gangjin"] == 12
    assert counts["damyang"] > counts["gangjin"], "더 오래 죽은 소스를 구분하지 못한다"


def test_opening_a_database_made_before_copy_model_adds_the_column():
    """`_ensure_column` 마이그레이션은 호출이 11곳인데 테스트가 **0건**이었다.

    모든 테스트가 DB를 새로 만들고 즉시 `init_db()`를 부르므로 `ALTER TABLE` 분기가
    한 번도 실행된 적이 없다. 이주가 안 돌면 `row["copy_model"]`을 읽는 공개
    `/card-news`와 관리 화면이 **동시에 500**이 된다(2026-08-10 추가한 컬럼).
    """
    import sqlite3

    db_path = Path(f"data/.test_oldschema_{uuid4().hex}.sqlite").resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # copy_model이 없던 시절의 스키마를 손으로 만든다.
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE card_news_sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            draft_id INTEGER NOT NULL,
            press_release_id INTEGER NOT NULL,
            publish_date TEXT NOT NULL,
            cover TEXT NOT NULL,
            cards TEXT NOT NULL,
            tags TEXT NOT NULL DEFAULT '',
            source_label TEXT NOT NULL DEFAULT '',
            image_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'draft',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            published_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "INSERT INTO card_news_sets (draft_id, press_release_id, publish_date, cover, cards,"
        " created_at, updated_at) VALUES (1, 1, '2026-08-09', '옛 표지', '[]', '', '')"
    )
    conn.commit()
    columns_before = {row[1] for row in conn.execute("PRAGMA table_info(card_news_sets)")}
    conn.close()
    assert "copy_model" not in columns_before, "표본이 구스키마가 아니다"

    store = Store(db_path)
    store.init_db()

    row = store.card_news_set(1)
    assert row is not None, "옛 행이 사라졌다"
    assert row["copy_model"] == "", "이주는 됐는데 옛 행의 값이 NULL이다"
    # 그 값을 읽는 쪽이 실제로 살아 있는지까지 본다.
    from news_summary.cardnews_service import decode_cards

    assert decode_cards(row).model == ""


def test_every_ensure_column_branch_actually_restores_its_column():
    """`_ensure_column`의 `ALTER TABLE` 분기 11곳이 **한 번도 실행된 적이 없었다.**

    모든 테스트가 DB를 새로 만들고 즉시 `init_db()`를 부르므로 컬럼이 이미 다 있다.
    이주가 안 돌면 그 칸을 읽는 화면이 500이 되는데, 그걸 지키는 것이 없었다.

    각 칸마다 **그 칸만 뺀 스키마**로 테이블을 만들고 `init_db()`를 태워 되살아나는지
    본다 — 개별 컬럼을 손으로 세지 않으므로 이주 목록이 늘어나면 자동으로 덮인다.

    **무엇을 잡고 무엇을 못 잡는지 분명히 해 둔다**(2026-08-11 실증).

    - 잡는다: 이주가 실제로 컬럼을 못 붙이는 경우(ALTER 문법·타입 오류 등)
    - 잡는다: 이주 줄이 사라지는 경우 — 아래 개수 가드가 운다
    - **못 잡는다**: `SCHEMA`에 칸을 새로 넣으면서 `_ensure_column`을 **안 쓴** 경우.
      그러면 이 목록이 안 늘어나 조용히 지나간다. 그 방향은
      `test_opening_a_database_made_before_copy_model_adds_the_column`처럼
      칸마다 구스키마 표본을 따로 만들어야 잡힌다.

    (처음에 "이주 목록의 칸이 새 DB에도 있는가"로 썼다가 버렸다. `init_db()`가 CREATE와
    ALTER를 둘 다 돌려서 SCHEMA에서 빼도 이주가 메워 준다 — 아무것도 못 잡는다.)
    """
    import re
    import sqlite3

    from news_summary import storage as storage_module

    source = Path(storage_module.__file__).read_text(encoding="utf-8")
    migrated = re.findall(r'_ensure_column\(\s*conn,\s*"([^"]+)",\s*"([^"]+)"', source)
    assert len(migrated) >= 11, f"이주 목록을 못 읽었다({len(migrated)}건) — 정규식이 깨졌다"

    def create_sql_without(table: str, column: str) -> str:
        match = re.search(
            rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", storage_module.SCHEMA, re.S
        )
        assert match, f"{table} 스키마를 못 찾았다"
        kept = [
            line
            for line in match.group(1).splitlines()
            if not re.match(rf"\s*{column}\s", line)
        ]
        # 뺀 줄이 마지막이었으면 앞 줄의 쉼표를 지워야 문법이 산다.
        body = "\n".join(kept).rstrip().rstrip(",")
        return f"CREATE TABLE {table} ({body}\n)"

    for table, column in migrated:
        db_path = Path(f"data/.test_alter_{table}_{column}_{uuid4().hex}.sqlite").resolve()
        conn = sqlite3.connect(db_path)
        conn.execute(create_sql_without(table, column))
        conn.commit()
        before = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        conn.close()
        assert column not in before, f"{table}.{column}을 빼지 못했다 — 표본이 틀렸다"

        store = Store(db_path)
        store.init_db()

        with store.connect() as check:
            after = {row["name"] for row in check.execute(f"PRAGMA table_info({table})").fetchall()}
        assert column in after, f"{table}.{column} 이주가 안 돌았다 — 기존 DB가 그 칸을 영영 못 받는다"
