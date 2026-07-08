from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_BACKUP_DIR = Path("data/backups")
BACKUP_FILE_PREFIX = "news-express"
POSTGRES_EXPORT_ARCHIVE_NAME = "data/postgres_export.json"
POSTGRES_BACKUP_TABLES = (
    "press_releases",
    "press_release_assets",
    "article_drafts",
    "app_metadata",
    "draft_history",
    "draft_generation_failures",
    "source_collection_runs",
    "visitor_access_logs",
)
ALLOWED_RESTORE_ROOTS = (
    ".env",
    "config/municipalities.yaml",
    "data/news_summary.sqlite",
    "data/writing_settings.json",
    "exports/",
)
ALLOWED_DATA_SUFFIXES = (".sqlite", ".json")


def create_backup(
    project_root: Path,
    db_path: Path | str,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
) -> Path:
    project_root = project_root.resolve()
    backup_dir = _resolve_backup_dir(project_root, backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = _unique_backup_path(backup_dir, timestamp)

    with tempfile.TemporaryDirectory() as tmp:
        temp_root = Path(tmp)
        with zipfile.ZipFile(backup_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            if _is_postgres_location(db_path):
                _add_postgres_export(archive, str(db_path))
            else:
                _add_sqlite_backup(archive, temp_root, project_root, _resolve_under(project_root, Path(db_path)))
            for relative in (".env", "data/writing_settings.json", "config/municipalities.yaml"):
                _add_file_if_exists(archive, project_root, relative)
            _add_directory_if_exists(archive, project_root, "exports")

    return backup_path


def verify_backup(backup_path: Path) -> dict[str, object]:
    backup_path = backup_path.resolve()
    if not backup_path.exists() or not backup_path.is_file():
        return {
            "ok": False,
            "status_label": "백업 없음",
            "message": "검증할 백업 파일이 없습니다.",
            "checked_sqlite": False,
        }

    try:
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            with zipfile.ZipFile(backup_path, "r") as archive:
                bad_member = archive.testzip()
                if bad_member:
                    return {
                        "ok": False,
                        "status_label": "백업 손상",
                        "message": f"압축 파일 안의 {bad_member} 항목이 손상됐습니다.",
                        "checked_sqlite": False,
                    }
                sqlite_members = [
                    member
                    for member in archive.infolist()
                    if not member.is_dir() and member.filename.replace("\\", "/").endswith(".sqlite")
                ]
                postgres_members = [
                    member
                    for member in archive.infolist()
                    if not member.is_dir() and member.filename.replace("\\", "/") == POSTGRES_EXPORT_ARCHIVE_NAME
                ]
                for member in sqlite_members[:1]:
                    extracted = temp_root / "verify.sqlite"
                    with archive.open(member) as source, extracted.open("wb") as target:
                        shutil.copyfileobj(source, target)
                    _verify_sqlite_database(extracted)
                    return {
                        "ok": True,
                        "status_label": "검증 정상",
                        "message": f"{backup_path.name} 압축과 SQLite 무결성을 확인했습니다.",
                        "checked_sqlite": True,
                    }
                for member in postgres_members[:1]:
                    with archive.open(member) as source:
                        payload = json.load(source)
                    _verify_postgres_export_payload(payload)
                    return {
                        "ok": True,
                        "status_label": "검증 정상",
                        "message": f"{backup_path.name} 압축과 PostgreSQL JSON 덤프 구조를 확인했습니다.",
                        "checked_sqlite": False,
                        "checked_database_export": True,
                    }
                return {
                    "ok": True,
                    "status_label": "검증 정상",
                    "message": f"{backup_path.name} 압축 파일을 확인했습니다.",
                    "checked_sqlite": False,
                    "checked_database_export": False,
                }
    except (OSError, sqlite3.Error, zipfile.BadZipFile, json.JSONDecodeError, ValueError) as exc:
        return {
            "ok": False,
            "status_label": "백업 확인 필요",
            "message": f"{type(exc).__name__}: {exc}",
            "checked_sqlite": False,
        }


def _unique_backup_path(backup_dir: Path, timestamp: str) -> Path:
    base_path = backup_dir / f"{BACKUP_FILE_PREFIX}-{timestamp}.zip"
    if not base_path.exists():
        return base_path
    for index in range(1, 1000):
        candidate = backup_dir / f"{BACKUP_FILE_PREFIX}-{timestamp}-{index}.zip"
        if not candidate.exists():
            return candidate
    raise FileExistsError("사용 가능한 백업 파일명을 만들 수 없습니다.")


def _verify_sqlite_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
    finally:
        conn.close()
    if not result or str(result[0]).lower() != "ok":
        detail = result[0] if result else "결과 없음"
        raise sqlite3.DatabaseError(f"SQLite integrity_check 실패: {detail}")


def restore_backup(
    project_root: Path,
    backup_path: Path,
    dry_run: bool = False,
) -> list[str]:
    project_root = project_root.resolve()
    backup_path = backup_path.resolve()
    restored: list[str] = []
    with zipfile.ZipFile(backup_path, "r") as archive:
        for member in archive.infolist():
            name = member.filename.replace("\\", "/")
            if member.is_dir():
                continue
            if not _is_allowed_restore_path(name):
                continue
            destination = (project_root / name).resolve()
            if not _is_within(project_root, destination):
                raise ValueError(f"백업 파일 경로가 안전하지 않습니다: {name}")
            restored.append(name)
            if dry_run:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.suffix == ".sqlite":
                _remove_sqlite_sidecars(destination)
            with archive.open(member) as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)
            if destination.suffix == ".sqlite":
                _remove_sqlite_sidecars(destination)
    return restored


def _add_sqlite_backup(archive: zipfile.ZipFile, temp_root: Path, project_root: Path, db_path: Path) -> None:
    if not db_path.exists():
        return
    temp_db = temp_root / "news_summary.sqlite"
    source = sqlite3.connect(db_path)
    try:
        target = sqlite3.connect(temp_db)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    archive.write(temp_db, _relative_archive_name(project_root, db_path))


def _add_postgres_export(archive: zipfile.ZipFile, database_url: str) -> None:
    exported_at = datetime.now(timezone.utc).isoformat()
    tables: dict[str, list[dict[str, Any]]] = {}
    with _connect_postgres(database_url) as conn:
        for table in POSTGRES_BACKUP_TABLES:
            order_column = "key" if table == "app_metadata" else "id"
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY {order_column}").fetchall()
            tables[table] = [dict(row) for row in rows]
    payload = {
        "format": "news-express-postgres-json-v1",
        "exported_at": exported_at,
        "tables": tables,
    }
    archive.writestr(
        POSTGRES_EXPORT_ARCHIVE_NAME,
        json.dumps(payload, ensure_ascii=False, default=str, indent=2),
    )


def _connect_postgres(database_url: str):
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ModuleNotFoundError as exc:  # pragma: no cover - PostgreSQL deployments install psycopg.
        raise RuntimeError("PostgreSQL 백업을 생성하려면 psycopg 패키지가 필요합니다.") from exc
    return psycopg.connect(database_url, row_factory=dict_row)


def _verify_postgres_export_payload(payload: object) -> None:
    if not isinstance(payload, dict):
        raise ValueError("PostgreSQL 덤프가 JSON 객체가 아닙니다.")
    if payload.get("format") != "news-express-postgres-json-v1":
        raise ValueError("PostgreSQL 덤프 형식이 올바르지 않습니다.")
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        raise ValueError("PostgreSQL 덤프 테이블 정보가 없습니다.")
    missing = [table for table in POSTGRES_BACKUP_TABLES if table not in tables]
    if missing:
        raise ValueError("PostgreSQL 덤프에 누락된 테이블이 있습니다: " + ", ".join(missing))
    for table, rows in tables.items():
        if not isinstance(rows, list):
            raise ValueError(f"PostgreSQL 덤프 테이블 형식이 올바르지 않습니다: {table}")


def _add_file_if_exists(archive: zipfile.ZipFile, project_root: Path, relative: str) -> None:
    path = project_root / relative
    if path.exists() and path.is_file():
        archive.write(path, relative.replace("\\", "/"))


def _add_directory_if_exists(archive: zipfile.ZipFile, project_root: Path, relative: str) -> None:
    path = project_root / relative
    if not path.exists() or not path.is_dir():
        return
    for file_path in path.rglob("*"):
        if file_path.is_file():
            archive.write(file_path, _relative_archive_name(project_root, file_path))


def _relative_archive_name(project_root: Path, path: Path) -> str:
    return path.resolve().relative_to(project_root).as_posix()


def _resolve_under(project_root: Path, path: Path) -> Path:
    resolved = (project_root / path).resolve() if not path.is_absolute() else path.resolve()
    if not _is_within(project_root, resolved):
        raise ValueError(f"프로젝트 밖 경로는 사용할 수 없습니다: {path}")
    return resolved


def _resolve_backup_dir(project_root: Path, path: Path) -> Path:
    path = Path(path)
    if path.is_absolute():
        return path.resolve()
    return _resolve_under(project_root, path)


def _is_postgres_location(value: Path | str) -> bool:
    return str(value).startswith(("postgresql://", "postgres://"))


def _is_within(root: Path, path: Path) -> bool:
    try:
        os.path.commonpath([str(root), str(path)]) == str(root)
    except ValueError:
        return False
    return os.path.commonpath([str(root), str(path)]) == str(root)


def _is_allowed_restore_path(name: str) -> bool:
    normalized = name.strip("/")
    if normalized == POSTGRES_EXPORT_ARCHIVE_NAME:
        return False
    if normalized.startswith("data/") and normalized.endswith(ALLOWED_DATA_SUFFIXES):
        return True
    for allowed in ALLOWED_RESTORE_ROOTS:
        if allowed.endswith("/"):
            if normalized.startswith(allowed):
                return True
        elif normalized == allowed:
            return True
    return False


def _remove_sqlite_sidecars(path: Path) -> None:
    if path.exists():
        try:
            conn = sqlite3.connect(path)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists():
            try:
                sidecar.unlink()
            except PermissionError:
                pass
