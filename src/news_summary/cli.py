from __future__ import annotations

import argparse

from .ops_logging import configure_logging, get_logger
from .settings import env_path, load_environment, load_sources
from .storage import Store


def main() -> None:
    load_environment()
    configure_logging()
    logger = get_logger("cli")
    parser = argparse.ArgumentParser(description="광주·전남 지자체 보도자료 수집 및 기사 초안 생성 도구")
    sub = parser.add_subparsers(dest="command", metavar="명령", required=True)

    sub.add_parser("init-db", help="데이터베이스를 준비합니다.")

    collect = sub.add_parser("collect", help="보도자료 원문을 수집합니다.")
    collect.add_argument("--limit", type=int, default=10, help="지자체별 최대 수집 건수")

    draft = sub.add_parser("draft", help="수집 원문으로 기사 초안을 만듭니다.")
    draft.add_argument("--limit", type=int, default=5, help="최대 초안 생성 건수")

    run = sub.add_parser("run", help="원문 수집과 초안 생성을 함께 실행합니다.")
    run.add_argument("--limit", type=int, default=10, help="지자체별 최대 처리 건수")

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

    serve = sub.add_parser("serve", help="로컬 검수 화면을 실행합니다.")
    serve.add_argument("--host", default="127.0.0.1", help="실행할 호스트")
    serve.add_argument("--port", type=int, default=5000, help="실행할 포트")

    sub.add_parser("show-sources", help="설정된 수집 소스를 보여줍니다.")
    args = parser.parse_args()

    store = Store(env_path("NEWS_SUMMARY_DB", "data/news_summary.sqlite"))
    config_path = env_path("NEWS_SUMMARY_CONFIG", "config/municipalities.yaml")

    if args.command == "init-db":
        store.init_db()
        logger.info("database initialized path=%s", store.path)
        print(f"데이터베이스 준비 완료: {store.path}")
    elif args.command == "collect":
        store.init_db()
        collect_command(store, config_path, args.limit)
    elif args.command == "draft":
        store.init_db()
        draft_command(store, args.limit)
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


def serve_command(host: str, port: int) -> None:
    from .scheduler import build_auto_collector_from_env
    from .web import create_app

    load_environment()
    logger = get_logger("cli")
    store = Store(env_path("NEWS_SUMMARY_DB", "data/news_summary.sqlite"))
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
