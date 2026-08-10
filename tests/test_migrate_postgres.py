from news_summary.migrate_postgres import SEQUENCE_TABLES, TABLES, _reset_sequences, _truncate_sql


def test_migration_includes_operation_events_table():
    assert "operation_events" in TABLES
    assert "operation_events" in SEQUENCE_TABLES
    assert "app_metadata" not in SEQUENCE_TABLES

    truncate_sql = _truncate_sql()

    assert "operation_events" in truncate_sql
    assert "RESTART IDENTITY CASCADE" in truncate_sql


def test_reset_sequences_covers_operation_events():
    class FakeConnection:
        def __init__(self):
            self.statements = []

        def execute(self, sql):
            self.statements.append(sql)

    conn = FakeConnection()

    _reset_sequences(conn)

    joined = "\n".join(conn.statements)
    assert len(conn.statements) == len(SEQUENCE_TABLES)
    assert "operation_events" in joined
    assert "app_metadata" not in joined


def test_migration_covers_every_table_the_backup_carries():
    """두 목록이 어긋나면 이관에서 그 테이블이 **오류 없이** 사라진다.

    실제로 `card_news_sets`가 백업에는 있고 이관에는 없었다(2026-08-10 감사).
    개별 테이블 이름을 세는 대신 두 목록을 맞물려 둬서, 앞으로 어느 쪽에
    테이블이 늘어도 반대쪽을 안 고치면 여기서 먼저 실패하게 한다.
    """
    from news_summary.backup import POSTGRES_BACKUP_TABLES

    assert set(TABLES) == set(POSTGRES_BACKUP_TABLES), (
        "백업과 이관이 담는 테이블이 다릅니다 — "
        f"이관에만: {sorted(set(TABLES) - set(POSTGRES_BACKUP_TABLES))}, "
        f"백업에만: {sorted(set(POSTGRES_BACKUP_TABLES) - set(TABLES))}"
    )


def test_card_news_survives_migration_and_replace():
    """카드뉴스는 초안·원문을 draft_id로 가리키는데 FK가 없다.

    `--replace`가 나머지 테이블만 RESTART IDENTITY로 번호를 다시 매기면
    남은 카드뉴스가 **다른 기사를 가리킨다.** 함께 비워지고 함께 실려야 한다.
    """
    assert "card_news_sets" in TABLES
    assert "card_news_sets" in SEQUENCE_TABLES
    assert "card_news_sets" in _truncate_sql()
    # 참조하는 쪽이 뒤에 와야 넣는 순서가 성립한다.
    assert TABLES.index("card_news_sets") > TABLES.index("article_drafts")
