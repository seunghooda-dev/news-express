from pathlib import Path
import json
import sqlite3
import zipfile

from news_summary.backup import POSTGRES_BACKUP_TABLES, POSTGRES_EXPORT_ARCHIVE_NAME, create_backup, restore_backup, verify_backup


def test_create_backup_includes_sqlite_and_config_files(tmp_path):
    project_root = tmp_path
    db_path = project_root / "data" / "news_summary.sqlite"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE sample (value TEXT)")
    conn.execute("INSERT INTO sample VALUES ('ok')")
    conn.commit()
    conn.close()
    (project_root / ".env").write_text("GEMINI_API_KEY=test\n", encoding="utf-8")
    (project_root / "config").mkdir()
    (project_root / "config" / "municipalities.yaml").write_text("sources: []\n", encoding="utf-8")
    (project_root / "data" / "writing_settings.json").write_text("{}\n", encoding="utf-8")

    backup_path = create_backup(project_root, Path("data/news_summary.sqlite"), Path("data/backups"))

    assert backup_path.exists()
    restored_names = restore_backup(project_root, backup_path, dry_run=True)
    assert "data/news_summary.sqlite" in restored_names
    assert ".env" in restored_names
    assert "config/municipalities.yaml" in restored_names
    assert "data/writing_settings.json" in restored_names
    verification = verify_backup(backup_path)
    assert verification["ok"] is True
    assert verification["checked_sqlite"] is True
    assert verification["sensitive_config_keys"] == ["GEMINI_API_KEY"]


def test_verify_backup_reports_sensitive_env_keys(tmp_path):
    project_root = tmp_path
    db_path = project_root / "data" / "news_summary.sqlite"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE sample (value TEXT)")
    conn.commit()
    conn.close()
    (project_root / ".env").write_text(
        "\n".join(
            [
                "GEMINI_API_KEY=test",
                "NEWS_SUMMARY_ADMIN_PASSWORD=secret",
                "NEWS_SUMMARY_PUBLIC_URL=https://example.com",
            ]
        ),
        encoding="utf-8",
    )

    backup_path = create_backup(project_root, Path("data/news_summary.sqlite"), Path("data/backups"))
    verification = verify_backup(backup_path)

    assert verification["ok"] is True
    assert verification["sensitive_config_keys"] == ["GEMINI_API_KEY", "NEWS_SUMMARY_ADMIN_PASSWORD"]


def test_create_backup_includes_postgres_json_export(monkeypatch, tmp_path):
    project_root = tmp_path
    backup_dir = tmp_path.parent / f"external_backups_{tmp_path.name}"
    table_rows = {table: [] for table in POSTGRES_BACKUP_TABLES}
    table_rows["press_releases"] = [
        {
            "id": 1,
            "source_id": "sample",
            "source_name": "테스트 군청",
            "region": "전남",
            "title": "PostgreSQL 백업 테스트",
            "url": "https://example.com/postgres-backup",
            "content": "본문",
            "published_at": "2026-07-08",
            "collected_at": "2026-07-08T09:00:00+09:00",
            "validation_status": "검증 완료",
            "validation_note": "원문 제목과 본문 구조를 확인했습니다.",
        }
    ]
    table_rows["app_metadata"] = [{"key": "sample", "value": "ok", "updated_at": "2026-07-08T09:00:00+09:00"}]

    class FakeCursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class FakePostgresConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def execute(self, sql):
            table = sql.split("FROM ", 1)[1].split(" ", 1)[0]
            return FakeCursor(table_rows[table])

    monkeypatch.setattr("news_summary.backup._connect_postgres", lambda database_url: FakePostgresConnection())

    backup_path = create_backup(project_root, "postgresql://example.invalid/news", backup_dir)

    assert backup_path.exists()
    with zipfile.ZipFile(backup_path, "r") as archive:
        names = archive.namelist()
        assert POSTGRES_EXPORT_ARCHIVE_NAME in names
        payload = json.loads(archive.read(POSTGRES_EXPORT_ARCHIVE_NAME))

    assert payload["format"] == "news-express-postgres-json-v1"
    assert payload["tables"]["press_releases"][0]["title"] == "PostgreSQL 백업 테스트"
    assert set(payload["tables"]) == set(POSTGRES_BACKUP_TABLES)
    verification = verify_backup(backup_path)
    assert verification["ok"] is True
    assert verification["checked_database_export"] is True
    assert restore_backup(project_root, backup_path, dry_run=True) == []


def test_restore_backup_rejects_path_traversal(tmp_path):
    backup_path = tmp_path / "bad.zip"
    with zipfile.ZipFile(backup_path, "w") as archive:
        archive.writestr("../outside.txt", "bad")

    restored = restore_backup(tmp_path, backup_path, dry_run=True)

    assert restored == []


def test_restore_backup_does_not_accept_file_prefix_matches(tmp_path):
    import zipfile

    backup_path = tmp_path / "prefix.zip"
    with zipfile.ZipFile(backup_path, "w") as archive:
        archive.writestr(".env.bad", "bad")
        archive.writestr("exports/article.md", "ok")

    restored = restore_backup(tmp_path, backup_path, dry_run=True)

    assert restored == ["exports/article.md"]


def test_verify_backup_reports_bad_zip(tmp_path):
    backup_path = tmp_path / "bad.zip"
    backup_path.write_text("not a zip", encoding="utf-8")

    verification = verify_backup(backup_path)

    assert verification["ok"] is False
    assert verification["status_label"] == "백업 확인 필요"
