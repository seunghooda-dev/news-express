from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .storage import Store


# 백업이 담는 테이블과 **같아야 한다.** 어긋나면 이관에서 그 테이블이 조용히
# 사라진다 — `card_news_sets`가 실제로 빠져 있었다(2026-08-10 감사에서 적발).
# `--replace`는 더 나쁘다: 나머지가 RESTART IDENTITY로 번호를 다시 받는데 빠진
# 테이블만 옛 draft_id를 들고 남아 **다른 기사를 가리키게 된다**(FK가 없다).
# 아래 순서는 참조하는 쪽이 뒤에 오게 둔다(카드뉴스가 초안·원문을 가리킨다).
TABLES = (
    "press_releases",
    "press_release_assets",
    "article_drafts",
    "app_metadata",
    "draft_history",
    "draft_generation_failures",
    "source_collection_runs",
    "visitor_access_logs",
    "operation_events",
    "card_news_sets",
)

SEQUENCE_TABLES = tuple(table for table in TABLES if table != "app_metadata")


def migrate_sqlite_to_postgres(sqlite_db: Path, database_url: str, *, replace: bool = False) -> dict[str, int]:
    if not sqlite_db.exists():
        raise FileNotFoundError(f"SQLite DB를 찾을 수 없습니다: {sqlite_db}")
    if not database_url.startswith(("postgresql://", "postgres://")):
        raise ValueError("PostgreSQL DATABASE_URL이 필요합니다.")

    target = Store(database_url)
    target.init_db()
    source = sqlite3.connect(sqlite_db)
    source.row_factory = sqlite3.Row
    try:
        with target.connect() as conn:
            if replace:
                conn.execute(_truncate_sql())
            _ensure_target_empty(conn)
            counts = {}
            for table in TABLES:
                counts[table] = _copy_table(source, conn, table)
            _reset_sequences(conn)
            return counts
    finally:
        source.close()


def _ensure_target_empty(conn: Any) -> None:
    existing = {}
    for table in TABLES:
        row = conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
        count = int(row["count"])
        if count:
            existing[table] = count
    if existing:
        detail = ", ".join(f"{table}={count}" for table, count in existing.items())
        raise RuntimeError(f"대상 PostgreSQL DB가 비어 있지 않습니다. 기존 데이터: {detail}. 덮어쓰려면 --replace를 사용하세요.")


def _copy_table(source: sqlite3.Connection, target: Any, table: str) -> int:
    rows = source.execute(f"SELECT * FROM {table} ORDER BY id" if table != "app_metadata" else f"SELECT * FROM {table}").fetchall()
    if not rows:
        return 0
    columns = rows[0].keys()
    column_sql = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})"
    values = [tuple(row[column] for column in columns) for row in rows]
    target.executemany(sql, values)
    return len(rows)


def _reset_sequences(conn: Any) -> None:
    for table in SEQUENCE_TABLES:
        conn.execute(
            f"""
            SELECT setval(
                pg_get_serial_sequence('{table}', 'id'),
                COALESCE((SELECT MAX(id) FROM {table}), 1),
                COALESCE((SELECT MAX(id) FROM {table}), 0) > 0
            )
            """
        )


def _truncate_sql() -> str:
    return f"TRUNCATE {', '.join(reversed(TABLES))} RESTART IDENTITY CASCADE"
