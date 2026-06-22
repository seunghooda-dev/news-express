from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path


DEFAULT_BACKUP_DIR = Path("data/backups")
BACKUP_FILE_PREFIX = "news-express"
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
    db_path: Path,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
) -> Path:
    project_root = project_root.resolve()
    backup_dir = _resolve_under(project_root, backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = _unique_backup_path(backup_dir, timestamp)

    with tempfile.TemporaryDirectory() as tmp:
        temp_root = Path(tmp)
        with zipfile.ZipFile(backup_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            _add_sqlite_backup(archive, temp_root, project_root, _resolve_under(project_root, db_path))
            for relative in (".env", "data/writing_settings.json", "config/municipalities.yaml"):
                _add_file_if_exists(archive, project_root, relative)
            _add_directory_if_exists(archive, project_root, "exports")

    return backup_path


def _unique_backup_path(backup_dir: Path, timestamp: str) -> Path:
    base_path = backup_dir / f"{BACKUP_FILE_PREFIX}-{timestamp}.zip"
    if not base_path.exists():
        return base_path
    for index in range(1, 1000):
        candidate = backup_dir / f"{BACKUP_FILE_PREFIX}-{timestamp}-{index}.zip"
        if not candidate.exists():
            return candidate
    raise FileExistsError("사용 가능한 백업 파일명을 만들 수 없습니다.")


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


def _is_within(root: Path, path: Path) -> bool:
    try:
        os.path.commonpath([str(root), str(path)]) == str(root)
    except ValueError:
        return False
    return os.path.commonpath([str(root), str(path)]) == str(root)


def _is_allowed_restore_path(name: str) -> bool:
    normalized = name.strip("/")
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
