from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .collectors import _normalize_published_at
from .models import ArticleDraft, PressRelease


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
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
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
            self._backfill_initial_draft_columns(conn)
            self._remove_news_brief_prefixes(conn)
            self._remove_leading_titles_from_bodies(conn)
            self._normalize_published_dates(conn)

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

    def add_press_release(self, item: PressRelease) -> int | None:
        with self.connect() as conn:
            existing = conn.execute("SELECT id FROM press_releases WHERE url = ?", (item.url,)).fetchone()
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
                        item.url,
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
                    item.url,
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
            cur = conn.execute(
                """
                INSERT INTO article_drafts
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
            return int(cur.lastrowid)

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
    ) -> None:
        with self.connect() as conn:
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
            conn.execute(
                "UPDATE article_drafts SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now(), draft_id),
            )

    def approved_drafts(self, limit: int | None = None) -> list[sqlite3.Row]:
        limit_clause = "" if limit is None else "LIMIT ?"
        params: tuple[object, ...] = () if limit is None else (limit,)
        with self.connect() as conn:
            return conn.execute(
                f"""
                SELECT ad.*, COALESCE(ad.updated_at, ad.created_at) AS approved_time,
                       pr.source_name, pr.region, pr.url, pr.title AS original_title, pr.published_at
                FROM article_drafts ad
                JOIN press_releases pr ON pr.id = ad.press_release_id
                WHERE ad.status = 'approved'
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
