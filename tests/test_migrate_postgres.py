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
