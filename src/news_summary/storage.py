from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .collectors import _canonical_url, _normalize_published_at
from .models import ArticleDraft, PressRelease, Source


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
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_draft_history_draft_id ON draft_history(draft_id, id DESC)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_source_collection_runs_source_id ON source_collection_runs(source_id, id DESC)"
            )

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
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
                    try:
                        conn.execute("UPDATE press_releases SET url = ? WHERE id = ?", (canonical_url, release_id))
                    except sqlite3.IntegrityError:
                        duplicate = conn.execute(
                            "SELECT id FROM press_releases WHERE url = ? AND id != ?",
                            (canonical_url, release_id),
                        ).fetchone()
                        if duplicate:
                            canonical_ids[canonical_url] = int(duplicate["id"])
                            self._merge_press_release_duplicate(conn, int(duplicate["id"]), release_id)
                continue

            self._merge_press_release_duplicate(conn, existing_id, release_id)

    def _merge_press_release_duplicate(
        self,
        conn: sqlite3.Connection,
        keep_id: int,
        duplicate_id: int,
    ) -> None:
        keep_draft = conn.execute("SELECT id FROM article_drafts WHERE press_release_id = ?", (keep_id,)).fetchone()
        duplicate_draft = conn.execute(
            "SELECT id FROM article_drafts WHERE press_release_id = ?",
            (duplicate_id,),
        ).fetchone()
        if duplicate_draft and not keep_draft:
            conn.execute("UPDATE article_drafts SET press_release_id = ? WHERE press_release_id = ?", (keep_id, duplicate_id))
        elif duplicate_draft and keep_draft:
            return

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
                return None

            cur = conn.execute(
                """
                INSERT INTO press_releases
                (source_id, source_name, region, title, url, content, published_at, collected_at,
                 validation_status, validation_note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
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
            return int(cur.lastrowid) if cur.lastrowid else None

    def pending_press_releases(self, limit: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT pr.*
                FROM press_releases pr
                LEFT JOIN article_drafts ad ON ad.press_release_id = pr.id
                WHERE ad.id IS NULL
                ORDER BY pr.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def add_article_draft(self, draft: ArticleDraft) -> int:
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM article_drafts WHERE press_release_id = ?",
                (draft.press_release_id,),
            ).fetchone()
            if existing:
                return int(existing["id"])
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO article_drafts
                (press_release_id, title, body, review_note, model, created_at,
                 initial_title, initial_body, initial_review_note, initial_model)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
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
            if cur.lastrowid:
                return int(cur.lastrowid)
            existing = conn.execute(
                "SELECT id FROM article_drafts WHERE press_release_id = ?",
                (draft.press_release_id,),
            ).fetchone()
            return int(existing["id"]) if existing else 0

    def counts(self) -> dict[str, int]:
        with self.connect() as conn:
            releases = conn.execute("SELECT COUNT(*) AS count FROM press_releases").fetchone()["count"]
            drafts = conn.execute("SELECT COUNT(*) AS count FROM article_drafts").fetchone()["count"]
            approved = conn.execute(
                "SELECT COUNT(*) AS count FROM article_drafts WHERE status = 'approved'"
            ).fetchone()["count"]
            needs_review = conn.execute(
                "SELECT COUNT(*) AS count FROM article_drafts WHERE status = 'needs_review'"
            ).fetchone()["count"]
        return {
            "press_releases": int(releases),
            "drafts": int(drafts),
            "approved": int(approved),
            "needs_review": int(needs_review),
        }

    def press_releases(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT pr.*, ad.id AS draft_id, ad.status AS draft_status
                FROM press_releases pr
                LEFT JOIN article_drafts ad ON ad.press_release_id = pr.id
                ORDER BY pr.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def press_releases_by_source(self, source_id: str, limit: int = 50) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT pr.*, ad.id AS draft_id, ad.status AS draft_status
                FROM press_releases pr
                LEFT JOIN article_drafts ad ON ad.press_release_id = pr.id
                WHERE pr.source_id = ?
                ORDER BY pr.id DESC
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
                ORDER BY ad.id DESC
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
        query += " ORDER BY ad.id DESC LIMIT ?"
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
        query += " ORDER BY ad.id DESC LIMIT ?"
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
