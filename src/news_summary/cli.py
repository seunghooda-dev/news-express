from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
import time

from .ops_logging import configure_logging, get_logger
from .settings import env_database, env_path, load_environment, load_sources
from .storage import Store


def main() -> None:
    load_environment()
    configure_logging()
    logger = get_logger("cli")
    parser = argparse.ArgumentParser(description="광주·전남 지자체 보도자료 수집 및 기사 초안 생성 도구")
    sub = parser.add_subparsers(dest="command", metavar="명령", required=True)

    sub.add_parser("init-db", help="데이터베이스를 준비합니다.")

    collect = sub.add_parser("collect", help="보도자료 원문을 수집합니다.")
    collect.add_argument("--limit", type=int, default=30, help="지자체별 최대 수집 건수")

    draft = sub.add_parser("draft", help="수집 원문으로 기사 초안을 만듭니다.")
    draft.add_argument("--limit", type=int, default=5, help="최대 초안 생성 건수")

    draft_date = sub.add_parser("draft-date", help="특정 게시일 원문으로 기사 초안을 만듭니다.")
    draft_date.add_argument("date", help="초안을 만들 게시일. 예: 2026-06-26")
    draft_date.add_argument("--limit", type=int, default=250, help="최대 초안 생성 건수")
    draft_date.add_argument("--require-gemini", action="store_true", help="Gemini 성공 건만 초안으로 저장합니다.")
    draft_date.add_argument("--sleep-seconds", type=float, default=0.0, help="초안 생성 사이 대기 초")
    draft_date.add_argument("--wait-cooldown", action="store_true", help="Gemini 쿨다운이면 기다렸다가 재시도합니다.")
    draft_date.add_argument("--quota-retry-limit", type=int, default=0, help="한도 초과 후 쿨다운 대기 재시도 횟수")
    draft_date.add_argument("--newest-first", action="store_true", help="최신 원문부터 처리합니다.")

    run = sub.add_parser("run", help="원문 수집과 초안 생성을 함께 실행합니다.")
    run.add_argument("--limit", type=int, default=30, help="지자체별 최대 처리 건수")

    show = sub.add_parser("show-drafts", help="최근 기사 초안을 보여줍니다.")
    show.add_argument("--limit", type=int, default=10, help="표시할 초안 수")

    export = sub.add_parser("export", help="승인된 초안을 파일로 내보냅니다.")
    export.add_argument("--output-dir", default="exports", help="내보낼 폴더")

    backup = sub.add_parser("backup", help="운영 데이터를 zip 백업으로 저장합니다.")
    backup.add_argument("--output-dir", default="data/backups", help="백업 파일을 저장할 폴더")

    restore = sub.add_parser("restore", help="백업 zip에서 운영 데이터를 복구합니다.")
    restore.add_argument("backup_path", help="복구할 백업 zip 파일")
    restore.add_argument("--yes", action="store_true", help="확인 질문 없이 복구합니다.")
    restore.add_argument("--dry-run", action="store_true", help="복구 대상 파일만 확인합니다.")

    migrate_pg = sub.add_parser("migrate-sqlite-to-postgres", help="SQLite 데이터를 PostgreSQL로 이관합니다.")
    migrate_pg.add_argument("--sqlite-db", default=None, help="이관할 SQLite DB 경로")
    migrate_pg.add_argument("--database-url", default=None, help="대상 PostgreSQL DATABASE_URL")
    migrate_pg.add_argument("--replace", action="store_true", help="대상 PostgreSQL 데이터를 비우고 다시 이관합니다.")

    serve = sub.add_parser("serve", help="로컬 검수 화면을 실행합니다.")
    serve.add_argument("--host", default="127.0.0.1", help="실행할 호스트")
    serve.add_argument("--port", type=int, default=5000, help="실행할 포트")

    sub.add_parser("show-sources", help="설정된 수집 소스를 보여줍니다.")
    args = parser.parse_args()

    store = Store(env_database())
    config_path = env_path("NEWS_SUMMARY_CONFIG", "config/municipalities.yaml")

    if args.command == "init-db":
        store.init_db()
        logger.info("database initialized path=%s", store.display_location)
        print(f"데이터베이스 준비 완료: {store.display_location}")
    elif args.command == "collect":
        store.init_db()
        collect_command(store, config_path, args.limit)
    elif args.command == "draft":
        store.init_db()
        draft_command(store, args.limit)
    elif args.command == "draft-date":
        store.init_db()
        draft_date_command(
            store,
            args.date,
            args.limit,
            require_gemini=args.require_gemini,
            sleep_seconds=args.sleep_seconds,
            wait_cooldown=args.wait_cooldown,
            quota_retry_limit=args.quota_retry_limit,
            newest_first=args.newest_first,
        )
    elif args.command == "run":
        store.init_db()
        collect_command(store, config_path, args.limit)
        draft_command(store, args.limit)
    elif args.command == "show-drafts":
        show_drafts(store, args.limit)
    elif args.command == "show-sources":
        show_sources(config_path)
    elif args.command == "export":
        export_command(store, env_path("NEWS_SUMMARY_EXPORT_DIR", args.output_dir))
    elif args.command == "backup":
        backup_command(store, env_path("NEWS_SUMMARY_DB", "data/news_summary.sqlite"), env_path("NEWS_SUMMARY_BACKUP_DIR", args.output_dir))
    elif args.command == "restore":
        restore_command(args.backup_path, args.yes, args.dry_run)
    elif args.command == "migrate-sqlite-to-postgres":
        sqlite_db = env_path("NEWS_SUMMARY_DB", "data/news_summary.sqlite") if args.sqlite_db is None else args.sqlite_db
        database_url = args.database_url or os.getenv("DATABASE_URL") or os.getenv("NEWS_SUMMARY_DATABASE_URL")
        migrate_sqlite_to_postgres_command(sqlite_db, database_url, replace=args.replace)
    elif args.command == "serve":
        logger.info("serve command host=%s port=%s", args.host, args.port)
        serve_command(args.host, args.port)


def collect_command(store: Store, config_path, limit: int) -> None:
    from .service import collect_enabled_sources

    for message in collect_enabled_sources(store, config_path, limit):
        print(message)


def draft_command(store: Store, limit: int) -> None:
    from .service import draft_pending_releases

    for message in draft_pending_releases(store, limit):
        print(message)


def draft_date_command(
    store: Store,
    published_date: str,
    limit: int,
    *,
    require_gemini: bool,
    sleep_seconds: float,
    wait_cooldown: bool,
    quota_retry_limit: int,
    newest_first: bool,
) -> None:
    from .service import draft_pending_releases_for_date, gemini_cooldown_until

    try:
        datetime.strptime(published_date, "%Y-%m-%d")
    except ValueError as exc:
        raise SystemExit("게시일은 YYYY-MM-DD 형식으로 입력하세요.") from exc

    attempts = 0
    oldest_first = not newest_first
    while True:
        cooldown_until = gemini_cooldown_until(store) if require_gemini else None
        if cooldown_until:
            if not wait_cooldown:
                print(f"Gemini 쿨다운 중: {cooldown_until.isoformat()}까지 초안 생성을 보류합니다.")
                return
            _wait_until(cooldown_until)

        pending_before = store.count_pending_press_releases_for_date(published_date)
        if pending_before <= 0:
            print(f"{published_date} 미변환 원문이 없습니다.")
            return

        print(f"{published_date} 미변환 원문 {pending_before}건 처리 시작")
        for message in draft_pending_releases_for_date(
            store,
            published_date,
            limit=limit,
            require_gemini=require_gemini,
            oldest_first=oldest_first,
            sleep_seconds=sleep_seconds,
        ):
            print(message)

        pending_after = store.count_pending_press_releases_for_date(published_date)
        print(f"{published_date} 남은 미변환 원문 {pending_after}건")
        if pending_after <= 0:
            return

        cooldown_until = gemini_cooldown_until(store) if require_gemini else None
        if not cooldown_until or not wait_cooldown or attempts >= quota_retry_limit:
            return
        attempts += 1


def _wait_until(target: datetime) -> None:
    target_utc = target.astimezone(timezone.utc)
    while True:
        remaining = (target_utc - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            return
        print(f"Gemini 쿨다운 대기 중: {int(remaining)}초 남음")
        time.sleep(min(60.0, max(1.0, remaining)))


def show_drafts(store: Store, limit: int) -> None:
    print("최근 초안")
    print("번호 | 상태 | 제목 | 출처")
    for row in store.recent_drafts(limit):
        print(f"{row['id']} | {_status_label(row['status'])} | {row['title']} | {row['source_name']}")


def show_sources(config_path) -> None:
    print("수집 소스")
    print("식별자 | 사용 | 방식 | 이름")
    for source in load_sources(config_path):
        enabled = "예" if source.enabled else "아니오"
        print(f"{source.id} | {enabled} | {_type_label(source.type)} | {source.name}")


def export_command(store: Store, output_dir) -> None:
    from .exporter import export_approved

    store.init_db()
    markdown_path, csv_path, count = export_approved(store, output_dir)
    print(f"승인 기사 {count}건을 내보냈습니다.")
    print(f"마크다운 파일: {markdown_path}")
    print(f"표 파일: {csv_path}")


def backup_command(store: Store, db_path, output_dir) -> None:
    from pathlib import Path

    from .backup import create_backup

    if store.is_postgres:
        raise SystemExit("PostgreSQL 모드에서는 이 SQLite zip 백업 명령을 사용할 수 없습니다. 클라우드 DB 백업/스냅샷을 사용하세요.")
    store.init_db()
    backup_path = create_backup(Path.cwd(), Path(db_path), Path(output_dir))
    print(f"백업 완료: {backup_path}")


def restore_command(backup_path: str, yes: bool, dry_run: bool) -> None:
    from pathlib import Path

    from .backup import restore_backup

    if not dry_run and not yes:
        raise SystemExit("복구하려면 --yes 옵션을 함께 지정하세요.")
    restored = restore_backup(Path.cwd(), Path(backup_path), dry_run=dry_run)
    if dry_run:
        print("복구 대상 파일:")
    else:
        print("복구 완료:")
    for name in restored:
        print(f"- {name}")


def migrate_sqlite_to_postgres_command(sqlite_db, database_url: str | None, *, replace: bool) -> None:
    from pathlib import Path

    from .migrate_postgres import migrate_sqlite_to_postgres

    if not database_url:
        raise SystemExit("DATABASE_URL 또는 NEWS_SUMMARY_DATABASE_URL을 설정하거나 --database-url을 지정하세요.")
    counts = migrate_sqlite_to_postgres(Path(sqlite_db), database_url, replace=replace)
    print("PostgreSQL 이관 완료:")
    for table, count in counts.items():
        print(f"- {table}: {count}건")


def serve_command(host: str, port: int) -> None:
    from .scheduler import build_auto_collector_from_env
    from .web import create_app

    load_environment()
    logger = get_logger("cli")
    store = Store(env_database())
    config_path = env_path("NEWS_SUMMARY_CONFIG", "config/municipalities.yaml")
    store.init_db()
    auto_collector = build_auto_collector_from_env(store, config_path)
    app = create_app()
    app.config["AUTO_COLLECTOR"] = auto_collector
    if auto_collector and auto_collector.snapshot().enabled:
        auto_collector.start()
        logger.info("auto collector started host=%s port=%s", host, port)
        print(
            "자동 수집 시작: 매시간 정각마다 전체 기관 원문 수집 후 Gemini 초안을 검수 대기에 추가합니다."
        )
    elif auto_collector:
        logger.info("auto collector prepared but disabled host=%s port=%s", host, port)
        print("자동 수집 꺼짐: 운영 관리 화면에서 다시 켤 수 있습니다.")
    logger.info("flask app starting host=%s port=%s", host, port)
    app.run(host=host, port=port, debug=False)


def _status_label(status: str) -> str:
    labels = {
        "needs_review": "검수 대기",
        "approved": "승인",
        "rejected": "반려",
    }
    return labels.get(status, status)


def _type_label(source_type: str) -> str:
    labels = {
        "html_board": "게시판",
        "json_board": "자료 게시판",
        "rss": "구독 피드",
    }
    return labels.get(source_type, source_type)


if __name__ == "__main__":
    main()
