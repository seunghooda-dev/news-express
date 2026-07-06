from __future__ import annotations

import ipaddress
import json
import re
import os
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from flask import Flask, Response, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.exceptions import HTTPException

from .auth import ADMIN_PASSWORD_HASH_KEY, auth_config, set_admin_password, verify_admin_password
from .backup import create_backup, restore_backup
from .exporter import export_approved
from .ops_logging import configure_logging, get_logger
from .scheduler import AUTO_COLLECT_STATUS_KEY
from .service import (
    GEMINI_COOLDOWN_REASON_KEY,
    business_days_between,
    collection_retention_cutoff_date,
    collection_retention_days,
    collect_and_draft_cycle,
    collect_enabled_sources,
    draft_pending_releases,
    gemini_cooldown_until,
    has_collection_non_business_day_between,
    is_collection_business_day,
    mark_gemini_cooldown,
    retention_holidays,
)
from .settings import PROJECT_ROOT, env_database, env_path, load_environment, load_sources
from .storage import Store
from .writing_settings import DEFAULT_WRITING_SETTINGS, custom_prompt_section, load_writing_settings, save_writing_settings
from .writer import GeminiRefineError, current_gemini_models, refine_draft_with_gemini


VALID_STATUSES = {"needs_review", "approved", "rejected"}
STATUS_ORDER = ("needs_review", "approved", "rejected")
STATUS_LABELS = {
    "needs_review": "검수 대기",
    "approved": "승인",
    "rejected": "반려",
}
LOCAL_TZ = timezone(timedelta(hours=9))
DATE_RE = re.compile(r"(20\d{2})[./-](\d{1,2})[./-](\d{1,2})")
DATETIME_RE = re.compile(r"(20\d{2})[./-](\d{1,2})[./-](\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?")
CLOUDFLARE_URL_RE = re.compile(r"https://[-a-zA-Z0-9]+\.trycloudflare\.com")
GEMINI_USAGE_RESET_AT_KEY = "gemini_usage_reset_at"
AUTH_EXEMPT_ENDPOINTS = {"favicon", "healthz", "login", "logout", "admin_setup", "static"}
OPERATIONS_ADMIN_PASSWORD_UNLOCKED_KEY = "operations_admin_password_unlocked"
LIST_PAGE_SIZE = 50
MAX_LIST_LIMIT = 500
DASHBOARD_PENDING_LIMIT = 20
DASHBOARD_RELEASE_LIMIT = 10
FILTER_FETCH_LIMIT = 1000
REGION_DISPLAY_PREFIXES = ("전남광주통합특별시", "전남광주특별시")


def _slow_request_threshold_seconds() -> float:
    raw_value = os.getenv("NEWS_SUMMARY_SLOW_REQUEST_SECONDS", "2.5")
    try:
        return max(0.5, float(raw_value))
    except ValueError:
        return 2.5


def create_app() -> Flask:
    load_environment()
    log_path = configure_logging()
    logger = get_logger("web")
    app = Flask(__name__)
    app.secret_key = os.getenv("NEWS_SUMMARY_SECRET_KEY", "local-news-summary-review")
    app.jinja_env.globals["status_label"] = status_label
    app.jinja_env.globals["status_badge_class"] = status_badge_class
    app.jinja_env.globals["model_label"] = model_label
    app.jinja_env.globals["model_badge_class"] = model_badge_class
    app.jinja_env.globals["interval_label"] = interval_label
    app.jinja_env.globals["review_flags"] = review_flags
    app.jinja_env.globals["approval_checks"] = approval_checks
    app.jinja_env.globals["body_character_count"] = body_character_count
    app.jinja_env.globals["change_type_label"] = change_type_label
    app.jinja_env.globals["file_size_label"] = file_size_label
    app.jinja_env.globals["region_display_label"] = region_display_label
    app.jinja_env.globals["source_display_label"] = source_display_label
    app.jinja_env.filters["date_label"] = format_datetime_label

    store = Store(env_database())
    config_path = env_path("NEWS_SUMMARY_CONFIG", "config/municipalities.yaml")
    export_dir = env_path("NEWS_SUMMARY_EXPORT_DIR", "exports")
    backup_dir = env_path("NEWS_SUMMARY_BACKUP_DIR", "data/backups")
    store.init_db()
    source_options = load_sources(config_path)
    store.sync_source_metadata(source_options)
    app.config["NEWS_SUMMARY_LOG_PATH"] = log_path

    @app.context_processor
    def inject_auth_state():
        return {
            "auth_state": auth_config(store),
            "admin_authenticated": bool(session.get("admin_authenticated")),
        }

    @app.after_request
    def add_security_headers(response: Response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.endpoint in {"operations", "ops_logs", "recrawl_status"}:
            response.headers.setdefault("Cache-Control", "no-store")
        try:
            _record_visitor_access(store, response.status_code)
        except Exception as exc:  # noqa: BLE001 - access logging must never block the page response.
            logger.warning("visitor access log failed path=%s error=%s", request.path, exc)
        started_at = getattr(g, "request_started_at", None)
        if started_at is not None:
            elapsed = time.perf_counter() - started_at
            threshold = _slow_request_threshold_seconds()
            if elapsed >= threshold:
                logger.warning(
                    "slow web request method=%s path=%s endpoint=%s status=%s elapsed=%.3fs threshold=%.3fs",
                    request.method,
                    request.path,
                    request.endpoint,
                    response.status_code,
                    elapsed,
                    threshold,
                )
        return response

    @app.before_request
    def open_store_connection_scope():
        g.request_started_at = time.perf_counter()
        endpoint = request.endpoint or ""
        if endpoint == "static":
            return None
        scope = store.reusable_connection_scope()
        scope.__enter__()
        g.store_connection_scope = scope
        return None

    @app.teardown_request
    def close_store_connection_scope(exc: BaseException | None):
        scope = getattr(g, "store_connection_scope", None)
        if not scope:
            return None
        if exc is None:
            scope.__exit__(None, None, None)
        else:
            scope.__exit__(type(exc), exc, exc.__traceback__)
        return None

    @app.before_request
    def require_admin_login():
        endpoint = request.endpoint or ""
        if endpoint in AUTH_EXEMPT_ENDPOINTS:
            return None
        config = auth_config(store)
        if not config.enabled:
            return None
        if config.setup_required:
            return redirect(url_for("admin_setup", next=_current_next_path()))
        if session.get("admin_authenticated"):
            return None
        return redirect(url_for("login", next=_current_next_path()))

    @app.errorhandler(Exception)
    def handle_unexpected_error(exc: Exception):
        if isinstance(exc, HTTPException):
            return exc
        logger.exception("unhandled web error method=%s path=%s", request.method, request.path)
        return "서버 오류가 발생했습니다. 운영 로그를 확인하세요.", 500

    @app.get("/")
    def dashboard():
        selected_regions = _selected_regions(config_path)
        pending_drafts = _draft_rows_for_listing(
            store,
            status="needs_review",
            selected_regions=selected_regions,
            limit=FILTER_FETCH_LIMIT,
            include_original_content=False,
        )
        recent_releases = _press_release_rows_for_listing(
            store,
            selected_regions=selected_regions,
            limit=DASHBOARD_RELEASE_LIMIT + 1,
        )
        source_summaries = _filter_source_summaries_by_regions(
            _source_summaries(store, config_path),
            selected_regions,
        )
        auto_collector = app.config.get("AUTO_COLLECTOR")
        duplicate_titles = _duplicate_titles(store)
        attention_count = sum(1 for draft in pending_drafts if review_flags(draft, duplicate_titles))
        auto_status = auto_collector.snapshot() if auto_collector else None
        return render_template(
            "dashboard.html",
            counts=_counts_for_regions(store, selected_regions) if selected_regions else store.counts(),
            pending_drafts=pending_drafts[: DASHBOARD_PENDING_LIMIT + 1],
            recent_releases=recent_releases,
            auto_collector_status=auto_status,
            source_summaries=source_summaries,
            duplicate_titles=duplicate_titles,
            attention_count=attention_count,
            region_options=_region_options(config_path),
            selected_regions=selected_regions,
            region_filter_hidden={},
            region_reset_url=url_for("dashboard"),
        )

    @app.get("/favicon.ico")
    def favicon():
        return Response(status=204)

    @app.get("/healthz")
    def healthz():
        try:
            with store.connect() as conn:
                conn.execute("SELECT 1").fetchone()
        except Exception as exc:  # noqa: BLE001 - health endpoint should return a clear degraded state.
            logger.warning("health check failed error=%s", exc)
            return jsonify({"ok": False, "database": "error"}), 503
        return jsonify({"ok": True, "database": "ok"})

    @app.route("/login", methods=["GET", "POST"])
    def login():
        config = auth_config(store)
        if not config.enabled:
            flash("관리자 비밀번호가 아직 설정되지 않아 로컬 잠금이 비활성화되어 있습니다.")
            return redirect(url_for("admin_setup"))
        if config.setup_required:
            return redirect(url_for("admin_setup", next=_safe_next()))
        if request.method == "POST":
            password = request.form.get("password") or ""
            if verify_admin_password(store, password):
                session["admin_authenticated"] = True
                logger.info("admin login succeeded remote_addr=%s", _masked_request_ip())
                return redirect(_safe_next())
            logger.warning("admin login failed remote_addr=%s", _masked_request_ip())
            flash("관리자 비밀번호가 올바르지 않습니다.")
        return render_template("login.html", next_url=_safe_next())

    @app.post("/logout")
    def logout():
        session.pop("admin_authenticated", None)
        flash("로그아웃했습니다.")
        return redirect(url_for("login"))

    @app.route("/admin/setup", methods=["GET", "POST"])
    def admin_setup():
        config = auth_config(store)
        if config.source == "environment":
            flash(".env의 관리자 비밀번호 설정이 우선 적용 중입니다.")
            return redirect(url_for("dashboard") if session.get("admin_authenticated") else url_for("login"))
        if config.enabled and not config.setup_required and not session.get("admin_authenticated"):
            return redirect(url_for("login", next=url_for("admin_setup")))
        if request.method == "POST":
            password = request.form.get("password") or ""
            confirm = request.form.get("confirm_password") or ""
            if len(password) < 8:
                flash("관리자 비밀번호는 8자 이상이어야 합니다.")
            elif password != confirm:
                flash("비밀번호 확인이 일치하지 않습니다.")
            else:
                set_admin_password(store, password)
                session["admin_authenticated"] = True
                logger.info("admin password configured remote_addr=%s", _masked_request_ip())
                flash("관리자 로그인을 활성화했습니다.")
                return redirect(url_for("dashboard"))
        return render_template("admin_setup.html", auth_state=config, next_url=_safe_next())

    @app.get("/ops-logs")
    def ops_logs():
        log_path = Path(app.config["NEWS_SUMMARY_LOG_PATH"])
        lines = _recent_log_lines(log_path, limit=250)
        active_log_tab = request.args.get("tab") or "errors"
        log_tabs = _ops_log_tabs(lines)
        if active_log_tab not in {tab["id"] for tab in log_tabs}:
            active_log_tab = "errors"
        active_log_lines = next(tab["lines"] for tab in log_tabs if tab["id"] == active_log_tab)
        return render_template(
            "ops_logs.html",
            log_path=log_path,
            log_lines=lines,
            log_tabs=log_tabs,
            active_log_tab=active_log_tab,
            active_log_lines=active_log_lines,
        )

    @app.get("/operations")
    def operations():
        auto_collector = app.config.get("AUTO_COLLECTOR")
        auto_status = auto_collector.snapshot() if auto_collector else None
        admin_password_source = _configured_admin_password_source(store)
        admin_password_configured = bool(admin_password_source)
        return render_template(
            "operations.html",
            auto_collector_status=auto_status,
            retention_policy=_retention_policy_summary(),
            pending_queue=store.pending_press_release_summary(),
            visitor_access=_visitor_access_overview(store),
            cloudflare_tunnel=_cloudflare_quick_tunnel_status(),
            backup_dir=backup_dir,
            backup_files=_backup_files(backup_dir),
            db_path=store.display_location,
            log_path=Path(app.config["NEWS_SUMMARY_LOG_PATH"]),
            admin_password_source=admin_password_source,
            admin_password_configured=admin_password_configured,
            admin_password_unlocked=bool(
                admin_password_configured and session.get(OPERATIONS_ADMIN_PASSWORD_UNLOCKED_KEY)
            ),
        )

    @app.post("/operations/auto-collect")
    def update_auto_collect():
        auto_collector = app.config.get("AUTO_COLLECTOR")
        if not auto_collector:
            flash("자동 수집 컨트롤러가 준비되지 않았습니다. 프로그램을 다시 실행해 주세요.")
            return redirect(url_for("operations"))
        enabled = request.form.get("enabled") == "true"
        auto_collector.set_enabled(enabled)
        logger.info("auto collector setting changed enabled=%s", enabled)
        flash("자동 수집을 켰습니다." if enabled else "자동 수집을 껐습니다.")
        return redirect(url_for("operations"))

    @app.post("/operations/admin-password/unlock")
    def unlock_admin_password_panel():
        source = _configured_admin_password_source(store)
        if source == "environment":
            flash(".env의 관리자 비밀번호 설정이 우선 적용 중이라 화면에서 변경할 수 없습니다.")
            return redirect(url_for("operations"))
        if not source:
            flash("관리자 비밀번호를 먼저 설정하세요.")
            return redirect(url_for("admin_setup"))

        current_password = request.form.get("current_password") or ""
        if verify_admin_password(store, current_password):
            session[OPERATIONS_ADMIN_PASSWORD_UNLOCKED_KEY] = True
            logger.info("admin password panel unlocked remote_addr=%s", _masked_request_ip())
            flash("관리자 비밀번호 변경 입력칸을 열었습니다.")
        else:
            logger.warning("admin password panel unlock failed remote_addr=%s", _masked_request_ip())
            flash("현재 관리자 비밀번호가 올바르지 않습니다.")
        return redirect(url_for("operations"))

    @app.post("/operations/admin-password")
    def change_admin_password():
        source = _configured_admin_password_source(store)
        if source == "environment":
            flash(".env의 관리자 비밀번호 설정이 우선 적용 중이라 화면에서 변경할 수 없습니다.")
            return redirect(url_for("operations"))
        if not source:
            flash("관리자 비밀번호를 먼저 설정하세요.")
            return redirect(url_for("admin_setup"))

        current_password = request.form.get("current_password") or ""
        new_password = request.form.get("new_password") or ""
        confirm_password = request.form.get("confirm_password") or ""
        if not session.get(OPERATIONS_ADMIN_PASSWORD_UNLOCKED_KEY) and not verify_admin_password(store, current_password):
            logger.warning("admin password change failed remote_addr=%s reason=current_password", _masked_request_ip())
            flash("현재 관리자 비밀번호가 올바르지 않습니다.")
        elif len(new_password) < 8:
            flash("새 관리자 비밀번호는 8자 이상이어야 합니다.")
        elif new_password != confirm_password:
            flash("새 비밀번호 확인이 일치하지 않습니다.")
        else:
            set_admin_password(store, new_password)
            session["admin_authenticated"] = True
            session.pop(OPERATIONS_ADMIN_PASSWORD_UNLOCKED_KEY, None)
            logger.info("admin password changed remote_addr=%s", _masked_request_ip())
            flash("관리자 비밀번호를 변경했습니다. 다음 로그인부터 새 비밀번호를 사용하세요.")
        return redirect(url_for("operations"))

    @app.post("/operations/backup")
    def create_backup_route():
        if store.is_postgres:
            flash("PostgreSQL 모드에서는 SQLite zip 백업 대신 클라우드 DB 백업/스냅샷을 사용하세요.")
            return redirect(url_for("operations"))
        backup_path = create_backup(PROJECT_ROOT, store.path, backup_dir)
        logger.info("backup created path=%s", backup_path)
        flash(f"백업을 생성했습니다: {backup_path.name}")
        return redirect(url_for("operations"))

    @app.get("/operations/backups/<path:filename>")
    def download_backup(filename: str):
        backup_path = _safe_backup_file(backup_dir, filename)
        if not backup_path:
            flash("백업 파일을 찾을 수 없습니다.")
            return redirect(url_for("operations"))
        return send_file(backup_path, as_attachment=True, download_name=backup_path.name)

    @app.post("/operations/restore")
    def restore_backup_route():
        backup_path = _safe_backup_file(backup_dir, request.form.get("backup_name") or "")
        if not backup_path:
            flash("복구할 백업 파일을 선택하세요.")
            return redirect(url_for("operations"))

        restored = restore_backup(PROJECT_ROOT, backup_path, dry_run=True)
        if not restored:
            flash("복구 가능한 항목이 없는 백업 파일입니다.")
            return redirect(url_for("operations"))

        action = request.form.get("action") or "preview"
        if action == "preview":
            flash("복구 대상: " + ", ".join(restored))
            return redirect(url_for("operations"))

        if request.form.get("confirm_restore") != "yes":
            flash("복구를 실행하려면 확인 체크박스를 선택해야 합니다.")
            return redirect(url_for("operations"))

        auto_collector = app.config.get("AUTO_COLLECTOR")
        if auto_collector:
            auto_collector.set_enabled(False)
        safety_backup = create_backup(PROJECT_ROOT, store.path, backup_dir)
        restored = restore_backup(PROJECT_ROOT, backup_path, dry_run=False)
        store.init_db()
        logger.warning(
            "backup restored backup=%s restored=%s safety_backup=%s",
            backup_path,
            restored,
            safety_backup,
        )
        flash(f"백업을 복구했습니다. 복구 전 안전 백업: {safety_backup.name}")
        return redirect(url_for("operations"))

    @app.get("/gemini-usage")
    def gemini_usage():
        auto_collector = app.config.get("AUTO_COLLECTOR")
        auto_status = auto_collector.snapshot() if auto_collector else None
        return render_template(
            "gemini_usage.html",
            gemini_usage=_gemini_usage_summary(store, auto_status),
            pending_queue=store.pending_press_release_summary(),
        )

    @app.post("/gemini-usage/reset")
    def reset_gemini_usage():
        store.set_app_metadata(GEMINI_USAGE_RESET_AT_KEY, datetime.now(timezone.utc).isoformat())
        flash("Gemini 로컬 사용량을 초기화했습니다.")
        return redirect(url_for("gemini_usage"))

    @app.get("/drafts")
    def drafts():
        status = request.args.get("status") or None
        if status and status not in VALID_STATUSES:
            status = None
        target_date = _parse_date(request.args.get("date"))
        query = (request.args.get("q") or "").strip()
        review_filter = (request.args.get("review") or "").strip()
        source_filter = (request.args.get("source") or "").strip()
        selected_regions = _selected_regions(config_path)
        display_limit = _list_display_limit()
        needs_original_content = bool(query or review_filter in {"application", "event", "support"})
        draft_rows = _draft_rows_for_listing(
            store,
            status=status,
            selected_regions=selected_regions,
            source_filter=source_filter,
            target_date=target_date,
            query=query,
            limit=FILTER_FETCH_LIMIT if review_filter else display_limit + 1,
            include_original_content=needs_original_content,
        )
        duplicate_titles = _duplicate_titles(store)
        if review_filter:
            draft_rows = _filter_drafts_by_review(draft_rows, review_filter, duplicate_titles)
        has_more = display_limit < MAX_LIST_LIMIT and len(draft_rows) > display_limit
        draft_rows = draft_rows[:display_limit]
        region_filter_hidden = _clean_query_args(
            status=status,
            date=target_date.isoformat() if target_date else "",
            review=review_filter,
            source=source_filter,
            q=query,
        )
        return render_template(
            "drafts.html",
            drafts=draft_rows,
            status=status,
            date_filter=target_date,
            query=query,
            review_filter=review_filter,
            source_filter=source_filter,
            duplicate_titles=duplicate_titles,
            source_options=load_sources(config_path),
            region_options=_region_options(config_path),
            selected_regions=selected_regions,
            region_filter_hidden=region_filter_hidden,
            region_reset_url=url_for("drafts", **region_filter_hidden),
            page_title=_drafts_page_title(status, target_date, review_filter, source_filter, config_path),
            displayed_count=len(draft_rows),
            has_more=has_more,
            more_url=_load_more_url("drafts", display_limit + LIST_PAGE_SIZE) if has_more else "",
        )

    @app.get("/drafts/next")
    def next_review_draft():
        next_draft_id = _next_review_draft_id(store)
        if not next_draft_id:
            flash("검수할 대기 초안이 없습니다.")
            return redirect(url_for("drafts", status="needs_review"))
        return redirect(url_for("draft_detail", draft_id=next_draft_id))

    @app.get("/press-releases")
    def press_releases():
        selected_regions = _selected_regions(config_path)
        display_limit = _list_display_limit()
        releases = _press_release_rows_for_listing(
            store,
            selected_regions=selected_regions,
            limit=display_limit + 1,
        )
        has_more = display_limit < MAX_LIST_LIMIT and len(releases) > display_limit
        releases = releases[:display_limit]
        return render_template(
            "press_releases.html",
            press_releases=releases,
            region_options=_region_options(config_path),
            selected_regions=selected_regions,
            region_filter_hidden={},
            region_reset_url=url_for("press_releases"),
            displayed_count=len(releases),
            has_more=has_more,
            more_url=_load_more_url("press_releases", display_limit + LIST_PAGE_SIZE) if has_more else "",
        )

    @app.get("/sources/<source_id>")
    def source_detail(source_id: str):
        source = _source_by_id(config_path, source_id)
        if not source:
            flash("기관 정보를 찾을 수 없습니다.")
            return redirect(url_for("dashboard"))
        summary = _source_summary_by_id(store, config_path, source_id)
        return render_template(
            "source_detail.html",
            source=source,
            summary=summary,
            recent_releases=store.press_releases_by_source(source_id, limit=20),
            recent_drafts=store.drafts_by_source(source_id, limit=20),
            pending_drafts=store.drafts_by_source(source_id, status="needs_review", limit=20),
            duplicate_titles=_duplicate_titles(store),
        )

    @app.get("/drafts/<int:draft_id>")
    def draft_detail(draft_id: int):
        draft = store.get_draft(draft_id)
        if not draft:
            flash("초안을 찾을 수 없습니다.")
            return redirect(url_for("dashboard"))
        duplicate_titles = _duplicate_titles(store)
        return render_template(
            "draft_detail.html",
            draft=draft,
            statuses=STATUS_ORDER,
            duplicate_titles=duplicate_titles,
            checks=approval_checks(draft, duplicate_titles),
            next_review_draft_id=_next_review_draft_id(store, current_id=draft_id),
            gemini_cooldown_until=gemini_cooldown_until(store),
            draft_history=store.draft_history(draft_id),
        )

    @app.get("/writing-settings")
    def writing_settings():
        settings = load_writing_settings()
        return render_template(
            "writing_settings.html",
            settings=settings,
            prompt_preview=custom_prompt_section(settings),
        )

    @app.post("/writing-settings")
    def update_writing_settings():
        if request.form.get("action") == "reset":
            save_writing_settings(DEFAULT_WRITING_SETTINGS)
            flash("기사 설정을 기본값으로 되돌렸습니다.")
        else:
            save_writing_settings(request.form)
            flash("기사 설정을 저장했습니다. 다음 Gemini 초안 생성부터 적용됩니다.")
        return redirect(url_for("writing_settings"))

    @app.post("/drafts/<int:draft_id>")
    def update_draft(draft_id: int):
        action = request.form.get("action") or ""
        next_after_save = action in {"save_next", "approved_next", "rejected_next"}
        if action in {"approved", "approved_next"}:
            status = "approved"
        elif action in {"rejected", "rejected_next"}:
            status = "rejected"
        else:
            status = request.form.get("status", "needs_review")
        if status not in VALID_STATUSES:
            status = "needs_review"
        store.update_draft(
            draft_id=draft_id,
            title=request.form.get("title", "").strip(),
            body=request.form.get("body", "").strip(),
            review_note=request.form.get("review_note", "").strip(),
            status=status,
            change_type=_draft_change_type(action, status),
        )
        logger.info("draft updated draft_id=%s status=%s", draft_id, status)
        flash("초안을 저장했습니다.")
        if next_after_save:
            next_draft_id = _next_review_draft_id(store, current_id=draft_id)
            if next_draft_id:
                return redirect(url_for("draft_detail", draft_id=next_draft_id))
            flash("다음 검수 대기 초안이 없습니다.")
            return redirect(url_for("drafts", status="needs_review"))
        return redirect(url_for("draft_detail", draft_id=draft_id))

    @app.post("/drafts/<int:draft_id>/history/<int:history_id>/restore")
    def restore_draft_history(draft_id: int, history_id: int):
        history = store.get_draft_history_item(draft_id, history_id)
        if not history:
            flash("복구할 이력을 찾을 수 없습니다.")
            return redirect(url_for("draft_detail", draft_id=draft_id))
        store.update_draft(
            draft_id=draft_id,
            title=history["title"],
            body=history["body"],
            review_note=history["review_note"],
            status=history["status"],
            model=history["model"],
            change_type="history_restore",
        )
        logger.info("draft history restored draft_id=%s history_id=%s", draft_id, history_id)
        flash("선택한 이전 버전으로 복구했습니다.")
        return redirect(url_for("draft_detail", draft_id=draft_id))

    @app.post("/drafts/<int:draft_id>/restore-initial")
    def restore_initial_draft(draft_id: int):
        draft = store.get_draft(draft_id)
        if not draft:
            flash("초안을 찾을 수 없습니다.")
            return redirect(url_for("dashboard"))

        status = request.form.get("status") or draft["status"]
        if status not in VALID_STATUSES:
            status = draft["status"]
        store.restore_initial_draft(draft_id, status=status)
        logger.info("draft restored draft_id=%s status=%s", draft_id, status)
        flash("처음 Gemini가 제시한 초안으로 복구했습니다.")
        return redirect(url_for("draft_detail", draft_id=draft_id))

    @app.post("/drafts/<int:draft_id>/refine")
    def refine_draft(draft_id: int):
        draft = store.get_draft(draft_id)
        if not draft:
            flash("초안을 찾을 수 없습니다.")
            return redirect(url_for("dashboard"))

        cooldown_until = gemini_cooldown_until(store)
        if cooldown_until:
            logger.info("gemini refine skipped cooldown draft_id=%s until=%s", draft_id, cooldown_until.isoformat())
            flash(_gemini_refine_cooldown_message(cooldown_until))
            return redirect(url_for("draft_detail", draft_id=draft_id))

        instruction = (request.form.get("refine_instruction") or "").strip()
        if not instruction:
            instruction = (request.form.get("preset_instruction") or "").strip()
        if not instruction:
            flash("Gemini에게 전달할 다듬기 방향을 입력하세요.")
            return redirect(url_for("draft_detail", draft_id=draft_id))

        status = request.form.get("status") or draft["status"]
        if status not in VALID_STATUSES:
            status = draft["status"]

        try:
            refined = refine_draft_with_gemini(
                draft,
                instruction,
                (request.form.get("title") or draft["title"]).strip(),
                (request.form.get("body") or draft["body"]).strip(),
                (request.form.get("review_note") or draft["review_note"]).strip(),
            )
        except GeminiRefineError as exc:
            models = ", ".join(exc.attempted_models)
            suffix = f" 시도한 모델: {models}" if models else ""
            logger.warning("gemini refine failed draft_id=%s models=%s error=%s", draft_id, models, exc)
            if _is_gemini_quota_message(str(exc)):
                cooldown_until = mark_gemini_cooldown(store, reason=f"수동 다듬기 한도 초과: {exc}")
                flash(f"Gemini 요청 한도 감지로 {format_datetime_label(cooldown_until.isoformat())}까지 다듬기를 보류합니다.")
            flash(f"{exc}{suffix}")
            return redirect(url_for("draft_detail", draft_id=draft_id))
        except Exception as exc:  # noqa: BLE001 - UI should report a concise Gemini failure.
            logger.exception("gemini refine unexpected failure draft_id=%s", draft_id)
            flash(f"Gemini 다듬기에 실패했습니다. {type(exc).__name__}")
            return redirect(url_for("draft_detail", draft_id=draft_id))

        store.update_draft(
            draft_id=draft_id,
            title=refined.title,
            body=refined.body,
            review_note=refined.review_note,
            status=status,
            model=refined.model,
            change_type="gemini_refine",
        )
        logger.info("gemini refine succeeded draft_id=%s model=%s status=%s", draft_id, refined.model, status)
        flash("Gemini가 요청한 방향으로 초안을 다시 다듬었습니다.")
        return redirect(url_for("draft_detail", draft_id=draft_id))

    @app.post("/collect")
    def collect():
        limit = _positive_int(request.form.get("limit"), default=5)
        for message in collect_enabled_sources(store, config_path, limit):
            flash(message)
        return redirect(url_for("dashboard"))

    @app.post("/recrawl")
    def recrawl():
        limit = _positive_int(request.form.get("limit"), default=30)
        source_count = max(1, len([source for source in load_sources(config_path) if source.enabled]))
        draft_limit = max(limit * source_count, 250)
        auto_collector = app.config.get("AUTO_COLLECTOR")
        if auto_collector:
            started = auto_collector.run_async_once(collect_limit=limit, draft_limit=draft_limit, label="수동 재수집")
            messages = ["수동 재수집을 시작했습니다."] if started else ["자동 수집이 이미 실행 중입니다."]
            logger.info(
                "manual recrawl requested started=%s collect_limit=%s draft_limit=%s",
                started,
                limit,
                draft_limit,
            )
        else:
            logger.info("manual recrawl running inline collect_limit=%s draft_limit=%s", limit, draft_limit)
            messages = collect_and_draft_cycle(
                store,
                config_path,
                collect_limit=limit,
                draft_limit=draft_limit,
                require_gemini=True,
            )
        for message in messages:
            flash(message)
        return redirect(url_for("dashboard"))

    @app.get("/recrawl/status")
    def recrawl_status():
        auto_collector = app.config.get("AUTO_COLLECTOR")
        if not auto_collector:
            return jsonify(
                {
                    "enabled": False,
                    "running": False,
                    "progress_current": 0,
                    "progress_total": 0,
                    "progress_message": "대기 중",
                    "progress_source_name": "",
                    "progress_phase": "idle",
                    "last_error": None,
                    "last_finished_at": None,
                    "last_auto_finished_at": None,
                    "next_run_at": None,
                    "gemini_cooldown_until": None,
                }
            )
        status = auto_collector.snapshot()
        cooldown_until = gemini_cooldown_until(store)
        payload = _auto_collector_status_payload(store, status)
        payload["gemini_cooldown_until"] = cooldown_until.isoformat() if cooldown_until else None
        payload["progress_source_name"] = source_display_label(payload.get("progress_source_name") or "")
        return jsonify(payload)

    @app.post("/draft")
    def draft():
        limit = _positive_int(request.form.get("limit"), default=5)
        for message in draft_pending_releases(store, limit):
            flash(message)
        return redirect(url_for("dashboard"))

    @app.post("/export")
    def export():
        scope = request.form.get("scope") or "unexported"
        approved_on = datetime.now(LOCAL_TZ).date() if scope == "today" else None
        unexported_only = scope not in {"all", "today"}
        markdown_path, csv_path, count = export_approved(
            store,
            Path(export_dir),
            unexported_only=unexported_only,
            approved_on=approved_on,
        )
        logger.info(
            "approved drafts exported count=%s scope=%s markdown=%s csv=%s",
            count,
            scope,
            markdown_path,
            csv_path,
        )
        scope_label = {"today": "오늘 승인 기사", "all": "전체 승인 기사"}.get(scope, "미내보내기 승인 기사")
        flash(f"{scope_label} {count}건을 내보냈습니다.")
        flash(f"마크다운 파일: {markdown_path}")
        flash(f"표 파일: {csv_path}")
        return redirect(url_for("dashboard"))

    return app


def _retention_policy_summary() -> dict[str, object]:
    cutoff = collection_retention_cutoff_date()
    return {
        "days": collection_retention_days(),
        "cutoff_date": cutoff.isoformat(),
        "description": "주말과 공휴일을 제외한 최근 운영일 기준입니다.",
    }


def _cloudflare_quick_tunnel_status(log_path: Path | None = None) -> dict[str, object]:
    log_path = log_path or PROJECT_ROOT / "data" / "tmp" / "cloudflare_quick_tunnel.err.log"
    public_url = ""
    updated_at = None
    connected = False
    if log_path.exists():
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
            matches = CLOUDFLARE_URL_RE.findall(text)
            public_url = matches[-1] if matches else ""
            connected = _cloudflare_tunnel_connected_from_log(text)
            updated_at = datetime.fromtimestamp(log_path.stat().st_mtime, tz=LOCAL_TZ).isoformat()
        except OSError:
            public_url = ""
    running = _cloudflared_running()
    if running and public_url and connected:
        label = "외부 접속 정상"
    elif running and public_url:
        label = "터널 재연결 중"
    elif running:
        label = "터널 실행 중"
    else:
        label = "터널 미감지"
    return {
        "running": running,
        "public_url": public_url,
        "log_path": str(log_path),
        "updated_at": updated_at,
        "label": label,
    }


def _cloudflare_tunnel_connected_from_log(text: str) -> bool:
    last_connected = text.rfind("Registered tunnel connection")
    last_error = max(
        text.rfind(marker)
        for marker in (
            "Serve tunnel error",
            "failed to serve tunnel connection",
            "control stream encountered a failure",
        )
    )
    return last_connected > last_error


def _cloudflared_running() -> bool:
    try:
        if os.name == "nt":
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "@(Get-Process cloudflared -ErrorAction SilentlyContinue).Count",
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            return int((result.stdout or "0").strip() or "0") > 0
        result = subprocess.run(
            ["pgrep", "-f", "cloudflared"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return result.returncode == 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _positive_int(value: str | None, default: int) -> int:
    try:
        parsed = int(value or default)
    except ValueError:
        return default
    return max(1, min(parsed, 100))


def _list_display_limit() -> int:
    try:
        parsed = int(request.args.get("limit") or LIST_PAGE_SIZE)
    except ValueError:
        parsed = LIST_PAGE_SIZE
    return max(LIST_PAGE_SIZE, min(parsed, MAX_LIST_LIMIT))


def _load_more_url(endpoint: str, next_limit: int) -> str:
    args = request.args.to_dict(flat=False)
    args["limit"] = [str(min(next_limit, MAX_LIST_LIMIT))]
    return url_for(endpoint, **args)


def _current_next_path() -> str:
    return request.full_path.rstrip("?") if request.query_string else request.path


def _configured_admin_password_source(store: Store) -> str:
    if os.getenv("NEWS_SUMMARY_ADMIN_PASSWORD_HASH") or os.getenv("NEWS_SUMMARY_ADMIN_PASSWORD"):
        return "environment"
    if store.get_app_metadata(ADMIN_PASSWORD_HASH_KEY):
        return "database"
    return ""


def _safe_next(default_endpoint: str = "dashboard") -> str:
    target = request.args.get("next") or request.form.get("next") or url_for(default_endpoint)
    if not target.startswith("/") or target.startswith("//"):
        return url_for(default_endpoint)
    return target


def _auto_collector_status_payload(store: Store, status) -> dict[str, object]:
    payload = {
        "enabled": status.enabled,
        "running": status.running,
        "active_label": status.active_label or "",
        "progress_current": status.progress_current,
        "progress_total": status.progress_total,
        "progress_message": status.progress_message,
        "progress_source_name": status.progress_source_name or "",
        "progress_phase": status.progress_phase,
        "last_error": status.last_error,
        "last_started_at": status.last_started_at,
        "last_finished_at": status.last_finished_at,
        "last_auto_finished_at": status.last_auto_finished_at,
        "next_run_at": status.next_run_at,
        "run_count": status.run_count,
    }
    stored_payload = _stored_auto_collector_status_payload(store)
    if not stored_payload:
        return payload

    stored_updated_at = _parse_datetime(stored_payload.get("status_updated_at"))
    current_updated_at = _parse_datetime(payload.get("last_finished_at") or payload.get("last_started_at"))
    if bool(stored_payload.get("running")) or current_updated_at is None or (
        stored_updated_at and stored_updated_at >= current_updated_at
    ):
        for key in payload:
            if key in stored_payload:
                payload[key] = stored_payload[key]
        payload["status_updated_at"] = stored_payload.get("status_updated_at")
    return payload


def _stored_auto_collector_status_payload(store: Store) -> dict[str, object]:
    raw_value = store.get_app_metadata(AUTO_COLLECT_STATUS_KEY)
    if not raw_value:
        return {}
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _client_ip() -> str:
    forwarded = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return (request.headers.get("X-Real-IP") or request.remote_addr or "").strip()


def _mask_ip(value: object) -> str:
    raw = str(value or "").split(",", 1)[0].strip().strip("[]")
    if raw.count(":") == 1 and "." in raw:
        raw = raw.rsplit(":", 1)[0]
    try:
        parsed = ipaddress.ip_address(raw)
    except ValueError:
        return "알 수 없음"
    if parsed.version == 4:
        parts = str(parsed).split(".")
        return f"{parts[0]}.{parts[1]}.xxx.xxx"
    hextets = parsed.exploded.split(":")
    return f"{hextets[0]}:{hextets[1]}:xxxx:xxxx"


def _masked_request_ip() -> str:
    return _mask_ip(_client_ip())


def _browser_label(user_agent: object) -> str:
    value = str(user_agent or "").lower()
    if not value:
        return "브라우저 미상"
    mobile = "모바일 " if any(token in value for token in ("mobile", "android", "iphone")) else ""
    if "edg/" in value or "edge/" in value:
        return f"{mobile}Edge".strip()
    if "firefox/" in value:
        return f"{mobile}Firefox".strip()
    if "chrome/" in value or "crios/" in value:
        return f"{mobile}Chrome".strip()
    if "safari/" in value:
        return f"{mobile}Safari".strip()
    return f"{mobile}기타".strip()


def _should_record_visitor_access(endpoint: str, method: str) -> bool:
    if method.upper() not in {"GET", "POST"}:
        return False
    if endpoint in {"static", "favicon", "healthz", "recrawl_status"}:
        return False
    if request.path.startswith("/static/"):
        return False
    return True


def _recent_visitor_dates(days: int = 7) -> list[date]:
    today = datetime.now(LOCAL_TZ).date()
    return [today - timedelta(days=offset) for offset in range(days)]


def _visitor_cutoff_iso(days: int = 7) -> str:
    oldest = _recent_visitor_dates(days)[-1]
    local_start = datetime.combine(oldest, datetime.min.time(), tzinfo=LOCAL_TZ)
    return local_start.astimezone(timezone.utc).isoformat()


def _visitor_date_label(value: date) -> str:
    today = datetime.now(LOCAL_TZ).date()
    if value == today:
        return "오늘"
    if value == today - timedelta(days=1):
        return "어제"
    return f"{value.month:02d}.{value.day:02d}"


def _record_visitor_access(store: Store, status_code: int) -> None:
    endpoint = request.endpoint or "unknown"
    if not _should_record_visitor_access(endpoint, request.method):
        return
    cutoff_iso = _visitor_cutoff_iso()
    store.record_visitor_access(
        _masked_request_ip(),
        request.method.upper(),
        request.path,
        endpoint,
        status_code,
        _browser_label(request.headers.get("User-Agent")),
    )
    store.prune_visitor_access_logs(cutoff_iso)


def _visitor_access_overview(store: Store) -> dict[str, object]:
    recent_dates = _recent_visitor_dates()
    date_keys = {item.isoformat(): item for item in recent_dates}
    selected_key = request.args.get("access_date") or recent_dates[0].isoformat()
    if selected_key not in date_keys:
        selected_key = recent_dates[0].isoformat()

    counts = {key: 0 for key in date_keys}
    selected_rows: list[dict[str, object]] = []
    selected_ips: set[str] = set()
    for row in store.visitor_access_logs_since(_visitor_cutoff_iso()):
        visited_at = _parse_datetime(row["visited_at"])
        if visited_at is None:
            continue
        if visited_at.tzinfo is None:
            visited_at = visited_at.replace(tzinfo=timezone.utc)
        local_visited_at = visited_at.astimezone(LOCAL_TZ)
        row_key = local_visited_at.date().isoformat()
        if row_key not in counts:
            continue
        counts[row_key] += 1
        if row_key == selected_key:
            masked_ip = str(row["masked_ip"] or "알 수 없음")
            selected_ips.add(masked_ip)
        if row_key == selected_key and len(selected_rows) < 200:
            selected_rows.append(
                {
                    "masked_ip": masked_ip,
                    "method": str(row["method"] or ""),
                    "path": str(row["path"] or ""),
                    "status_code": int(row["status_code"] or 0),
                    "user_agent": str(row["user_agent"] or "브라우저 미상"),
                    "visited_at": local_visited_at.isoformat(),
                }
            )

    return {
        "selected_date": selected_key,
        "dates": [
            {
                "date": item.isoformat(),
                "label": _visitor_date_label(item),
                "count": counts[item.isoformat()],
                "active": item.isoformat() == selected_key,
            }
            for item in recent_dates
        ],
        "rows": selected_rows,
        "selected_count": counts[selected_key],
        "unique_masked_ips": len(selected_ips),
        "total_count": sum(counts.values()),
    }


def _backup_files(backup_dir: Path) -> list[dict[str, object]]:
    if not backup_dir.exists():
        return []
    files = []
    for path in sorted(backup_dir.glob("*.zip"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            stat = path.stat()
        except OSError:
            continue
        files.append(
            {
                "name": path.name,
                "path": path,
                "size": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=LOCAL_TZ).isoformat(),
            }
        )
    return files


def _safe_backup_file(backup_dir: Path, filename: str) -> Path | None:
    if not filename or Path(filename).name != filename or not filename.endswith(".zip"):
        return None
    backup_dir = backup_dir.resolve()
    path = (backup_dir / filename).resolve()
    try:
        if os.path.commonpath([str(backup_dir), str(path)]) != str(backup_dir):
            return None
    except ValueError:
        return None
    return path if path.exists() and path.is_file() else None


def _draft_change_type(action: str, status: str) -> str:
    if action in {"approved", "approved_next"} or status == "approved":
        return "approval"
    if action in {"rejected", "rejected_next"} or status == "rejected":
        return "rejection"
    return "manual"


def change_type_label(change_type: str | None) -> str:
    labels = {
        "manual": "수동 수정",
        "approval": "승인 변경",
        "rejection": "반려 변경",
        "gemini_refine": "Gemini 다듬기",
        "restore_initial": "처음 초안 복구",
        "history_restore": "이전 버전 복구",
        "status": "상태 변경",
    }
    return labels.get(change_type or "", change_type or "변경")


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def status_badge_class(status: str | None) -> str:
    if status == "approved":
        return "badge-approved"
    if status == "rejected":
        return "badge-warning"
    return "badge-neutral"


def region_display_label(region: str) -> str:
    label = (region or "").strip()
    for prefix in REGION_DISPLAY_PREFIXES:
        if label == prefix:
            return "광주·전남 전체"
    return _strip_integrated_city_prefix(label)


def source_display_label(source_name: str) -> str:
    return _strip_integrated_city_prefix(source_name or "")


def _strip_integrated_city_prefix(value: str) -> str:
    label = (value or "").strip()
    for prefix in REGION_DISPLAY_PREFIXES:
        if label.startswith(f"{prefix} "):
            return label.removeprefix(prefix).strip()
        if label.startswith(f"{prefix}청"):
            return f"시청{label.removeprefix(f'{prefix}청')}".strip()
    return label


def model_label(model: str | None) -> str:
    if not model:
        return "모델 미상"
    normalized = model.split(":", 1)[0].lower()
    if "gemini" in normalized:
        if "lite" in normalized:
            return "Gemini Lite"
        if "flash" in normalized:
            return "Gemini Flash"
        return "Gemini"
    if ":rule-based" in model:
        return "규칙 기반"
    if "gpt" in normalized:
        return "OpenAI"
    return model.split(":", 1)[0]


def model_badge_class(model: str | None) -> str:
    normalized = (model or "").split(":", 1)[0].lower()
    if "gemini" in normalized and "lite" in normalized:
        return "badge-gemini-lite"
    if "gemini" in normalized and "flash" in normalized:
        return "badge-gemini-flash"
    if model and ":gemini" in model:
        return "badge-gemini"
    if model and ":rule-based" in model:
        return "badge-warning"
    return "badge-neutral"


def interval_label(seconds: int | None) -> str:
    if not seconds:
        return "주기 미상"
    if seconds == 3600:
        return "매시간 정각"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours}시간마다"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return "1분마다" if minutes == 1 else f"{minutes}분마다"
    return f"{seconds}초마다"


def body_character_count(value: object) -> int:
    return len(str(value or "").replace("\r\n", "\n"))


def file_size_label(size: object) -> str:
    try:
        value = float(size or 0)
    except (TypeError, ValueError):
        return "0 B"
    units = ("B", "KB", "MB", "GB")
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    if index == 0:
        return f"{int(value)} {units[index]}"
    return f"{value:.1f} {units[index]}"


def format_datetime_label(value: object) -> str:
    if not value:
        return "일시 미상"
    text = str(value).strip()
    date_only = re.fullmatch(r"20\d{2}[./-]\d{1,2}[./-]\d{1,2}", text)
    if date_only:
        match = DATETIME_RE.search(text)
        if match:
            year, month, day, _, _ = match.groups()
            return f"{int(year)}.{int(month):02d}.{int(day):02d}"

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None

    if parsed:
        if parsed.tzinfo:
            parsed = parsed.astimezone(LOCAL_TZ)
        return f"{parsed.year}.{parsed.month:02d}.{parsed.day:02d} {parsed.hour:02d}:{parsed.minute:02d}"

    match = DATETIME_RE.search(text)
    if not match:
        return text
    year, month, day, hour, minute = match.groups()
    date_part = f"{int(year)}.{int(month):02d}.{int(day):02d}"
    if hour and minute:
        return f"{date_part} {int(hour):02d}:{minute}"
    return date_part


def review_flags(draft, duplicate_titles: set[str] | None = None) -> list[str]:
    flags: list[str] = []
    title = str(_row_value(draft, "title") or "")
    original_title = str(_row_value(draft, "original_title") or "")
    body = str(_row_value(draft, "body") or "")
    original_content = str(_row_value(draft, "original_content") or "")
    review_note = str(_row_value(draft, "review_note") or "")
    model = str(_row_value(draft, "model") or "")
    validation_note = str(_row_value(draft, "validation_note") or "")

    if _date_warning(draft):
        flags.append("게시일 확인")
    if duplicate_titles and original_title in duplicate_titles:
        flags.append("중복 제목")
    if ":rule-based" in model or "gemini-error" in model:
        flags.append("AI 확인")
    if "제목 핵심어 0개" in validation_note or "기존 수집 원문" in validation_note:
        flags.append("원문 검증 확인")
    if title.startswith("[뉴스 단신]") or body.lstrip().startswith(("[뉴스 단신]", title)):
        flags.append("형식 확인")
    return flags


def approval_checks(draft, duplicate_titles: set[str] | None = None) -> list[dict[str, object]]:
    flags = set(review_flags(draft, duplicate_titles))
    body = str(_row_value(draft, "body") or "")
    original = str(_row_value(draft, "original_content") or "")
    paragraphs = [part for part in re.split(r"\n\s*\n", body.strip()) if part.strip()]
    has_application_info = any(token in original for token in ("신청", "모집", "접수", "대상", "무료", "참가비", "지원"))
    body_has_application_info = any(token in body for token in ("신청", "모집", "접수", "대상", "무료", "참가비", "지원"))
    checks = [
        {"label": "주의 필요 표시 없음", "ok": not flags},
        {"label": "본문 3~4문단", "ok": 3 <= len(paragraphs) <= 4},
        {"label": "제목/본문 형식 정상", "ok": "형식 확인" not in flags},
        {"label": "게시일 정상", "ok": "게시일 확인" not in flags},
    ]
    if has_application_info:
        checks.append({"label": "신청·모집 정보 반영", "ok": body_has_application_info})
    return checks


def _group_drafts_by_recent_dates(
    drafts,
    today: date | None = None,
    days: int = 5,
    include_older: bool = False,
    date_source: str = "published",
) -> list[dict[str, object]]:
    today = today or datetime.now(LOCAL_TZ).date()
    dates = [today - timedelta(days=offset) for offset in range(days)]
    buckets = {target_date: [] for target_date in dates}
    older_drafts = []

    for draft in drafts:
        draft_date = _draft_group_date(draft, date_source)
        if draft_date in buckets:
            buckets[draft_date].append(draft)
        elif include_older:
            older_drafts.append(draft)

    groups = [
        {
            "date": target_date,
            "iso_date": target_date.isoformat(),
            "label": _date_group_label(target_date, today),
            "drafts": buckets[target_date],
            "date_source": date_source,
        }
        for target_date in dates
    ]
    if include_older and older_drafts:
        groups.append(
            {
                "date": None,
                "iso_date": "",
                "label": "이전 검수 대기",
                "drafts": _sort_drafts_latest_first(older_drafts),
                "date_source": date_source,
            }
        )
    return groups


def _filter_drafts_by_date(drafts, target_date: date):
    return [draft for draft in drafts if _draft_date(draft) == target_date]


def _filter_drafts_by_query(drafts, query: str):
    terms = [term.casefold() for term in query.split() if term.strip()]
    if not terms:
        return list(drafts)

    filtered = []
    for draft in drafts:
        haystack = " ".join(
            str(_row_value(draft, key) or "")
            for key in ("title", "source_name", "region", "original_title", "original_content", "review_note")
        ).casefold()
        if all(term in haystack for term in terms):
            filtered.append(draft)
    return filtered


def _region_options(config_path: Path) -> list[str]:
    regions = []
    seen = set()
    for source in load_sources(config_path):
        if not source.enabled:
            continue
        region = source.region.strip()
        if region and region not in seen:
            regions.append(region)
            seen.add(region)
    return regions


def _selected_regions(config_path: Path) -> list[str]:
    allowed = _region_options(config_path)
    allowed_set = set(allowed)
    requested = [region.strip() for region in request.args.getlist("region") if region.strip()]
    selected = []
    seen = set()
    for region in requested:
        if region in allowed_set and region not in seen:
            selected.append(region)
            seen.add(region)
    return [region for region in allowed if region in seen]


def _filter_rows_by_regions(rows, selected_regions: list[str]):
    if not selected_regions:
        return list(rows)
    return [row for row in rows if _region_matches(str(_row_value(row, "region") or ""), selected_regions)]


def _filter_source_summaries_by_regions(summaries: list[dict[str, object]], selected_regions: list[str]):
    if not selected_regions:
        return summaries
    return [summary for summary in summaries if _region_matches(str(summary.get("region") or ""), selected_regions)]


def _counts_for_regions(store: Store, selected_regions: list[str]) -> dict[str, int]:
    if not selected_regions:
        return store.counts()
    region_condition, region_params = _region_sql_condition("pr.region", selected_regions)
    with store.connect() as conn:
        row = conn.execute(
            f"""
            SELECT
                (SELECT COUNT(*) FROM press_releases pr WHERE {region_condition}) AS releases,
                COUNT(*) AS drafts,
                SUM(CASE WHEN ad.status = 'approved' THEN 1 ELSE 0 END) AS approved,
                SUM(CASE WHEN ad.status = 'needs_review' THEN 1 ELSE 0 END) AS needs_review
            FROM article_drafts ad
            JOIN press_releases pr ON pr.id = ad.press_release_id
            WHERE {region_condition}
            """,
            (*region_params, *region_params),
        ).fetchone()
    return {
        "press_releases": int(row["releases"] or 0),
        "drafts": int(row["drafts"] or 0),
        "approved": int(row["approved"] or 0),
        "needs_review": int(row["needs_review"] or 0),
    }


def _draft_rows_for_listing(
    store: Store,
    *,
    status: str | None = None,
    selected_regions: list[str] | None = None,
    source_filter: str = "",
    target_date: date | None = None,
    query: str = "",
    limit: int = LIST_PAGE_SIZE,
    include_original_content: bool = True,
):
    selected_regions = selected_regions or []
    where = []
    params: list[object] = []
    if status:
        where.append("ad.status = ?")
        params.append(status)
    if source_filter:
        where.append("pr.source_id = ?")
        params.append(source_filter)
    if selected_regions:
        region_condition, region_params = _region_sql_condition("pr.region", selected_regions)
        where.append(f"({region_condition})")
        params.extend(region_params)
    if target_date:
        where.append(f"{_draft_date_sql_expr()} = ?")
        params.append(target_date.isoformat())
    for term in [term.casefold() for term in query.split() if term.strip()]:
        like = f"%{term}%"
        where.append(
            "("
            "LOWER(COALESCE(ad.title, '')) LIKE ? OR "
            "LOWER(COALESCE(pr.source_name, '')) LIKE ? OR "
            "LOWER(COALESCE(pr.region, '')) LIKE ? OR "
            "LOWER(COALESCE(pr.title, '')) LIKE ? OR "
            "LOWER(COALESCE(pr.content, '')) LIKE ? OR "
            "LOWER(COALESCE(ad.review_note, '')) LIKE ?"
            ")"
        )
        params.extend([like] * 6)

    where_sql = "WHERE " + " AND ".join(where) if where else ""
    params.append(limit)
    original_content_select = "pr.content AS original_content" if include_original_content else "'' AS original_content"
    with store.connect() as conn:
        return conn.execute(
            f"""
            SELECT ad.*, pr.source_id, pr.source_name, pr.region, pr.url, {original_content_select},
                   pr.title AS original_title, pr.published_at,
                   pr.validation_status, pr.validation_note
            FROM article_drafts ad
            JOIN press_releases pr ON pr.id = ad.press_release_id
            {where_sql}
            ORDER BY COALESCE(ad.updated_at, ad.created_at) DESC, ad.id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()


def _press_release_rows_for_listing(
    store: Store,
    *,
    selected_regions: list[str] | None = None,
    limit: int = LIST_PAGE_SIZE,
):
    selected_regions = selected_regions or []
    where = []
    params: list[object] = []
    if selected_regions:
        region_condition, region_params = _region_sql_condition("pr.region", selected_regions)
        where.append(f"({region_condition})")
        params.extend(region_params)

    where_sql = "WHERE " + " AND ".join(where) if where else ""
    params.append(limit)
    with store.connect() as conn:
        return conn.execute(
            f"""
            SELECT pr.*, ad.id AS draft_id, ad.status AS draft_status, ad.model AS draft_model
            FROM press_releases pr
            LEFT JOIN article_drafts ad ON ad.press_release_id = pr.id
            {where_sql}
            ORDER BY CASE WHEN pr.published_at IS NULL OR TRIM(pr.published_at) = '' THEN 1 ELSE 0 END ASC,
                     pr.published_at DESC,
                     pr.collected_at DESC,
                     pr.id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()


def _draft_date_sql_expr() -> str:
    return (
        "REPLACE("
        "REPLACE("
        "SUBSTR(TRIM(COALESCE(NULLIF(pr.published_at, ''), ad.created_at, '')), 1, 10), "
        "'.', '-'"
        "), "
        "'/', '-'"
        ")"
    )


def _region_matches(region: str, selected_regions: list[str]) -> bool:
    region = region.strip()
    for selected in selected_regions:
        match_values = _expanded_region_match_values(selected)
        if not match_values:
            continue
        for match_value in match_values:
            if region == match_value:
                return True
            if " " not in match_value and region.startswith(f"{match_value} "):
                return True
    return False


def _region_sql_condition(column: str, selected_regions: list[str]) -> tuple[str, tuple[object, ...]]:
    clauses = []
    params: list[object] = []
    for selected in selected_regions:
        for match_value in _expanded_region_match_values(selected):
            clauses.append(f"{column} = ?")
            params.append(match_value)
            if " " not in match_value:
                clauses.append(f"{column} LIKE ?")
                params.append(f"{match_value} %")
    return " OR ".join(f"({clause})" for clause in clauses) or "1 = 1", tuple(params)


def _expanded_region_match_values(region: str) -> list[str]:
    region = region.strip()
    if not region:
        return []
    values = [region]
    for prefix in REGION_DISPLAY_PREFIXES:
        if region == prefix:
            values.extend(["전남", "광주"])
        elif region.startswith(f"{prefix} "):
            suffix = region.removeprefix(prefix).strip()
            if suffix:
                values.append(suffix)
                if not suffix.startswith("광주 "):
                    values.append(f"전남 {suffix}")
    deduped = []
    seen = set()
    for value in values:
        if value not in seen:
            deduped.append(value)
            seen.add(value)
    return deduped


def _clean_query_args(**values: object) -> dict[str, object]:
    return {key: value for key, value in values.items() if value not in (None, "")}


def _filter_drafts_by_review(drafts, review_filter: str, duplicate_titles: set[str]):
    today = datetime.now(LOCAL_TZ).date()
    if review_filter == "today":
        return [draft for draft in drafts if _draft_date(draft) == today]
    if review_filter == "attention":
        return [draft for draft in drafts if review_flags(draft, duplicate_titles)]
    if review_filter == "date_issue":
        return [draft for draft in drafts if _date_warning(draft)]
    if review_filter == "application":
        return [draft for draft in drafts if _contains_any(draft, ("신청", "모집", "접수", "대상", "무료", "참가비"))]
    if review_filter == "event":
        return [draft for draft in drafts if _contains_any(draft, ("행사", "축제", "교육", "프로그램", "전시", "공연"))]
    if review_filter == "support":
        return [draft for draft in drafts if _contains_any(draft, ("지원", "예산", "금액", "만원", "보조", "환급", "사업비"))]
    return list(drafts)


def _model_counts(store: Store) -> dict[str, int]:
    with store.connect() as conn:
        gemini = conn.execute("SELECT COUNT(*) AS count FROM article_drafts WHERE model LIKE '%:gemini'").fetchone()[
            "count"
        ]
        rule_based = conn.execute(
            "SELECT COUNT(*) AS count FROM article_drafts WHERE model LIKE '%:rule-based%'"
        ).fetchone()["count"]
    return {
        "gemini": int(gemini),
        "rule_based": int(rule_based),
    }


def _gemini_usage_summary(store: Store, auto_status=None) -> dict[str, object]:
    today = datetime.now(LOCAL_TZ).date()
    reset_at = _parse_datetime(store.get_app_metadata(GEMINI_USAGE_RESET_AT_KEY))
    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT model, created_at, updated_at
            FROM article_drafts
            WHERE model LIKE '%:gemini%'
               OR model LIKE '%:gemini-refine%'
            """
        ).fetchall()

    total_drafts = 0
    total_refines = 0
    today_drafts = 0
    today_refines = 0
    model_counts: dict[str, int] = {}

    for row in rows:
        model = str(row["model"] or "")
        is_refine = ":gemini-refine" in model
        model_name = model.split(":", 1)[0]
        event_date = _parse_datetime(row["updated_at"] if is_refine else row["created_at"])
        if reset_at and (not event_date or event_date <= reset_at):
            continue
        model_counts[model_name] = model_counts.get(model_name, 0) + 1
        if is_refine:
            total_refines += 1
            if event_date and event_date.date() == today:
                today_refines += 1
        else:
            total_drafts += 1
            if event_date and event_date.date() == today:
                today_drafts += 1

    top_models = sorted(model_counts.items(), key=lambda item: item[1], reverse=True)[:3]
    current_model_names = current_gemini_models()
    current_models = [(model, model_counts.get(model, 0)) for model in current_model_names if model_counts.get(model, 0)]
    legacy_models = [
        (model, count)
        for model, count in sorted(model_counts.items(), key=lambda item: item[1], reverse=True)
        if model not in current_model_names
    ]
    cooldown_until = gemini_cooldown_until(store)
    return {
        "today_total": today_drafts + today_refines,
        "today_drafts": today_drafts,
        "today_refines": today_refines,
        "total": total_drafts + total_refines,
        "total_drafts": total_drafts,
        "total_refines": total_refines,
        "top_models": top_models,
        "current_models": current_models,
        "legacy_models": legacy_models,
        "last_error": getattr(auto_status, "last_error", None) if auto_status else None,
        "reset_at": reset_at.isoformat() if reset_at else None,
        "cooldown_until": cooldown_until.isoformat() if cooldown_until else None,
        "cooldown_reason": store.get_app_metadata(GEMINI_COOLDOWN_REASON_KEY) if cooldown_until else None,
    }


def _duplicate_titles(store: Store) -> set[str]:
    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT title
            FROM press_releases
            GROUP BY title
            HAVING COUNT(*) > 1
            """
        ).fetchall()
    return {str(row["title"]) for row in rows}


def _source_summaries(store: Store, config_path: Path) -> list[dict[str, object]]:
    sources = [source for source in load_sources(config_path) if source.enabled]
    today = datetime.now(LOCAL_TZ).date()
    yesterday = today - timedelta(days=1)
    holidays = retention_holidays({today.year - 1, today.year, today.year + 1})
    is_business_today = is_collection_business_day(today, holidays)
    source_statuses = store.latest_source_collection_statuses()
    with store.connect() as conn:
        stats = {
            row["source_id"]: row
            for row in conn.execute(
                """
                SELECT source_id,
                       COUNT(*) AS releases,
                       MAX(collected_at) AS last_collected,
                       SUM(
                           CASE
                               WHEN REPLACE(
                                   REPLACE(
                                       SUBSTR(TRIM(COALESCE(NULLIF(published_at, ''), collected_at, '')), 1, 10),
                                       '.', '-'
                                   ),
                                   '/', '-'
                               ) = ?
                               THEN 1
                               ELSE 0
                           END
                       ) AS yesterday_releases,
                       SUM(
                           CASE
                               WHEN REPLACE(
                                   REPLACE(
                                       SUBSTR(TRIM(COALESCE(NULLIF(published_at, ''), collected_at, '')), 1, 10),
                                       '.', '-'
                                   ),
                                   '/', '-'
                               ) = ?
                               THEN 1
                               ELSE 0
                           END
                       ) AS today_releases
                FROM press_releases
                GROUP BY source_id
                """,
                (yesterday.isoformat(), today.isoformat()),
            ).fetchall()
        }
        latest = {
            row["source_id"]: row
            for row in conn.execute(
                """
                SELECT pr.source_id, pr.published_at
                FROM press_releases pr
                JOIN (
                    SELECT source_id, MAX(id) AS max_id
                    FROM press_releases
                    GROUP BY source_id
                ) latest ON latest.max_id = pr.id
                """
            ).fetchall()
        }
        latest_success = {
            row["source_id"]: row
            for row in conn.execute(
                """
                SELECT scr.source_id, scr.checked_at
                FROM source_collection_runs scr
                JOIN (
                    SELECT source_id, MAX(id) AS max_id
                    FROM source_collection_runs
                    WHERE status = 'ok'
                    GROUP BY source_id
                ) latest ON latest.max_id = scr.id
                """
            ).fetchall()
        }

    summaries = []
    for source in sources:
        stat = stats.get(source.id)
        latest_row = latest.get(source.id)
        status_row = source_statuses.get(source.id)
        success_row = latest_success.get(source.id)
        releases = int(stat["releases"]) if stat else 0
        today_releases = int(stat["today_releases"] or 0) if stat else 0
        yesterday_releases = int(stat["yesterday_releases"] or 0) if stat else 0
        issue = ""
        last_status = str(status_row["status"]) if status_row else ""
        last_checked_at = status_row["checked_at"] if status_row else None
        last_message = status_row["message"] if status_row else ""
        failure_stage = status_row["failure_stage"] if status_row else ""
        failure_reason = status_row["failure_reason"] if status_row else ""
        releases_found = int(status_row["releases_found"] or 0) if status_row else 0
        failure_stage, failure_reason = _source_failure_display(failure_stage, failure_reason, last_message)
        last_checked_datetime = _parse_datetime(last_checked_at)
        last_checked_date = last_checked_datetime.astimezone(LOCAL_TZ).date() if last_checked_datetime else None
        business_gap = business_days_between(last_checked_date, today, holidays) if last_checked_date else None
        holiday_gap = (
            has_collection_non_business_day_between(last_checked_date, today, holidays)
            if last_checked_date
            else False
        )
        status_label = "정상"
        status_level = "ok"
        status_detail = ""
        last_success_datetime = _parse_datetime(success_row["checked_at"]) if success_row else None
        last_success_date = last_success_datetime.astimezone(LOCAL_TZ).date() if last_success_datetime else None
        has_success_today = last_success_date == today
        if last_status == "failed":
            if has_success_today and today_releases > 0:
                issue = "일시 지연"
                status_label = "일시 지연"
                status_level = "warning"
                status_detail = "오늘 원문은 수집됐지만 마지막 연결 점검이 일시적으로 실패했습니다."
            else:
                issue = str(failure_stage or "수집 실패")
                status_label = "수집 실패"
                status_level = "error"
                status_detail = str(failure_reason or last_message or "")
        elif business_gap is not None and business_gap > 1:
            issue = "점검 지연"
            status_label = "점검 지연"
            status_level = "warning"
            status_detail = "영업일 기준 자동 수집 점검이 지연됐습니다."
        elif not status_row:
            issue = "점검 기록 없음"
            status_label = "점검 전"
            status_level = "warning"
        elif not is_business_today:
            status_label = "휴일 대기"
            status_detail = "주말 또는 공휴일이라 새 보도자료가 없을 수 있습니다."
        elif holiday_gap and last_checked_date != today:
            status_label = "휴일 이후 대기"
            status_detail = "연휴 이후 첫 영업일 보정 수집 대상입니다."
        elif last_status == "ok" and (releases_found == 0 or releases == 0):
            status_label = "새 기사 없음"
            status_detail = "사이트 점검은 성공했지만 보관 기준 안의 새 원문이 없습니다."
        elif latest_row and _parse_date(latest_row["published_at"]) is None:
            issue = "게시일 확인"
            status_label = "게시일 확인"
            status_level = "warning"
        summaries.append(
            {
                "id": source.id,
                "name": source.name,
                "region": source.region,
                "releases": releases,
                "yesterday_releases": yesterday_releases,
                "today_releases": today_releases,
                "last_collected": stat["last_collected"] if stat else None,
                "issue": issue,
                "status_label": status_label,
                "status_level": status_level,
                "status_detail": status_detail,
                "business_gap": business_gap,
                "last_status": last_status or "unknown",
                "last_checked_at": last_checked_at,
                "last_message": last_message,
                "failure_stage": failure_stage,
                "failure_reason": failure_reason,
            }
        )
    return summaries


def _source_summary_by_id(store: Store, config_path: Path, source_id: str) -> dict[str, object]:
    for summary in _source_summaries(store, config_path):
        if summary["id"] == source_id:
            return summary
    source = _source_by_id(config_path, source_id)
    return {
        "id": source_id,
        "name": source.name if source else source_id,
        "region": source.region if source else "",
        "releases": 0,
        "yesterday_releases": 0,
        "today_releases": 0,
        "last_collected": None,
        "issue": "수집 없음",
        "status_label": "점검 전",
        "status_level": "warning",
        "status_detail": "기관 설정은 있지만 아직 수집 점검 기록이 없습니다.",
        "business_gap": None,
        "last_status": "unknown",
        "last_checked_at": None,
        "last_message": "",
        "failure_stage": "",
        "failure_reason": "",
    }


def _source_failure_display(stage: object, reason: object, message: object) -> tuple[str, str]:
    stage_text = str(stage or "").strip()
    reason_text = str(reason or "").strip()
    message_text = str(message or "")
    lowered = message_text.lower()
    if stage_text and stage_text != "사이트 접속":
        return stage_text, reason_text
    if "certificate_verify_failed" in lowered or "certificate verify failed" in lowered:
        return "SSL 인증서", reason_text or "인증서 검증 실패"
    if "getaddrinfo failed" in lowered or "could not resolve" in lowered:
        return "DNS 조회", reason_text or "도메인 주소를 찾지 못함"
    if "handshake operation timed out" in lowered:
        return "외부 사이트 응답 지연", reason_text or "TLS 연결 시간 초과"
    if "timed out" in lowered or "timeout" in lowered or "타임아웃" in message_text:
        return "외부 사이트 응답 지연", reason_text or "응답 지연 또는 타임아웃"
    if "10054" in message_text or "강제로 끊겼습니다" in message_text:
        return "연결 강제 종료", reason_text or "원격 서버가 연결을 끊음"
    return stage_text, reason_text


def _source_by_id(config_path: Path, source_id: str):
    for source in load_sources(config_path):
        if source.id == source_id:
            return source
    return None


def _source_release_date(published_at: object, collected_at: object) -> date | None:
    published_datetime = _parse_datetime(published_at)
    if published_datetime:
        return published_datetime.date()
    published_date = _parse_date(published_at)
    if published_date:
        return published_date
    collected_datetime = _parse_datetime(collected_at)
    return collected_datetime.date() if collected_datetime else None


def _draft_date(draft) -> date | None:
    return _parse_date(_row_value(draft, "published_at")) or _parse_date(_row_value(draft, "created_at"))


def _draft_created_date(draft) -> date | None:
    return _parse_date(_row_value(draft, "created_at")) or _parse_date(_row_value(draft, "published_at"))


def _draft_group_date(draft, date_source: str) -> date | None:
    if date_source == "created":
        return _draft_created_date(draft)
    return _draft_date(draft)


def _next_review_draft_id(store: Store, current_id: int | None = None) -> int | None:
    for draft in store.drafts(status="needs_review", limit=1000):
        draft_id = int(draft["id"])
        if current_id is None or draft_id != current_id:
            return draft_id
    return None


def _is_gemini_quota_message(message: str) -> bool:
    lowered = message.lower()
    return "429" in message or "resource_exhausted" in lowered or "quota" in lowered or "요청 한도" in message


def _gemini_refine_cooldown_message(cooldown_until: datetime) -> str:
    return f"Gemini 쿨다운 중: {format_datetime_label(cooldown_until.isoformat())}까지 수동 다듬기를 보류합니다."


def _recent_log_lines(log_path: Path, limit: int = 250) -> list[str]:
    if not log_path.exists():
        return []
    try:
        return log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except OSError:
        return ["운영 로그 파일을 읽을 수 없습니다."]


def _ops_log_tabs(lines: list[str]) -> list[dict[str, object]]:
    categories = [
        ("errors", "오류", lambda line: " ERROR " in line or " WARNING " in line or "slow web request" in line),
        ("gemini", "Gemini", lambda line: "gemini" in line.lower() or "제미나이" in line or "쿨다운" in line),
        (
            "collector",
            "자동수집",
            lambda line: (
                "scheduler" in line.lower()
                or "collector" in line.lower()
                or "collect" in line.lower()
                or "recrawl" in line.lower()
                or "수집" in line
            ),
        ),
        ("all", "전체", lambda line: True),
    ]
    tabs = []
    for tab_id, label, matcher in categories:
        matched = [line for line in lines if matcher(line)][-80:]
        tabs.append({"id": tab_id, "label": label, "count": len(matched), "lines": matched})
    return tabs


def _sort_drafts_latest_first(drafts):
    return sorted(drafts, key=_draft_sort_key, reverse=True)


def _draft_sort_key(draft) -> tuple[datetime, int]:
    parsed = (
        _parse_datetime(_row_value(draft, "published_at"))
        or _parse_datetime(_row_value(draft, "created_at"))
        or datetime.min.replace(tzinfo=LOCAL_TZ)
    )
    draft_id = _row_value(draft, "id") or 0
    try:
        parsed_id = int(draft_id)
    except (TypeError, ValueError):
        parsed_id = 0
    return parsed, parsed_id


def _parse_date(value: object) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    match = DATE_RE.search(text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        try:
            return date(year, month, day)
        except ValueError:
            return None

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None

    if parsed.tzinfo:
        parsed = parsed.astimezone(LOCAL_TZ)
    return parsed.date()


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            match = DATETIME_RE.search(text)
            if not match:
                return None
            year, month, day, hour, minute = match.groups()
            parsed = datetime(
                int(year),
                int(month),
                int(day),
                int(hour or 0),
                int(minute or 0),
                tzinfo=LOCAL_TZ,
            )
    if parsed.tzinfo:
        return parsed.astimezone(LOCAL_TZ)
    return parsed.replace(tzinfo=LOCAL_TZ)


def _date_group_label(target_date: date, today: date) -> str:
    suffix = " (오늘)" if target_date == today else ""
    return f"{target_date.year}년 {target_date.month}월 {target_date.day}일{suffix}"


def _drafts_page_title(
    status: str | None,
    target_date: date | None,
    review_filter: str = "",
    source_filter: str = "",
    config_path: Path | None = None,
) -> str:
    if source_filter and config_path:
        for source in load_sources(config_path):
            if source.id == source_filter:
                return f"{source.name} 기사"
    if review_filter:
        labels = {
            "today": "오늘 기사",
            "attention": "주의 필요 기사",
            "date_issue": "게시일 확인 필요",
            "application": "신청·모집 기사",
            "event": "행사·교육 기사",
            "support": "지원·예산 기사",
        }
        return labels.get(review_filter, "필터 기사")
    if target_date:
        label = _date_group_label(target_date, datetime.now(LOCAL_TZ).date())
        status_text = status_label(status) if status else "전체"
        return f"{label} {status_text} 전체"
    if status:
        return f"{status_label(status)} 기사"
    return "기사 초안"


def _row_value(row, key: str):
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _contains_any(draft, tokens: tuple[str, ...]) -> bool:
    haystack = " ".join(
        str(_row_value(draft, key) or "")
        for key in ("title", "original_title", "original_content", "body", "review_note")
    )
    return any(token in haystack for token in tokens)


def _date_warning(draft) -> str:
    raw = str(_row_value(draft, "published_at") or "").strip()
    if not raw:
        return "게시일 없음"
    if _parse_date(raw) is None:
        return "게시일 파싱 실패"
    if not re.match(r"^\s*20\d{2}[./-]\d{1,2}[./-]\d{1,2}", raw):
        return "게시일 앞 문구 확인"
    return ""
