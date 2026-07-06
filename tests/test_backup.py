from pathlib import Path
import sqlite3

from news_summary.backup import create_backup, restore_backup, verify_backup


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


def test_restore_backup_rejects_path_traversal(tmp_path):
    import zipfile

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
