from __future__ import annotations

from contextlib import contextmanager
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .collectors import _canonical_url, _normalize_published_at
from .models import ArticleDraft, PressRelease, PressReleaseAsset, Source


try:
    import psycopg
    from psycopg.rows import dict_row
except ModuleNotFoundError:  # pragma: no cover - exercised only when PostgreSQL is configured without psycopg.
    psycopg = None
    dict_row = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS press_releases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    source_name TEXT NOT NULL,
    region TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    content TEXT NOT NULL,
    published_at TEXT,
    collected_at TEXT NOT NULL,
    validation_status TEXT NOT NULL DEFAULT '검증 완료',
    validation_note TEXT NOT NULL DEFAULT '기존 수집 원문입니다.'
);

CREATE TABLE IF NOT EXISTS press_release_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    press_release_id INTEGER NOT NULL,
    asset_type TEXT NOT NULL DEFAULT 'file',
    url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    filename TEXT NOT NULL DEFAULT '',
    content_type TEXT NOT NULL DEFAULT '',
    is_image INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(press_release_id, url),
    FOREIGN KEY (press_release_id) REFERENCES press_releases(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS article_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    press_release_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    review_note TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL,
    initial_title TEXT,
    initial_body TEXT,
    initial_review_note TEXT,
    initial_model TEXT,
    status TEXT NOT NULL DEFAULT 'needs_review',
    updated_at TEXT,
    exported_at TEXT,
    FOREIGN KEY (press_release_id) REFERENCES press_releases(id)
);

CREATE TABLE IF NOT EXISTS app_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS draft_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    review_note TEXT NOT NULL,
    status TEXT NOT NULL,
    model TEXT NOT NULL,
    change_type TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    FOREIGN KEY (draft_id) REFERENCES article_drafts(id)
);

CREATE TABLE IF NOT EXISTS draft_generation_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    press_release_id INTEGER NOT NULL,
    failure_kind TEXT NOT NULL,
    message TEXT NOT NULL,
    attempted_models TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 1,
    first_failed_at TEXT NOT NULL,
    last_failed_at TEXT NOT NULL,
    next_retry_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY (press_release_id) REFERENCES press_releases(id)
);

CREATE TABLE IF NOT EXISTS source_collection_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    source_name TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL,
    failure_stage TEXT NOT NULL DEFAULT '',
    failure_reason TEXT NOT NULL DEFAULT '',
    releases_found INTEGER NOT NULL DEFAULT 0,
    inserted_count INTEGER NOT NULL DEFAULT 0,
    repaired_dates INTEGER NOT NULL DEFAULT 0,
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS visitor_access_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    masked_ip TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    status_code INTEGER NOT NULL DEFAULT 0,
    user_agent TEXT NOT NULL DEFAULT '',
    visited_at TEXT NOT NULL
);
"""

POSTGRES_CONNECTION_HEALTH_CHECK_SECONDS = 60.0
POSTGRES_SCHEMA_INIT_LOCK_ID = 907_260_718_101


POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS press_releases (
    id BIGSERIAL PRIMARY KEY,
    source_id TEXT NOT NULL,
    source_name TEXT NOT NULL,
    region TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    content TEXT NOT NULL,
    published_at TEXT,
    collected_at TEXT NOT NULL,
    validation_status TEXT NOT NULL DEFAULT '검증 완료',
    validation_note TEXT NOT NULL DEFAULT '기존 수집 원문입니다.'
);

CREATE TABLE IF NOT EXISTS press_release_assets (
    id BIGSERIAL PRIMARY KEY,
    press_release_id BIGINT NOT NULL REFERENCES press_releases(id) ON DELETE CASCADE,
    asset_type TEXT NOT NULL DEFAULT 'file',
    url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    filename TEXT NOT NULL DEFAULT '',
    content_type TEXT NOT NULL DEFAULT '',
    is_image INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(press_release_id, url)
);

CREATE TABLE IF NOT EXISTS article_drafts (
    id BIGSERIAL PRIMARY KEY,
    press_release_id BIGINT NOT NULL REFERENCES press_releases(id),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    review_note TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL,
    initial_title TEXT,
    initial_body TEXT,
    initial_review_note TEXT,
    initial_model TEXT,
    status TEXT NOT NULL DEFAULT 'needs_review',
    updated_at TEXT,
    exported_at TEXT
);

CREATE TABLE IF NOT EXISTS app_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS draft_history (
    id BIGSERIAL PRIMARY KEY,
    draft_id BIGINT NOT NULL REFERENCES article_drafts(id),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    review_note TEXT NOT NULL,
    status TEXT NOT NULL,
    model TEXT NOT NULL,
    change_type TEXT NOT NULL,
    changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS draft_generation_failures (
    id BIGSERIAL PRIMARY KEY,
    press_release_id BIGINT NOT NULL REFERENCES press_releases(id),
    failure_kind TEXT NOT NULL,
    message TEXT NOT NULL,
    attempted_models TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 1,
    first_failed_at TEXT NOT NULL,
    last_failed_at TEXT NOT NULL,
    next_retry_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS source_collection_runs (
    id BIGSERIAL PRIMARY KEY,
    source_id TEXT NOT NULL,
    source_name TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL,
    failure_stage TEXT NOT NULL DEFAULT '',
    failure_reason TEXT NOT NULL DEFAULT '',
    releases_found INTEGER NOT NULL DEFAULT 0,
    inserted_count INTEGER NOT NULL DEFAULT 0,
    repaired_dates INTEGER NOT NULL DEFAULT 0,
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS visitor_access_logs (
    id BIGSERIAL PRIMARY KEY,
    masked_ip TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    status_code INTEGER NOT NULL DEFAULT 0,
    user_agent TEXT NOT NULL DEFAULT '',
    visited_at TEXT NOT NULL
);
"""


class _PostgresCursor:
    def __init__(self, cursor: Any, lastrowid: int | None = None) -> None:
        self._cursor = cursor
        self.lastrowid = lastrowid

    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def fetchall(self) -> list[Any]:
        return self._cursor.fetchall()

    @property
    def rowcount(self) -> int:
        return int(getattr(self._cursor, "rowcount", 0) or 0)


class _PostgresConnection:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def __enter__(self) -> "_PostgresConnection":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        self._conn.close()

    def execute(self, sql: str, params: tuple[object, ...] | list[object] = ()) -> _PostgresCursor:
        translated = _postgres_sql(sql)
        cursor = self._conn.execute(translated, params)
        return _PostgresCursor(cursor)

    def executemany(self, sql: str, rows: list[tuple[object, ...]]) -> None:
        if not rows:
            return
        translated = _postgres_sql(sql)
        with self._conn.cursor() as cursor:
            cursor.executemany(translated, rows)

    def executescript(self, script: str) -> None:
        for statement in _split_sql_script(script):
            self.execute(statement)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    @property
    def closed(self) -> bool:
        return bool(getattr(self._conn, "closed", False))


class _ScopedConnection:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def __enter__(self) -> Any:
        return self._conn

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


def _split_sql_script(script: str) -> list[str]:
    return [statement.strip() for statement in script.split(";") if statement.strip()]


def _postgres_sql(sql: str) -> str:
    return sql.replace("%", "%%").replace("?", "%s")


def _redact_database_url(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.password:
        return url
    username = parsed.username or ""
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    userinfo = f"{username}:***@" if username else ""
    return urlunsplit((parsed.scheme, f"{userinfo}{host}{port}", parsed.path, parsed.query, parsed.fragment))


class Store:
    def __init__(self, path: Path | str) -> None:
        self.location = str(path)
        self.is_postgres = self.location.startswith(("postgresql://", "postgres://"))
        self.path = path if self.is_postgres else Path(path)
        self.display_location = _redact_database_url(self.location) if self.is_postgres else str(self.path)
        self._local = threading.local()
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> Any:
        scoped_connection = getattr(self._local, "connection", None)
        if scoped_connection is not None:
            return _ScopedConnection(scoped_connection)
        return self._new_connection()

    @contextmanager
    def connection_scope(self) -> Any:
        existing = getattr(self._local, "connection", None)
        if existing is not None:
            yield existing
            return

        context = self._new_connection()
        conn = context.__enter__()
        self._local.connection = conn
        try:
            yield conn
        except Exception as exc:
            context.__exit__(type(exc), exc, exc.__traceback__)
            raise
        else:
            context.__exit__(None, None, None)
        finally:
            if not self.is_postgres:
                context.close()
            if hasattr(self._local, "connection"):
                del self._local.connection

    @contextmanager
    def reusable_connection_scope(self) -> Any:
        if not self.is_postgres:
            with self.connection_scope() as conn:
                yield conn
            return

        existing = getattr(self._local, "connection", None)
        if existing is not None:
            yield existing
            return

        conn = self._persistent_postgres_connection()
        self._local.connection = conn
        try:
            yield conn
        except Exception:
            try:
                conn.rollback()
            finally:
                self._close_persistent_postgres_connection()
            raise
        else:
            try:
                conn.commit()
            except Exception:
                self._close_persistent_postgres_connection()
                raise
        finally:
            if hasattr(self._local, "connection"):
                del self._local.connection

    @contextmanager
    def app_metadata_cache_scope(self) -> Any:
        existing = getattr(self._local, "app_metadata_cache", None)
        if existing is not None:
            yield existing
            return

        with self.connect() as conn:
            rows = conn.execute("SELECT key, value FROM app_metadata").fetchall()
        cache = {str(row["key"]): str(row["value"]) for row in rows}
        self._local.app_metadata_cache = cache
        try:
            yield cache
        finally:
            if hasattr(self._local, "app_metadata_cache"):
                del self._local.app_metadata_cache

    def _persistent_postgres_connection(self) -> Any:
        conn = getattr(self._local, "persistent_connection", None)
        if conn is not None and not conn.closed:
            last_checked_at = getattr(self._local, "persistent_connection_checked_at", 0.0)
            if time.monotonic() - last_checked_at < POSTGRES_CONNECTION_HEALTH_CHECK_SECONDS:
                return conn
            if self._postgres_connection_is_usable(conn):
                self._local.persistent_connection_checked_at = time.monotonic()
                return conn

        self._close_persistent_postgres_connection()
        context = self._new_connection()
        conn = context.__enter__()
        self._local.persistent_connection = conn
        self._local.persistent_connection_checked_at = time.monotonic()
        return conn

    def _postgres_connection_is_usable(self, conn: Any) -> bool:
        try:
            conn.execute("SELECT 1").fetchone()
            conn.commit()
        except Exception:
            return False
        return True

    def _close_persistent_postgres_connection(self) -> None:
        conn = getattr(self._local, "persistent_connection", None)
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            pass
        finally:
            if hasattr(self._local, "persistent_connection"):
                del self._local.persistent_connection
            if hasattr(self._local, "persistent_connection_checked_at"):
                del self._local.persistent_connection_checked_at

    def _new_connection(self) -> Any:
        if self.is_postgres:
            if psycopg is None:
                raise RuntimeError("PostgreSQL을 사용하려면 psycopg 패키지가 필요합니다. python -m pip install -e .")
            conn = psycopg.connect(self.location, row_factory=dict_row)
            return _PostgresConnection(conn)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            self._acquire_schema_init_lock(conn)
            conn.executescript(POSTGRES_SCHEMA if self.is_postgres else SCHEMA)
            self._ensure_column(conn, "article_drafts", "initial_title", "TEXT")
            self._ensure_column(conn, "article_drafts", "initial_body", "TEXT")
            self._ensure_column(conn, "article_drafts", "initial_review_note", "TEXT")
            self._ensure_column(conn, "article_drafts", "initial_model", "TEXT")
            self._ensure_column(conn, "article_drafts", "updated_at", "TEXT")
            self._ensure_column(conn, "article_drafts", "exported_at", "TEXT")
            self._ensure_column(conn, "press_releases", "validation_status", "TEXT DEFAULT '검증 완료'")
            self._ensure_column(conn, "press_releases", "validation_note", "TEXT DEFAULT '기존 수집 원문입니다.'")
            self._ensure_column(conn, "source_collection_runs", "failure_stage", "TEXT DEFAULT ''")
            self._ensure_column(conn, "source_collection_runs", "failure_reason", "TEXT DEFAULT ''")
            self._backfill_initial_draft_columns(conn)
            self._remove_news_brief_prefixes(conn)
            self._remove_leading_titles_from_bodies(conn)
            self._normalize_published_dates(conn)
            self._normalize_press_release_urls(conn)
            self._ensure_single_draft_index(conn)
            self._ensure_indexes(conn)

    def _acquire_schema_init_lock(self, conn: Any) -> None:
        if self.is_postgres:
            conn.execute("SELECT pg_advisory_xact_lock(?)", (POSTGRES_SCHEMA_INIT_LOCK_ID,))

    def _ensure_indexes(self, conn: Any) -> None:
        index_statements = [
            "CREATE INDEX IF NOT EXISTS idx_article_drafts_press_release_id ON article_drafts(press_release_id)",
            "CREATE INDEX IF NOT EXISTS idx_article_drafts_status_updated ON article_drafts(status, updated_at, id)",
            "CREATE INDEX IF NOT EXISTS idx_article_drafts_updated ON article_drafts(updated_at, id)",
            "CREATE INDEX IF NOT EXISTS idx_article_drafts_model_dates ON article_drafts(model, created_at, updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_press_releases_title ON press_releases(title)",
            "CREATE INDEX IF NOT EXISTS idx_press_releases_source_published ON press_releases(source_id, published_at, collected_at, id)",
            "CREATE INDEX IF NOT EXISTS idx_press_releases_region_published ON press_releases(region, published_at, collected_at, id)",
            "CREATE INDEX IF NOT EXISTS idx_press_release_assets_release ON press_release_assets(press_release_id, sort_order, id)",
            "CREATE INDEX IF NOT EXISTS idx_draft_history_draft_id ON draft_history(draft_id, id)",
            "CREATE INDEX IF NOT EXISTS idx_draft_generation_failures_press_release ON draft_generation_failures(press_release_id, resolved_at, next_retry_at)",
            "CREATE INDEX IF NOT EXISTS idx_draft_generation_failures_retry ON draft_generation_failures(resolved_at, next_retry_at, id)",
            "CREATE INDEX IF NOT EXISTS idx_source_collection_runs_source_id ON source_collection_runs(source_id, id)",
            "CREATE INDEX IF NOT EXISTS idx_visitor_access_logs_visited ON visitor_access_logs(visited_at, id)",
        ]
        for statement in index_statements:
            conn.execute(statement)

    def _ensure_column(self, conn: Any, table: str, column: str, definition: str) -> None:
        if self.is_postgres:
            columns = {
                row["column_name"]
                for row in conn.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = ?
                    """,
                    (table,),
                ).fetchall()
            }
        else:
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _backfill_initial_draft_columns(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            UPDATE article_drafts
            SET initial_title = COALESCE(initial_title, title),
                initial_body = COALESCE(initial_body, body),
                initial_review_note = COALESCE(initial_review_note, review_note),
                initial_model = COALESCE(initial_model, model)
            WHERE initial_title IS NULL
               OR initial_body IS NULL
               OR initial_review_note IS NULL
               OR initial_model IS NULL
            """
        )

    def _remove_news_brief_prefixes(self, conn: sqlite3.Connection) -> None:
        for column in ("title", "initial_title"):
            conn.execute(
                f"""
                UPDATE article_drafts
                SET {column} = LTRIM(SUBSTR({column}, 8))
                WHERE SUBSTR({column}, 1, 7) = '[뉴스 단신]'
                """
            )

    def _remove_leading_titles_from_bodies(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT id, title, body, initial_title, initial_body FROM article_drafts"
        ).fetchall()
        for row in rows:
            body = _strip_leading_body_title(row["body"], row["title"])
            initial_body = _strip_leading_body_title(row["initial_body"], row["initial_title"])
            if body != row["body"] or initial_body != row["initial_body"]:
                conn.execute(
                    """
                    UPDATE article_drafts
                    SET body = ?, initial_body = ?
                    WHERE id = ?
                    """,
                    (body, initial_body, row["id"]),
                )

    def _normalize_published_dates(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("SELECT id, published_at FROM press_releases").fetchall()
        for row in rows:
            normalized = _normalize_published_at(row["published_at"])
            if normalized and normalized != row["published_at"]:
                conn.execute(
                    "UPDATE press_releases SET published_at = ? WHERE id = ?",
                    (normalized, row["id"]),
                )

    def _normalize_press_release_urls(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("SELECT id, url FROM press_releases ORDER BY id").fetchall()
        canonical_ids: dict[str, int] = {}
        for row in rows:
            release_id = int(row["id"])
            canonical_url = _canonical_url(str(row["url"]))
            existing_id = canonical_ids.get(canonical_url)
            if existing_id is None:
                canonical_ids[canonical_url] = release_id
                if canonical_url != row["url"]:
                    duplicate = conn.execute(
                        "SELECT id FROM press_releases WHERE url = ? AND id != ?",
                        (canonical_url, release_id),
                    ).fetchone()
                    if duplicate:
                        canonical_ids[canonical_url] = int(duplicate["id"])
                        self._merge_press_release_duplicate(conn, int(duplicate["id"]), release_id)
                    else:
                        conn.execute("UPDATE press_releases SET url = ? WHERE id = ?", (canonical_url, release_id))
                continue

            self._merge_press_release_duplicate(conn, existing_id, release_id)

    def _merge_press_release_duplicate(
        self,
        conn: sqlite3.Connection,
        keep_id: int,
        duplicate_id: int,
    ) -> bool:
        keep_draft = conn.execute("SELECT id FROM article_drafts WHERE press_release_id = ?", (keep_id,)).fetchone()
        duplicate_draft = conn.execute(
            "SELECT id FROM article_drafts WHERE press_release_id = ?",
            (duplicate_id,),
        ).fetchone()
        if duplicate_draft and not keep_draft:
            conn.execute("UPDATE article_drafts SET press_release_id = ? WHERE press_release_id = ?", (keep_id, duplicate_id))
        elif duplicate_draft and keep_draft:
            return False
        conn.execute(
            "UPDATE draft_generation_failures SET press_release_id = ? WHERE press_release_id = ?",
            (keep_id, duplicate_id),
        )

        conn.execute(
            """
            UPDATE press_releases
            SET source_id = duplicate.source_id,
                source_name = duplicate.source_name,
                region = duplicate.region,
                title = duplicate.title,
                content = duplicate.content,
                published_at = COALESCE(duplicate.published_at, press_releases.published_at),
                collected_at = duplicate.collected_at,
                validation_status = duplicate.validation_status,
                validation_note = duplicate.validation_note
            FROM press_releases AS duplicate
            WHERE press_releases.id = ?
              AND duplicate.id = ?
            """,
            (keep_id, duplicate_id),
        )
        conn.execute("DELETE FROM press_releases WHERE id = ?", (duplicate_id,))
        return True

    def deduplicate_press_releases(self, limit: int = 50) -> dict[str, int]:
        if limit <= 0:
            return {"groups": 0, "merged": 0, "skipped": 0}
        published_date_expr = _published_date_expr("published_at")
        with self.connect() as conn:
            groups = conn.execute(
                f"""
                SELECT source_id,
                       LOWER(TRIM(title)) AS normalized_title,
                       {published_date_expr} AS published_date,
                       COUNT(*) AS count
                FROM press_releases
                WHERE TRIM(COALESCE(title, '')) != ''
                  AND {published_date_expr} != ''
                GROUP BY source_id, LOWER(TRIM(title)), {published_date_expr}
                HAVING COUNT(*) > 1
                ORDER BY count DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            merged = 0
            skipped = 0
            for group in groups:
                rows = conn.execute(
                    f"""
                    SELECT pr.id,
                           CASE WHEN ad.id IS NULL THEN 0 ELSE 1 END AS has_draft
                    FROM press_releases pr
                    LEFT JOIN article_drafts ad ON ad.press_release_id = pr.id
                    WHERE pr.source_id = ?
                      AND LOWER(TRIM(pr.title)) = ?
                      AND {published_date_expr.replace('published_at', 'pr.published_at')} = ?
                    ORDER BY has_draft DESC, pr.id ASC
                    """,
                    (group["source_id"], group["normalized_title"], group["published_date"]),
                ).fetchall()
                if len(rows) < 2:
                    continue
                keep_id = int(rows[0]["id"])
                for row in rows[1:]:
                    if self._merge_press_release_duplicate(conn, keep_id, int(row["id"])):
                        merged += 1
                    else:
                        skipped += 1
            return {"groups": len(groups), "merged": merged, "skipped": skipped}

    def _ensure_single_draft_index(self, conn: sqlite3.Connection) -> None:
        duplicate = conn.execute(
            """
            SELECT press_release_id
            FROM article_drafts
            GROUP BY press_release_id
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        ).fetchone()
        if duplicate:
            return
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_article_drafts_press_release_id
            ON article_drafts(press_release_id)
            """
        )

    def get_app_metadata(self, key: str) -> str | None:
        cache = getattr(self._local, "app_metadata_cache", None)
        if cache is not None:
            return cache.get(key)
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM app_metadata WHERE key = ?", (key,)).fetchone()
            return str(row["value"]) if row else None

    def set_app_metadata(self, key: str, value: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO app_metadata (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, now),
            )
        cache = getattr(self._local, "app_metadata_cache", None)
        if cache is not None:
            cache[key] = value

    def sync_source_metadata(self, sources: list[Source]) -> None:
        if not sources:
            return
        with self.connect() as conn:
            for source in sources:
                conn.execute(
                    """
                    UPDATE press_releases
                    SET source_name = ?, region = ?
                    WHERE source_id = ?
                      AND (source_name != ? OR region != ?)
                    """,
                    (source.name, source.region, source.id, source.name, source.region),
                )

    def add_press_release(self, item: PressRelease) -> int | None:
        item_url = _canonical_url(item.url)
        with self.connect() as conn:
            existing = conn.execute("SELECT id FROM press_releases WHERE url = ?", (item_url,)).fetchone()
            if existing:
                release_id = int(existing["id"])
                conn.execute(
                    """
                    UPDATE press_releases
                    SET source_id = ?, source_name = ?, region = ?, title = ?,
                        content = ?, published_at = COALESCE(?, published_at), collected_at = ?,
                        validation_status = ?, validation_note = ?
                    WHERE url = ?
                    """,
                    (
                        item.source_id,
                        item.source_name,
                        item.region,
                        item.title,
                        item.content,
                        item.published_at,
                        item.collected_at,
                        item.validation_status,
                        item.validation_note,
                        item_url,
                    ),
                )
                self._replace_press_release_assets(conn, release_id, item.assets)
                return None

            insert_sql = """
                INSERT INTO press_releases
                (source_id, source_name, region, title, url, content, published_at, collected_at,
                 validation_status, validation_note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
            if self.is_postgres:
                insert_sql += " RETURNING id"
            cur = conn.execute(
                insert_sql,
                (
                    item.source_id,
                    item.source_name,
                    item.region,
                    item.title,
                    item_url,
                    item.content,
                    item.published_at,
                    item.collected_at,
                    item.validation_status,
                    item.validation_note,
                ),
            )
            if self.is_postgres:
                row = cur.fetchone()
                release_id = int(row["id"]) if row else None
            else:
                release_id = int(cur.lastrowid) if cur.lastrowid else None
            if release_id:
                self._replace_press_release_assets(conn, release_id, item.assets)
            return release_id

    def _replace_press_release_assets(
        self,
        conn: Any,
        press_release_id: int,
        assets: list[PressReleaseAsset],
    ) -> None:
        conn.execute("DELETE FROM press_release_assets WHERE press_release_id = ?", (press_release_id,))
        if not assets:
            return
        rows = [
            (
                press_release_id,
                asset.asset_type,
                _canonical_url(asset.url),
                asset.title,
                asset.filename,
                asset.content_type,
                1 if asset.is_image else 0,
                index,
                _now(),
            )
            for index, asset in enumerate(assets)
            if asset.url
        ]
        if not rows:
            return
        conn.executemany(
            """
            INSERT INTO press_release_assets
            (press_release_id, asset_type, url, title, filename, content_type, is_image, sort_order, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (press_release_id, url) DO NOTHING
            """,
            rows,
        )

    def press_release_assets(self, press_release_id: int) -> list[Any]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT *
                FROM press_release_assets
                WHERE press_release_id = ?
                ORDER BY sort_order ASC, id ASC
                """,
                (press_release_id,),
            ).fetchall()

    def get_press_release_asset(self, asset_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT pra.*, pr.source_id, pr.title AS press_title, pr.url AS press_url
                FROM press_release_assets pra
                JOIN press_releases pr ON pr.id = pra.press_release_id
                WHERE pra.id = ?
                """,
                (asset_id,),
            ).fetchone()

    def press_release_assets_by_ids(self, press_release_ids: list[int]) -> dict[int, list[Any]]:
        if not press_release_ids:
            return {}
        placeholders = ",".join("?" for _ in press_release_ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM press_release_assets
                WHERE press_release_id IN ({placeholders})
                ORDER BY press_release_id ASC, sort_order ASC, id ASC
                """,
                tuple(press_release_ids),
            ).fetchall()
        grouped: dict[int, list[Any]] = {}
        for row in rows:
            grouped.setdefault(int(row["press_release_id"]), []).append(row)
        return grouped

    def press_release_assets_by_source(self, source_id: str, limit: int = 30) -> list[Any]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT pra.*, pr.title AS press_title, pr.published_at, pr.url AS press_url,
                       ad.id AS draft_id, ad.status AS draft_status, ad.model AS draft_model
                FROM press_release_assets pra
                JOIN press_releases pr ON pr.id = pra.press_release_id
                LEFT JOIN article_drafts ad ON ad.press_release_id = pr.id
                WHERE pr.source_id = ?
                ORDER BY CASE WHEN pr.published_at IS NULL OR TRIM(pr.published_at) = '' THEN 1 ELSE 0 END ASC,
                         pr.published_at DESC,
                         pr.collected_at DESC,
                         pr.id DESC,
                         pra.sort_order ASC,
                         pra.id ASC
                LIMIT ?
                """,
                (source_id, limit),
            ).fetchall()

    def press_release_image_assets(self, limit: int = 2000) -> list[Any]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT *
                FROM press_release_assets
                WHERE is_image = 1
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def delete_press_release_assets(self, asset_ids: list[int]) -> int:
        if not asset_ids:
            return 0
        placeholders = ",".join("?" for _ in asset_ids)
        with self.connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM press_release_assets WHERE id IN ({placeholders})",
                tuple(asset_ids),
            )
            return int(getattr(cursor, "rowcount", 0) or 0)

    def get_press_release(self, release_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT pr.*, ad.id AS draft_id, ad.status AS draft_status, ad.model AS draft_model
                FROM press_releases pr
                LEFT JOIN article_drafts ad ON ad.press_release_id = pr.id
                WHERE pr.id = ?
                """,
                (release_id,),
            ).fetchone()

    def pending_press_releases(self, limit: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT pr.*
                FROM press_releases pr
                LEFT JOIN article_drafts ad ON ad.press_release_id = pr.id
                WHERE ad.id IS NULL
                ORDER BY CASE WHEN pr.published_at IS NULL OR TRIM(pr.published_at) = '' THEN 1 ELSE 0 END ASC,
                         pr.published_at DESC,
                         pr.collected_at DESC,
                         pr.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def pending_press_releases_ready_for_retry(self, limit: int) -> list[sqlite3.Row]:
        now = _now()
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT pr.*
                FROM press_releases pr
                LEFT JOIN article_drafts ad ON ad.press_release_id = pr.id
                LEFT JOIN draft_generation_failures dgf
                  ON dgf.press_release_id = pr.id
                 AND dgf.resolved_at IS NULL
                WHERE ad.id IS NULL
                  AND (dgf.id IS NULL OR dgf.next_retry_at <= ?)
                ORDER BY CASE WHEN dgf.id IS NULL THEN 0 ELSE 1 END ASC,
                         CASE WHEN pr.published_at IS NULL OR TRIM(pr.published_at) = '' THEN 1 ELSE 0 END ASC,
                         pr.published_at DESC,
                         pr.collected_at DESC,
                         pr.id DESC
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()

    def record_draft_generation_failure(
        self,
        press_release_id: int,
        failure_kind: str,
        message: str,
        attempted_models: str,
        next_retry_at: str,
    ) -> None:
        now = _now()
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT id, attempts
                FROM draft_generation_failures
                WHERE press_release_id = ?
                  AND resolved_at IS NULL
                ORDER BY id DESC
                LIMIT 1
                """,
                (press_release_id,),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE draft_generation_failures
                    SET failure_kind = ?,
                        message = ?,
                        attempted_models = ?,
                        attempts = ?,
                        last_failed_at = ?,
                        next_retry_at = ?
                    WHERE id = ?
                    """,
                    (
                        failure_kind,
                        message[:1000],
                        attempted_models[:300],
                        int(existing["attempts"] or 0) + 1,
                        now,
                        next_retry_at,
                        existing["id"],
                    ),
                )
                return
            conn.execute(
                """
                INSERT INTO draft_generation_failures
                (press_release_id, failure_kind, message, attempted_models,
                 attempts, first_failed_at, last_failed_at, next_retry_at, resolved_at)
                VALUES (?, ?, ?, ?, 1, ?, ?, ?, NULL)
                """,
                (
                    press_release_id,
                    failure_kind,
                    message[:1000],
                    attempted_models[:300],
                    now,
                    now,
                    next_retry_at,
                ),
            )

    def mark_draft_generation_success(self, press_release_id: int) -> None:
        with self.connect() as conn:
            self._resolve_draft_generation_failures(conn, press_release_id)

    def _resolve_draft_generation_failures(self, conn: Any, press_release_id: int) -> None:
        conn.execute(
            """
            UPDATE draft_generation_failures
            SET resolved_at = ?
            WHERE press_release_id = ?
              AND resolved_at IS NULL
            """,
            (_now(), press_release_id),
        )

    def draft_generation_failure_summary(self, limit: int = 5) -> dict[str, object]:
        now = _now()
        with self.connect() as conn:
            total = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM draft_generation_failures dgf
                LEFT JOIN article_drafts ad ON ad.press_release_id = dgf.press_release_id
                WHERE dgf.resolved_at IS NULL
                  AND ad.id IS NULL
                """
            ).fetchone()["count"]
            due = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM draft_generation_failures dgf
                LEFT JOIN article_drafts ad ON ad.press_release_id = dgf.press_release_id
                WHERE dgf.resolved_at IS NULL
                  AND ad.id IS NULL
                  AND dgf.next_retry_at <= ?
                """,
                (now,),
            ).fetchone()["count"]
            next_retry_row = conn.execute(
                """
                SELECT MIN(dgf.next_retry_at) AS next_retry_at
                FROM draft_generation_failures dgf
                LEFT JOIN article_drafts ad ON ad.press_release_id = dgf.press_release_id
                WHERE dgf.resolved_at IS NULL
                  AND ad.id IS NULL
                """
            ).fetchone()
            oldest_failure_row = conn.execute(
                """
                SELECT MIN(dgf.first_failed_at) AS first_failed_at
                FROM draft_generation_failures dgf
                LEFT JOIN article_drafts ad ON ad.press_release_id = dgf.press_release_id
                WHERE dgf.resolved_at IS NULL
                  AND ad.id IS NULL
                """
            ).fetchone()
            by_kind = conn.execute(
                """
                SELECT dgf.failure_kind, COUNT(*) AS count
                FROM draft_generation_failures dgf
                LEFT JOIN article_drafts ad ON ad.press_release_id = dgf.press_release_id
                WHERE dgf.resolved_at IS NULL
                  AND ad.id IS NULL
                GROUP BY dgf.failure_kind
                ORDER BY count DESC, failure_kind ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            latest = conn.execute(
                """
                SELECT dgf.*, pr.title, pr.source_name
                FROM draft_generation_failures dgf
                JOIN press_releases pr ON pr.id = dgf.press_release_id
                LEFT JOIN article_drafts ad ON ad.press_release_id = dgf.press_release_id
                WHERE dgf.resolved_at IS NULL
                  AND ad.id IS NULL
                ORDER BY dgf.last_failed_at DESC, dgf.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return {
            "total": int(total or 0),
            "due": int(due or 0),
            "next_retry_at": str(next_retry_row["next_retry_at"] or "") if next_retry_row else "",
            "oldest_first_failed_at": str(oldest_failure_row["first_failed_at"] or "") if oldest_failure_row else "",
            "by_kind": [
                {"kind": str(row["failure_kind"]), "count": int(row["count"])}
                for row in by_kind
            ],
            "latest": [
                {
                    "press_release_id": int(row["press_release_id"]),
                    "title": str(row["title"]),
                    "source_name": str(row["source_name"]),
                    "failure_kind": str(row["failure_kind"]),
                    "attempts": int(row["attempts"] or 0),
                    "next_retry_at": str(row["next_retry_at"]),
                }
                for row in latest
            ],
        }

    def pending_press_releases_for_date(
        self,
        published_date: str,
        limit: int,
        *,
        oldest_first: bool = False,
    ) -> list[sqlite3.Row]:
        direction = "ASC" if oldest_first else "DESC"
        with self.connect() as conn:
            return conn.execute(
                f"""
                SELECT pr.*
                FROM press_releases pr
                LEFT JOIN article_drafts ad ON ad.press_release_id = pr.id
                WHERE ad.id IS NULL
                  AND pr.published_at LIKE ?
                ORDER BY pr.published_at {direction},
                         pr.collected_at {direction},
                         pr.id {direction}
                LIMIT ?
                """,
                (f"{published_date}%", limit),
            ).fetchall()

    def count_pending_press_releases_for_date(self, published_date: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM press_releases pr
                LEFT JOIN article_drafts ad ON ad.press_release_id = pr.id
                WHERE ad.id IS NULL
                  AND pr.published_at LIKE ?
                """,
                (f"{published_date}%",),
            ).fetchone()
        return int(row["count"])

    def pending_press_release_summary(self, limit: int = 5) -> dict[str, object]:
        published_date_expr = _published_date_expr("pr.published_at")
        with self.connect() as conn:
            total = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM press_releases pr
                LEFT JOIN article_drafts ad ON ad.press_release_id = pr.id
                WHERE ad.id IS NULL
                """
            ).fetchone()["count"]
            by_date = conn.execute(
                f"""
                SELECT
                    CASE
                        WHEN {published_date_expr} = '' THEN '게시일 없음'
                        ELSE {published_date_expr}
                    END AS published_date,
                    COUNT(*) AS count
                FROM press_releases pr
                LEFT JOIN article_drafts ad ON ad.press_release_id = pr.id
                WHERE ad.id IS NULL
                GROUP BY
                    CASE
                        WHEN {published_date_expr} = '' THEN '게시일 없음'
                        ELSE {published_date_expr}
                    END
                ORDER BY published_date DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            by_source = conn.execute(
                """
                SELECT pr.source_name, COUNT(*) AS count
                FROM press_releases pr
                LEFT JOIN article_drafts ad ON ad.press_release_id = pr.id
                WHERE ad.id IS NULL
                GROUP BY pr.source_name
                ORDER BY count DESC, pr.source_name ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            latest = conn.execute(
                f"""
                SELECT pr.published_at
                FROM press_releases pr
                LEFT JOIN article_drafts ad ON ad.press_release_id = pr.id
                WHERE ad.id IS NULL
                  AND {published_date_expr} != ''
                ORDER BY {published_date_expr} DESC, pr.collected_at DESC, pr.id DESC
                LIMIT 1
                """
            ).fetchone()
            oldest = conn.execute(
                f"""
                SELECT pr.published_at
                FROM press_releases pr
                LEFT JOIN article_drafts ad ON ad.press_release_id = pr.id
                WHERE ad.id IS NULL
                  AND {published_date_expr} != ''
                ORDER BY {published_date_expr} ASC, pr.collected_at ASC, pr.id ASC
                LIMIT 1
                """
            ).fetchone()
        return {
            "total": int(total),
            "by_date": [
                {"date": str(row["published_date"]), "count": int(row["count"])}
                for row in by_date
            ],
            "by_source": [
                {"source_name": str(row["source_name"]), "count": int(row["count"])}
                for row in by_source
            ],
            "latest_published_at": str(latest["published_at"]) if latest else None,
            "oldest_published_at": str(oldest["published_at"]) if oldest else None,
        }

    def delete_press_releases_before(self, cutoff_date: str) -> dict[str, int]:
        published_date_expr = _published_date_expr("published_at")
        press_where = f"TRIM(COALESCE(published_at, '')) != '' AND {published_date_expr} < ?"
        joined_where = f"TRIM(COALESCE(pr.published_at, '')) != '' AND {_published_date_expr('pr.published_at')} < ?"
        with self.connect() as conn:
            press_count = int(
                conn.execute(
                    f"SELECT COUNT(*) AS count FROM press_releases WHERE {press_where}",
                    (cutoff_date,),
                ).fetchone()["count"]
            )
            draft_count = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*) AS count
                    FROM article_drafts ad
                    JOIN press_releases pr ON pr.id = ad.press_release_id
                    WHERE {joined_where}
                    """,
                    (cutoff_date,),
                ).fetchone()["count"]
            )
            history_count = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*) AS count
                    FROM draft_history dh
                    JOIN article_drafts ad ON ad.id = dh.draft_id
                    JOIN press_releases pr ON pr.id = ad.press_release_id
                    WHERE {joined_where}
                    """,
                    (cutoff_date,),
                ).fetchone()["count"]
            )
            if press_count <= 0:
                return {"press_releases": 0, "drafts": 0, "draft_history": 0}
            conn.execute(
                f"""
                DELETE FROM draft_generation_failures
                WHERE press_release_id IN (
                    SELECT id
                    FROM press_releases
                    WHERE {press_where}
                )
                """,
                (cutoff_date,),
            )
            conn.execute(
                f"""
                DELETE FROM draft_history
                WHERE draft_id IN (
                    SELECT ad.id
                    FROM article_drafts ad
                    JOIN press_releases pr ON pr.id = ad.press_release_id
                    WHERE {joined_where}
                )
                """,
                (cutoff_date,),
            )
            conn.execute(
                f"""
                DELETE FROM article_drafts
                WHERE press_release_id IN (
                    SELECT id
                    FROM press_releases
                    WHERE {press_where}
                )
                """,
                (cutoff_date,),
            )
            conn.execute(
                f"DELETE FROM press_releases WHERE {press_where}",
                (cutoff_date,),
            )
        return {"press_releases": press_count, "drafts": draft_count, "draft_history": history_count}

    def add_article_draft(self, draft: ArticleDraft) -> int:
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM article_drafts WHERE press_release_id = ?",
                (draft.press_release_id,),
            ).fetchone()
            if existing:
                self._resolve_draft_generation_failures(conn, draft.press_release_id)
                return int(existing["id"])
            if self.is_postgres:
                insert_sql = """
                INSERT INTO article_drafts
                (press_release_id, title, body, review_note, model, created_at,
                 initial_title, initial_body, initial_review_note, initial_model)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (press_release_id) DO NOTHING
                RETURNING id
                """
            else:
                insert_sql = """
                INSERT OR IGNORE INTO article_drafts
                (press_release_id, title, body, review_note, model, created_at,
                 initial_title, initial_body, initial_review_note, initial_model)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
            cur = conn.execute(
                insert_sql,
                (
                    draft.press_release_id,
                    draft.title,
                    draft.body,
                    draft.review_note,
                    draft.model,
                    draft.created_at,
                    draft.title,
                    draft.body,
                    draft.review_note,
                    draft.model,
                ),
            )
            if self.is_postgres:
                row = cur.fetchone()
                if row:
                    self._resolve_draft_generation_failures(conn, draft.press_release_id)
                    return int(row["id"])
                existing = conn.execute(
                    "SELECT id FROM article_drafts WHERE press_release_id = ?",
                    (draft.press_release_id,),
                ).fetchone()
                if existing:
                    self._resolve_draft_generation_failures(conn, draft.press_release_id)
                return int(existing["id"]) if existing else 0
            if cur.lastrowid:
                self._resolve_draft_generation_failures(conn, draft.press_release_id)
                return int(cur.lastrowid)
            existing = conn.execute(
                "SELECT id FROM article_drafts WHERE press_release_id = ?",
                (draft.press_release_id,),
            ).fetchone()
            if existing:
                self._resolve_draft_generation_failures(conn, draft.press_release_id)
            return int(existing["id"]) if existing else 0

    def counts(self) -> dict[str, int]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM press_releases) AS releases,
                    COUNT(*) AS drafts,
                    SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) AS approved,
                    SUM(CASE WHEN status = 'needs_review' THEN 1 ELSE 0 END) AS needs_review
                FROM article_drafts
                """
            ).fetchone()
        return {
            "press_releases": int(row["releases"] or 0),
            "drafts": int(row["drafts"] or 0),
            "approved": int(row["approved"] or 0),
            "needs_review": int(row["needs_review"] or 0),
        }

    def press_releases(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT pr.*, ad.id AS draft_id, ad.status AS draft_status, ad.model AS draft_model
                FROM press_releases pr
                LEFT JOIN article_drafts ad ON ad.press_release_id = pr.id
                ORDER BY CASE WHEN pr.published_at IS NULL OR TRIM(pr.published_at) = '' THEN 1 ELSE 0 END ASC,
                         pr.published_at DESC,
                         pr.collected_at DESC,
                         pr.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def press_releases_by_source(self, source_id: str, limit: int = 50) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT pr.*, ad.id AS draft_id, ad.status AS draft_status, ad.model AS draft_model
                FROM press_releases pr
                LEFT JOIN article_drafts ad ON ad.press_release_id = pr.id
                WHERE pr.source_id = ?
                ORDER BY CASE WHEN pr.published_at IS NULL OR TRIM(pr.published_at) = '' THEN 1 ELSE 0 END ASC,
                         pr.published_at DESC,
                         pr.collected_at DESC,
                         pr.id DESC
                LIMIT ?
                """,
                (source_id, limit),
            ).fetchall()

    def press_releases_missing_published_at(self, source_id: str, limit: int = 20) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT id, source_id, source_name, title, url, published_at
                FROM press_releases
                WHERE source_id = ?
                  AND (published_at IS NULL OR TRIM(published_at) = '')
                ORDER BY id DESC
                LIMIT ?
                """,
                (source_id, limit),
            ).fetchall()

    def update_press_release_published_at(self, release_id: int, published_at: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE press_releases SET published_at = ? WHERE id = ?",
                (published_at, release_id),
            )

    def recent_drafts(self, limit: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT ad.*, pr.source_name, pr.url
                FROM article_drafts ad
                JOIN press_releases pr ON pr.id = ad.press_release_id
                ORDER BY COALESCE(ad.updated_at, ad.created_at) DESC,
                         ad.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def drafts(self, status: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
        query = """
                SELECT ad.*, pr.source_id, pr.source_name, pr.region, pr.url, pr.content AS original_content,
                   pr.title AS original_title, pr.published_at,
                   pr.validation_status, pr.validation_note
            FROM article_drafts ad
            JOIN press_releases pr ON pr.id = ad.press_release_id
        """
        params: tuple[object, ...]
        if status:
            query += " WHERE ad.status = ?"
            params = (status, limit)
        else:
            params = (limit,)
        query += " ORDER BY COALESCE(ad.updated_at, ad.created_at) DESC, ad.id DESC LIMIT ?"
        with self.connect() as conn:
            return conn.execute(query, params).fetchall()

    def drafts_by_source(
        self,
        source_id: str,
        status: str | None = None,
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        query = """
                SELECT ad.*, pr.source_id, pr.source_name, pr.region, pr.url, pr.content AS original_content,
                   pr.title AS original_title, pr.published_at,
                   pr.validation_status, pr.validation_note
            FROM article_drafts ad
            JOIN press_releases pr ON pr.id = ad.press_release_id
            WHERE pr.source_id = ?
        """
        params: tuple[object, ...]
        if status:
            query += " AND ad.status = ?"
            params = (source_id, status, limit)
        else:
            params = (source_id, limit)
        query += " ORDER BY COALESCE(ad.updated_at, ad.created_at) DESC, ad.id DESC LIMIT ?"
        with self.connect() as conn:
            return conn.execute(query, params).fetchall()

    def get_draft(self, draft_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT ad.*, pr.source_id, pr.source_name, pr.region, pr.url,
                       pr.title AS original_title, pr.content AS original_content,
                       pr.published_at, pr.collected_at,
                       pr.validation_status, pr.validation_note
                FROM article_drafts ad
                JOIN press_releases pr ON pr.id = ad.press_release_id
                WHERE ad.id = ?
                """,
                (draft_id,),
            ).fetchone()

    def update_draft(
        self,
        draft_id: int,
        title: str,
        body: str,
        review_note: str,
        status: str,
        model: str | None = None,
        change_type: str = "manual",
    ) -> None:
        with self.connect() as conn:
            previous = conn.execute("SELECT * FROM article_drafts WHERE id = ?", (draft_id,)).fetchone()
            if previous and _draft_changed(previous, title, body, review_note, status, model):
                self._record_draft_history(conn, previous, change_type)
            if model is None:
                conn.execute(
                    """
                    UPDATE article_drafts
                    SET title = ?, body = ?, review_note = ?, status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (title, body, review_note, status, _now(), draft_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE article_drafts
                    SET title = ?, body = ?, review_note = ?, status = ?, model = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (title, body, review_note, status, model, _now(), draft_id),
                )

    def restore_initial_draft(self, draft_id: int, status: str) -> None:
        with self.connect() as conn:
            previous = conn.execute("SELECT * FROM article_drafts WHERE id = ?", (draft_id,)).fetchone()
            if previous:
                self._record_draft_history(conn, previous, "restore_initial")
            conn.execute(
                """
                UPDATE article_drafts
                SET title = COALESCE(initial_title, title),
                    body = COALESCE(initial_body, body),
                    review_note = COALESCE(initial_review_note, review_note),
                    model = COALESCE(initial_model, model),
                    status = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (status, _now(), draft_id),
            )

    def set_draft_status(self, draft_id: int, status: str) -> None:
        with self.connect() as conn:
            previous = conn.execute("SELECT * FROM article_drafts WHERE id = ?", (draft_id,)).fetchone()
            if previous and previous["status"] != status:
                self._record_draft_history(conn, previous, "status")
            conn.execute(
                "UPDATE article_drafts SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now(), draft_id),
            )

    def approved_drafts(self, limit: int | None = None, unexported_only: bool = False) -> list[sqlite3.Row]:
        limit_clause = "" if limit is None else "LIMIT ?"
        params: tuple[object, ...] = () if limit is None else (limit,)
        where = "WHERE ad.status = 'approved'"
        if unexported_only:
            where += " AND ad.exported_at IS NULL"
        with self.connect() as conn:
            return conn.execute(
                f"""
                SELECT ad.*, COALESCE(ad.updated_at, ad.created_at) AS approved_time,
                       pr.source_name, pr.region, pr.url, pr.title AS original_title, pr.published_at
                FROM article_drafts ad
                JOIN press_releases pr ON pr.id = ad.press_release_id
                {where}
                ORDER BY COALESCE(ad.updated_at, ad.created_at) DESC, ad.id DESC
                {limit_clause}
                """,
                params,
            ).fetchall()

    def mark_exported(self, draft_ids: list[int]) -> None:
        if not draft_ids:
            return
        placeholders = ",".join("?" for _ in draft_ids)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE article_drafts SET exported_at = ? WHERE id IN ({placeholders})",
                (_now(), *draft_ids),
            )

    def draft_history(self, draft_id: int, limit: int = 20) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT *
                FROM draft_history
                WHERE draft_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (draft_id, limit),
            ).fetchall()

    def get_draft_history_item(self, draft_id: int, history_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT *
                FROM draft_history
                WHERE draft_id = ? AND id = ?
                """,
                (draft_id, history_id),
            ).fetchone()

    def record_source_collection_status(
        self,
        source_id: str,
        source_name: str,
        status: str,
        message: str,
        failure_stage: str = "",
        failure_reason: str = "",
        releases_found: int = 0,
        inserted_count: int = 0,
        repaired_dates: int = 0,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO source_collection_runs
                (source_id, source_name, status, message, failure_stage, failure_reason,
                 releases_found, inserted_count, repaired_dates, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    source_name,
                    status,
                    message,
                    failure_stage,
                    failure_reason,
                    max(0, releases_found),
                    max(0, inserted_count),
                    max(0, repaired_dates),
                    _now(),
                ),
            )

    def latest_source_collection_statuses(self) -> dict[str, sqlite3.Row]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT scr.*
                FROM source_collection_runs scr
                JOIN (
                    SELECT source_id, MAX(id) AS max_id
                    FROM source_collection_runs
                    GROUP BY source_id
                ) latest ON latest.max_id = scr.id
                """
            ).fetchall()
        return {str(row["source_id"]): row for row in rows}

    def record_visitor_access(
        self,
        masked_ip: str,
        method: str,
        path: str,
        endpoint: str,
        status_code: int,
        user_agent: str,
        visited_at: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO visitor_access_logs
                (masked_ip, method, path, endpoint, status_code, user_agent, visited_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    masked_ip,
                    method[:12],
                    path[:300],
                    endpoint[:80],
                    int(status_code or 0),
                    user_agent[:80],
                    visited_at or _now(),
                ),
            )

    def visitor_access_logs_since(self, cutoff_iso: str, limit: int = 2000) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT *
                FROM visitor_access_logs
                WHERE visited_at >= ?
                ORDER BY visited_at DESC, id DESC
                LIMIT ?
                """,
                (cutoff_iso, limit),
            ).fetchall()

    def prune_visitor_access_logs(self, cutoff_iso: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM visitor_access_logs WHERE visited_at < ?",
                (cutoff_iso,),
            ).fetchone()
            conn.execute("DELETE FROM visitor_access_logs WHERE visited_at < ?", (cutoff_iso,))
        return int(row["count"] or 0)

    def _record_draft_history(self, conn: sqlite3.Connection, row: sqlite3.Row, change_type: str) -> None:
        conn.execute(
            """
            INSERT INTO draft_history
            (draft_id, title, body, review_note, status, model, change_type, changed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["title"],
                row["body"],
                row["review_note"],
                row["status"],
                row["model"],
                change_type,
                _now(),
            ),
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _published_date_expr(column: str) -> str:
    return f"REPLACE(REPLACE(SUBSTR(TRIM(COALESCE({column}, '')), 1, 10), '.', '-'), '/', '-')"


def _draft_changed(
    previous: sqlite3.Row,
    title: str,
    body: str,
    review_note: str,
    status: str,
    model: str | None,
) -> bool:
    next_model = previous["model"] if model is None else model
    return any(
        (
            previous["title"] != title,
            previous["body"] != body,
            previous["review_note"] != review_note,
            previous["status"] != status,
            previous["model"] != next_model,
        )
    )


def _strip_leading_body_title(body: str | None, title: str | None) -> str | None:
    if not body:
        return body
    title_key = _compare_key(title or "")
    lines = body.splitlines()
    stripped: list[str] = []
    skipping = True

    for line in lines:
        text = line.strip()
        cleaned = _strip_news_brief_prefix(text)
        cleaned = cleaned.removeprefix("제목:").strip()
        cleaned = _strip_news_brief_prefix(cleaned)
        is_title = title_key and _compare_key(cleaned) == title_key
        is_label = cleaned in {"본문", "본문:", "제목", "제목:"}
        if skipping and (not text or is_title or is_label):
            continue
        skipping = False
        stripped.append(line)

    return "\n".join(stripped).strip() or body


def _strip_news_brief_prefix(text: str) -> str:
    return text.removeprefix("[뉴스 단신]").strip()


def _compare_key(text: str) -> str:
    return "".join(ch for ch in text.casefold() if ch.isalnum())
