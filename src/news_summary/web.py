from __future__ import annotations

import ipaddress
import hashlib
import json
import re
import os
import subprocess
import time
from collections import Counter, OrderedDict
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import RLock
from urllib.parse import quote, urlparse

import httpx
from flask import Flask, Response, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.exceptions import HTTPException

from .asset_filters import is_display_noise_image_asset
from .auth import ADMIN_PASSWORD_HASH_KEY, auth_config, set_admin_password, verify_admin_password
from .backup import create_backup, restore_backup, verify_backup
from .collectors import public_press_release_url
from .exporter import export_approved
from .ops_logging import configure_logging, get_logger
from .scheduler import AUTO_COLLECT_STATUS_KEY
from .scheduler import (
    AUTO_BACKUP_VERIFY_STATUS_KEY,
    AUTO_COLLECTION_ANOMALY_STATUS_KEY,
    AUTO_DAILY_REPORT_KEY,
    AUTO_DEDUPLICATE_STATUS_KEY,
    AUTO_OPERATIONS_SUMMARY_STATUS_KEY,
    AUTO_SERVER_HEALTH_STATUS_KEY,
    AUTO_URL_DISCOVERY_STATUS_KEY,
    _source_recovery_candidates,
)
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
    is_transient_site_failure,
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
OPERATIONS_WRITE_UNLOCKED_KEY = "operations_write_unlocked"
OPERATIONS_WRITE_UNLOCKED_AT_KEY = "operations_write_unlocked_at"
LIST_PAGE_SIZE = 50
MAX_LIST_LIMIT = 500
DASHBOARD_PENDING_LIMIT = 20
DASHBOARD_RELEASE_LIMIT = 10
FILTER_FETCH_LIMIT = 1000
REGION_DISPLAY_PREFIXES = ("전남광주통합특별시", "전남광주특별시")
DEFAULT_MAX_ASSET_DOWNLOAD_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_ASSET_PREVIEW_BYTES = 12 * 1024 * 1024
DEFAULT_ASSET_PREVIEW_CACHE_BYTES = 64 * 1024 * 1024
DEFAULT_ASSET_PREVIEW_CACHE_SECONDS = 3600
DEFAULT_ASSET_PREVIEW_STALE_SECONDS = 6 * 3600
LATEST_GITHUB_COMMIT_CACHE_SECONDS = 60
DEFAULT_OPERATIONS_REPORT_CACHE_SECONDS = 20
DEFAULT_DASHBOARD_SOURCE_CACHE_SECONDS = 30
VISITOR_ACCESS_PRUNE_INTERVAL_SECONDS = 3600
DEFAULT_AUTO_RUNNING_STALE_MINUTES = 240
DEFAULT_AUTO_RUNNING_WARN_MINUTES = 180
DEFAULT_AUTO_FINISH_OVERDUE_MINUTES = 90
DEFAULT_AUTO_NEXT_RUN_GRACE_MINUTES = 10
DEFAULT_COLLECTION_COVERAGE_CHECK_HOUR = 9
DEFAULT_OPERATIONS_WRITE_UNLOCK_MINUTES = 30
DEFAULT_GEMINI_RETRY_DUE_WARNING_COUNT = 10
RUNTIME_DEPLOY_PATH_PREFIXES = ("config/", "scripts/", "src/", "templates/")
RUNTIME_DEPLOY_PATHS = ("pyproject.toml", "render.yaml")
RECOVERY_REASON_LABELS = {
    "failed": "실패 재검증",
    "focused": "이상치 집중 재수집",
    "quiet": "업무일 무수집 보정",
}
REQUIRED_SOURCE_COVERAGE = (
    ("gwangju-city", "광주청사"),
    ("gwangju-donggu", "광주 동구"),
    ("gwangju-seogu", "광주 서구"),
    ("gwangju-namgu", "광주 남구"),
    ("gwangju-bukgu", "광주 북구"),
    ("gwangju-gwangsan", "광주 광산구"),
    ("jeonnam-province", "전남광주통합특별시청"),
    ("mokpo-city", "목포"),
    ("yeosu-city", "여수"),
    ("suncheon-city", "순천"),
    ("naju-city", "나주"),
    ("gwangyang-city", "광양"),
    ("damyang-county", "담양"),
    ("gokseong-county", "곡성"),
    ("gurye-county", "구례"),
    ("goheung-county", "고흥"),
    ("boseong-county", "보성"),
    ("hwasun-county", "화순"),
    ("jangheung-county", "장흥"),
    ("gangjin-county", "강진"),
    ("haenam-county", "해남"),
    ("yeongam-county", "영암"),
    ("muan-county", "무안"),
    ("hampyeong-county", "함평"),
    ("yeonggwang-county", "영광"),
    ("jangseong-county", "장성"),
    ("wando-county", "완도"),
    ("jindo-county", "진도"),
    ("shinan-county", "신안"),
)

AssetPreviewCache = OrderedDict[tuple[int, str], tuple[float, float, str, bytes]]
_latest_github_commit_cache: dict[tuple[str, str], tuple[float, str | None]] = {}
_github_compare_files_cache: dict[tuple[str, str, str], tuple[float, list[str] | None]] = {}
_operations_report_cache: dict[tuple[str, ...], tuple[float, dict[str, object]]] = {}
_operations_report_cache_lock = RLock()
_dashboard_source_summary_cache: dict[tuple[str, str], tuple[float, list[dict[str, object]]]] = {}
_dashboard_source_summary_cache_lock = RLock()
_visitor_access_prune_lock = RLock()
_visitor_access_last_pruned_at = 0.0


class AssetDownloadError(RuntimeError):
    pass


logger = get_logger("web")


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
    app.jinja_env.globals["public_press_release_url"] = public_press_release_url
    app.jinja_env.globals["asset_version"] = _static_asset_version()
    app.jinja_env.filters["date_label"] = format_datetime_label

    store = Store(env_database())
    config_path = env_path("NEWS_SUMMARY_CONFIG", "config/municipalities.yaml")
    export_dir = env_path("NEWS_SUMMARY_EXPORT_DIR", "exports")
    backup_dir = env_path("NEWS_SUMMARY_BACKUP_DIR", "data/backups")
    store.init_db()
    source_options = load_sources(config_path)
    store.sync_source_metadata(source_options)
    app.config["NEWS_SUMMARY_LOG_PATH"] = log_path
    asset_preview_cache: AssetPreviewCache = OrderedDict()
    asset_preview_cache_lock = RLock()

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
            limit=DASHBOARD_PENDING_LIMIT + 1,
            include_original_content=False,
        )
        recent_releases = _press_release_rows_for_listing(
            store,
            selected_regions=selected_regions,
            limit=DASHBOARD_RELEASE_LIMIT + 1,
        )
        source_summaries = _filter_source_summaries_by_regions(
            _dashboard_source_summaries(store, config_path),
            selected_regions,
        )
        auto_collector = app.config.get("AUTO_COLLECTOR")
        _ensure_auto_collector_running(auto_collector)
        duplicate_titles = _duplicate_titles(store)
        attention_count = _attention_count_for_dashboard(store, selected_regions, duplicate_titles)
        auto_status = (
            _auto_collector_status_payload(store, auto_collector.snapshot()) if auto_collector else None
        )
        dashboard_pending_drafts = pending_drafts[: DASHBOARD_PENDING_LIMIT + 1]
        return render_template(
            "dashboard.html",
            counts=_counts_for_regions(store, selected_regions) if selected_regions else store.counts(),
            pending_drafts=dashboard_pending_drafts,
            draft_thumbnails=_draft_thumbnail_map(store, dashboard_pending_drafts[:DASHBOARD_PENDING_LIMIT]),
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
        gemini_queue_health = _gemini_queue_health_payload(store)
        source_collection_health = _source_collection_health_payload(store)
        auto_collector = app.config.get("AUTO_COLLECTOR")
        if auto_collector:
            _ensure_auto_collector_running(auto_collector)
            auto_status = _auto_collector_status_payload(store, auto_collector.snapshot())
            auto_label = _auto_collector_health_label(auto_status)
            timing_health = _auto_collector_timing_health(auto_status)
            return jsonify(
                {
                    "ok": True,
                    "database": "ok",
                    "auto_collector": auto_label,
                    "auto_collector_thread_alive": auto_status.get("thread_alive"),
                    "auto_collector_timing": timing_health["status"],
                    "auto_collector_overdue": timing_health["overdue"],
                    "auto_collector_lag_minutes": timing_health["lag_minutes"],
                    "auto_collector_run_minutes": timing_health["run_minutes"],
                    "auto_collector_schedule_delay_minutes": timing_health["schedule_delay_minutes"],
                    "auto_collector_health_message": timing_health["message"],
                    "last_auto_finished_at": auto_status["last_auto_finished_at"],
                    "next_run_at": auto_status["next_run_at"],
                    "commit": _running_commit_short(),
                    **gemini_queue_health,
                    **source_collection_health,
                }
            )
        return jsonify(
            {
                "ok": True,
                "database": "ok",
                "auto_collector": "unavailable",
                "commit": _running_commit_short(),
                **gemini_queue_health,
                **source_collection_health,
            }
        )

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
        session.pop(OPERATIONS_ADMIN_PASSWORD_UNLOCKED_KEY, None)
        session.pop(OPERATIONS_WRITE_UNLOCKED_KEY, None)
        session.pop(OPERATIONS_WRITE_UNLOCKED_AT_KEY, None)
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
        with store.reusable_connection_scope():
            with store.app_metadata_cache_scope():
                auto_collector = app.config.get("AUTO_COLLECTOR")
                _ensure_auto_collector_running(auto_collector)
                auto_status = (
                    _auto_collector_status_payload(store, auto_collector.snapshot()) if auto_collector else None
                )
                admin_password_source = _configured_admin_password_source(store)
                admin_password_configured = bool(admin_password_source)
                pending_queue = store.pending_press_release_summary()
                draft_failure_summary = store.draft_generation_failure_summary()
                context = {
                    "auto_collector_status": auto_status,
                    "pending_queue": pending_queue,
                    "draft_failure_summary": draft_failure_summary,
                    "visitor_access": _visitor_access_overview(store),
                    "backup_dir": backup_dir,
                    "backup_files": _backup_files(backup_dir),
                    "db_path": store.display_location,
                    "log_path": Path(app.config["NEWS_SUMMARY_LOG_PATH"]),
                    "admin_password_source": admin_password_source,
                    "admin_password_configured": admin_password_configured,
                    "admin_password_unlocked": bool(
                        admin_password_configured and session.get(OPERATIONS_ADMIN_PASSWORD_UNLOCKED_KEY)
                    ),
                    "operations_write_unlocked": _operations_write_access_unlocked(store),
                    "operations_write_expires_at": _operations_write_access_expires_at(),
                    "operations_write_unlock_minutes": _operations_write_unlock_minutes(),
                }
                context.update(_operations_cached_report_bundle(store, config_path, backup_dir, auto_status, pending_queue))
                return render_template("operations.html", **context)

    @app.post("/operations/auto-collect")
    def update_auto_collect():
        if locked_response := _require_operations_write_access(store):
            return locked_response
        auto_collector = app.config.get("AUTO_COLLECTOR")
        if not auto_collector:
            flash("자동 수집 컨트롤러가 준비되지 않았습니다. 프로그램을 다시 실행해 주세요.")
            return redirect(url_for("operations"))
        enabled = request.form.get("enabled") == "true"
        auto_collector.set_enabled(enabled)
        _clear_operations_report_cache()
        logger.info("auto collector setting changed enabled=%s", enabled)
        flash("자동 수집을 켰습니다." if enabled else "자동 수집을 껐습니다.")
        return redirect(url_for("operations"))

    @app.post("/operations/write-access/unlock")
    def unlock_operations_write_access():
        source = _configured_admin_password_source(store)
        if not source:
            flash("운영 변경 기능을 사용하려면 관리자 비밀번호를 먼저 설정하세요.")
            return redirect(url_for("admin_setup"))
        current_password = request.form.get("current_password") or ""
        if verify_admin_password(store, current_password):
            _unlock_operations_write_session()
            logger.info("operations write access unlocked remote_addr=%s", _masked_request_ip())
            flash("운영 변경 기능 잠금을 해제했습니다.")
        else:
            logger.warning("operations write access unlock failed remote_addr=%s", _masked_request_ip())
            flash("관리자 비밀번호가 올바르지 않습니다.")
        return redirect(url_for("operations"))

    @app.post("/operations/write-access/lock")
    def lock_operations_write_access():
        session.pop(OPERATIONS_WRITE_UNLOCKED_KEY, None)
        session.pop(OPERATIONS_WRITE_UNLOCKED_AT_KEY, None)
        flash("운영 변경 기능을 다시 잠갔습니다.")
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
            _unlock_operations_write_session()
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
            session.pop(OPERATIONS_WRITE_UNLOCKED_KEY, None)
            session.pop(OPERATIONS_WRITE_UNLOCKED_AT_KEY, None)
            _clear_operations_report_cache()
            logger.info("admin password changed remote_addr=%s", _masked_request_ip())
            flash("관리자 비밀번호를 변경했습니다. 다음 로그인부터 새 비밀번호를 사용하세요.")
        return redirect(url_for("operations"))

    @app.post("/operations/backup")
    def create_backup_route():
        if locked_response := _require_operations_write_access(store):
            return locked_response
        try:
            backup_path = create_backup(PROJECT_ROOT, store.path, backup_dir)
        except Exception as exc:  # noqa: BLE001 - backup failures should be visible in operations.
            logger.exception("backup creation failed")
            flash(f"백업 생성에 실패했습니다: {type(exc).__name__}: {exc}")
            return redirect(url_for("operations"))
        _persist_backup_verification_result(store, backup_path)
        _clear_operations_report_cache()
        logger.info("backup created path=%s", backup_path)
        flash(f"백업을 생성했습니다: {backup_path.name}")
        return redirect(url_for("operations"))

    @app.get("/operations/backups/<path:filename>")
    def download_backup(filename: str):
        if locked_response := _require_operations_write_access(store):
            return locked_response
        backup_path = _safe_backup_file(backup_dir, filename)
        if not backup_path:
            flash("백업 파일을 찾을 수 없습니다.")
            return redirect(url_for("operations"))
        return send_file(backup_path, as_attachment=True, download_name=backup_path.name)

    @app.post("/operations/restore")
    def restore_backup_route():
        if locked_response := _require_operations_write_access(store):
            return locked_response
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
        _clear_operations_report_cache()
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
        draft_thumbnails = _draft_thumbnail_map(store, draft_rows)
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
            draft_thumbnails=draft_thumbnails,
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

    @app.get("/press-releases/<int:release_id>")
    def press_release_detail(release_id: int):
        release = store.get_press_release(release_id)
        if not release:
            flash("수집 원문을 찾을 수 없습니다.")
            return redirect(url_for("press_releases"))
        press_assets = _display_press_assets(store.press_release_assets(release_id))
        return render_template(
            "press_release_detail.html",
            release=release,
            press_assets=press_assets,
            has_press_image=any(asset["is_image"] for asset in press_assets),
        )

    @app.get("/press-releases/assets/<int:asset_id>/download")
    def download_press_release_asset(asset_id: int):
        asset = store.get_press_release_asset(asset_id)
        if not asset:
            flash("첨부파일을 찾을 수 없습니다.")
            return redirect(url_for("press_releases"))
        asset_url = str(asset["url"] or "")
        if not asset_url.startswith(("http://", "https://")):
            flash("다운로드할 수 없는 첨부파일 주소입니다.")
            return redirect(url_for("press_release_detail", release_id=asset["press_release_id"]))
        try:
            response_content_type, response_content = _download_asset_content_from_url(asset_url, asset)
        except (httpx.HTTPError, AssetDownloadError) as exc:
            logger.warning("asset download failed asset_id=%s url=%s error=%s", asset_id, asset_url, exc)
            flash("첨부파일 다운로드에 실패했습니다. 원문 사이트 상태를 확인해 주세요.")
            return redirect(url_for("press_release_detail", release_id=asset["press_release_id"]))

        filename = _asset_download_filename(asset, response_content_type)
        content_type = str(response_content_type or asset["content_type"] or "application/octet-stream")
        return Response(
            response_content,
            headers={
                "Content-Type": content_type,
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
                "Content-Length": str(len(response_content)),
            },
        )

    @app.get("/press-releases/assets/<int:asset_id>/preview")
    def preview_press_release_asset(asset_id: int):
        asset = store.get_press_release_asset(asset_id)
        if not asset or not asset["is_image"]:
            return Response("이미지 미리보기를 찾을 수 없습니다.", status=404, content_type="text/plain; charset=utf-8")
        asset_url = str(asset["url"] or "")
        if not asset_url.startswith(("http://", "https://")):
            return Response("이미지 미리보기를 표시할 수 없습니다.", status=404, content_type="text/plain; charset=utf-8")
        if _should_redirect_asset_preview(asset_url):
            return redirect(asset_url, code=302)
        cache_key = (int(asset_id), asset_url)
        with asset_preview_cache_lock:
            cached_preview = _asset_preview_cache_get(asset_preview_cache, cache_key)
        if cached_preview:
            response_content_type, response_content = cached_preview
            return _asset_preview_response(response_content_type, response_content, "HIT")
        try:
            response_content_type, response_content = _download_asset_content_from_url(
                asset_url,
                asset,
                max_bytes=_max_asset_preview_bytes(),
            )
        except (httpx.HTTPError, AssetDownloadError) as exc:
            logger.warning("asset preview failed asset_id=%s url=%s error=%s", asset_id, asset_url, exc)
            with asset_preview_cache_lock:
                stale_preview = _asset_preview_cache_get(asset_preview_cache, cache_key, allow_stale=True)
            if stale_preview:
                response_content_type, response_content = stale_preview
                return _asset_preview_response(response_content_type, response_content, "STALE")
            return Response("이미지 미리보기에 실패했습니다.", status=502, content_type="text/plain; charset=utf-8")

        with asset_preview_cache_lock:
            _asset_preview_cache_put(
                asset_preview_cache,
                cache_key,
                response_content_type,
                response_content,
            )
        return _asset_preview_response(response_content_type, response_content, "MISS")

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
            recent_assets=_display_press_assets(store.press_release_assets_by_source(source_id, limit=30)),
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
        press_assets = _display_press_assets(store.press_release_assets(int(draft["press_release_id"])))
        return render_template(
            "draft_detail.html",
            draft=draft,
            press_assets=press_assets,
            has_press_image=any(asset["is_image"] for asset in press_assets),
            statuses=STATUS_ORDER,
            duplicate_titles=duplicate_titles,
            next_review_draft_id=_next_review_draft_id(store, current_id=draft_id),
            gemini_cooldown_until=gemini_cooldown_until(store),
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
        _ensure_auto_collector_running(auto_collector)
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


def _deployment_version_report() -> dict[str, object]:
    running_commit = _running_commit()
    repo = os.getenv("NEWS_SUMMARY_GITHUB_REPO", "seunghooda-dev/news-express").strip()
    branch = os.getenv("NEWS_SUMMARY_GITHUB_BRANCH", "codex/news-express").strip()
    latest_commit = _latest_github_commit(repo, branch) if repo and branch else None
    deploy_config = _render_deploy_config_report()
    change_report = _deployment_change_report(repo, running_commit, branch) if repo and running_commit and latest_commit else None
    if running_commit and latest_commit:
        is_current = running_commit.lower().startswith(latest_commit[:12].lower()) or latest_commit.lower().startswith(
            running_commit[:12].lower()
        )
        if is_current:
            status_label = "최신 배포"
            status_level = "ok"
        elif change_report and change_report["runtime_change_count"] == 0:
            status_label = "문서 변경만 미배포"
            status_level = "ok"
        else:
            status_label = "배포 필요"
            status_level = "warning"
    elif running_commit:
        status_label = "실행 버전 확인"
        status_level = "neutral"
    else:
        status_label = "버전 확인 불가"
        status_level = "warning"
    return {
        "status_label": status_label,
        "status_level": status_level,
        "running_commit": _short_commit(running_commit),
        "latest_commit": _short_commit(latest_commit),
        "repo": repo,
        "branch": branch,
        "change_report": change_report,
        **deploy_config,
    }


def _render_deploy_config_report() -> dict[str, object]:
    config_path = PROJECT_ROOT / "render.yaml"
    try:
        import yaml

        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        services = raw.get("services") or []
        service = next((item for item in services if item.get("name") == "news-express"), None)
        trigger = str((service or {}).get("autoDeployTrigger") or "").strip()
    except Exception as exc:  # noqa: BLE001 - operations page should stay available when config parsing fails.
        logger.warning("render deploy config check failed error=%s", exc)
        return {
            "auto_deploy_trigger": "",
            "auto_deploy_label": "확인 불가",
            "auto_deploy_level": "warning",
        }

    labels = {
        "commit": "On Commit",
        "checksPass": "Checks Pass",
        "off": "Off",
    }
    return {
        "auto_deploy_trigger": trigger,
        "auto_deploy_label": labels.get(trigger, trigger or "확인 불가"),
        "auto_deploy_level": "ok" if trigger in {"commit", "checksPass"} else "warning",
    }


def _running_commit_short() -> str | None:
    return _short_commit(_running_commit())


def _static_asset_version() -> str:
    commit = _running_commit_short()
    if commit:
        return commit
    try:
        return str(int((PROJECT_ROOT / "src" / "news_summary" / "static" / "app.css").stat().st_mtime))
    except OSError:
        return str(int(time.time()))


def _running_commit() -> str | None:
    for key in ("RENDER_GIT_COMMIT", "NEWS_SUMMARY_GIT_COMMIT", "GIT_COMMIT", "SOURCE_VERSION"):
        value = os.getenv(key)
        if value:
            return value.strip()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            timeout=2,
            check=True,
        )
    except Exception:
        return None
    return result.stdout.strip() or None


def _latest_github_commit(repo: str, branch: str) -> str | None:
    cache_key = (repo, branch)
    now = time.monotonic()
    cached = _latest_github_commit_cache.get(cache_key)
    if cached and now - cached[0] <= LATEST_GITHUB_COMMIT_CACHE_SECONDS:
        return cached[1]
    url = f"https://api.github.com/repos/{repo}/commits/{quote(branch, safe='')}"
    try:
        response = httpx.get(url, timeout=2.5, headers={"Accept": "application/vnd.github+json"})
        response.raise_for_status()
        payload = response.json()
    except Exception:
        _latest_github_commit_cache[cache_key] = (now, None)
        return None
    sha = payload.get("sha") if isinstance(payload, dict) else None
    result = str(sha).strip() if sha else None
    _latest_github_commit_cache[cache_key] = (now, result)
    return result


def _deployment_change_report(repo: str, running_commit: str | None, branch: str) -> dict[str, object] | None:
    if not running_commit:
        return None
    filenames = _github_compare_files(repo, running_commit, branch)
    if filenames is None:
        return None
    runtime_files = [filename for filename in filenames if _is_runtime_deploy_file(filename)]
    return {
        "changed_count": len(filenames),
        "runtime_change_count": len(runtime_files),
        "sample_files": filenames[:5],
        "runtime_sample_files": runtime_files[:5],
    }


def _github_compare_files(repo: str, base_commit: str, head: str) -> list[str] | None:
    cache_key = (repo, base_commit[:40], head)
    now = time.monotonic()
    cached = _github_compare_files_cache.get(cache_key)
    if cached and now - cached[0] <= LATEST_GITHUB_COMMIT_CACHE_SECONDS:
        return cached[1]
    url = f"https://api.github.com/repos/{repo}/compare/{quote(base_commit, safe='')}...{quote(head, safe='')}"
    try:
        response = httpx.get(url, timeout=3.0, headers={"Accept": "application/vnd.github+json"})
        response.raise_for_status()
        payload = response.json()
    except Exception:
        _github_compare_files_cache[cache_key] = (now, None)
        return None
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list):
        _github_compare_files_cache[cache_key] = (now, None)
        return None
    filenames = [
        str(item.get("filename") or "").strip().replace("\\", "/")
        for item in files
        if isinstance(item, dict) and str(item.get("filename") or "").strip()
    ]
    _github_compare_files_cache[cache_key] = (now, filenames)
    return filenames


def _is_runtime_deploy_file(filename: str) -> bool:
    normalized = filename.strip().replace("\\", "/")
    return normalized in RUNTIME_DEPLOY_PATHS or normalized.startswith(RUNTIME_DEPLOY_PATH_PREFIXES)


def _short_commit(value: str | None) -> str | None:
    if not value:
        return None
    return value[:7]


def _asset_download_filename(asset, content_type: str = "") -> str:
    filename = str(asset["filename"] or asset["title"] or "attachment").strip()
    filename = re.sub(r'[\\/:*?"<>|\r\n]+', "_", filename)
    filename = re.sub(r"\s+", " ", filename).strip(" ._")[:160] or "attachment"
    if "." not in filename:
        content_type = str(asset["content_type"] or content_type or "").lower()
        extension = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/bmp": ".bmp",
            "image/tiff": ".tif",
            "application/pdf": ".pdf",
            "application/x-hwp": ".hwp",
            "application/hwp+zip": ".hwpx",
            "application/zip": ".zip",
        }.get(content_type.split(";", 1)[0].strip(), "")
        filename += extension
    return filename


def _download_asset_content_from_url(
    asset_url: str,
    asset,
    *,
    max_bytes: int | None = None,
) -> tuple[str, bytes]:
    timeout = httpx.Timeout(30.0, connect=10.0)
    headers = _asset_request_headers(asset_url, asset)
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout, headers=headers) as client:
            return _download_asset_content(client, asset_url, asset, max_bytes=max_bytes)
    except httpx.ConnectError as exc:
        if not _should_retry_asset_download_without_tls_verify(asset_url, exc):
            raise
        logger.warning("asset download ssl verification failed; retrying without verification url=%s", asset_url)
        with httpx.Client(follow_redirects=True, timeout=timeout, headers=headers, verify=False) as client:
            return _download_asset_content(client, asset_url, asset, max_bytes=max_bytes)


def _asset_request_headers(asset_url: str, asset) -> dict[str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0 Safari/537.36 NewsExpress/1.0"
        ),
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
    }
    if _asset_row_bool(asset, "is_image"):
        headers["Accept"] = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
    else:
        headers["Accept"] = "application/octet-stream,*/*;q=0.8"
    press_url = str(_asset_row_value(asset, "press_url") or "").strip()
    if press_url.startswith(("http://", "https://")):
        headers["Referer"] = press_url
    else:
        try:
            parsed = urlparse(asset_url)
        except ValueError:
            parsed = None
        if parsed and parsed.scheme and parsed.netloc:
            headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
    return headers


def _asset_row_value(asset, key: str):
    try:
        return asset[key]
    except (KeyError, IndexError, TypeError):
        if isinstance(asset, dict):
            return asset.get(key)
    return None


def _asset_row_bool(asset, key: str) -> bool:
    return bool(_asset_row_value(asset, key))


def _should_redirect_asset_preview(asset_url: str) -> bool:
    try:
        parsed = urlparse(asset_url)
    except ValueError:
        return False
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if not (host == "gangjin.go.kr" or host.endswith(".gangjin.go.kr")):
        return False
    if "/ybmodule.file/" not in path:
        return False
    if not any(token in path for token in ("/www_press/", "/board_www/")):
        return False
    return bool(re.search(r"\.(?:jpe?g|png|gif|webp|bmp)$", path))


def _should_retry_asset_download_without_tls_verify(asset_url: str, exc: BaseException) -> bool:
    if not str(asset_url or "").startswith("https://"):
        return False
    current: BaseException | None = exc
    while current is not None:
        text = str(current).lower()
        if "certificate_verify_failed" in text or ("certificate" in text and "verify failed" in text):
            return True
        current = current.__cause__ or current.__context__
    return False


def _download_asset_content(
    client: httpx.Client,
    asset_url: str,
    asset,
    *,
    max_bytes: int | None = None,
) -> tuple[str, bytes]:
    max_bytes = max_bytes or _max_asset_download_bytes()
    chunks: list[bytes] = []
    total = 0
    with client.stream("GET", asset_url) as response:
        response.raise_for_status()
        content_type = str(response.headers.get("content-type") or "")
        content_length = _response_content_length(response.headers)
        if content_length and content_length > max_bytes:
            raise AssetDownloadError(f"첨부파일 크기 초과: {content_length} bytes")
        for chunk in response.iter_bytes():
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise AssetDownloadError(f"첨부파일 크기 초과: {total} bytes")
            chunks.append(chunk)

    content = b"".join(chunks)
    effective_content_type = _asset_download_content_type(asset, content_type, content)
    if not effective_content_type:
        raise AssetDownloadError(f"첨부파일이 아닌 응답: {content_type or 'unknown'}")
    return effective_content_type, content


def _max_asset_download_bytes() -> int:
    raw_value = os.getenv("NEWS_SUMMARY_MAX_ASSET_DOWNLOAD_MB", "25")
    try:
        megabytes = float(raw_value)
    except ValueError:
        megabytes = DEFAULT_MAX_ASSET_DOWNLOAD_BYTES / 1024 / 1024
    megabytes = max(1.0, min(megabytes, 100.0))
    return int(megabytes * 1024 * 1024)


def _max_asset_preview_bytes() -> int:
    default_megabytes = DEFAULT_MAX_ASSET_PREVIEW_BYTES / 1024 / 1024
    raw_value = os.getenv("NEWS_SUMMARY_MAX_ASSET_PREVIEW_MB", str(default_megabytes))
    try:
        megabytes = float(raw_value)
    except ValueError:
        megabytes = DEFAULT_MAX_ASSET_PREVIEW_BYTES / 1024 / 1024
    megabytes = max(1.0, min(megabytes, 25.0))
    return int(megabytes * 1024 * 1024)


def _max_asset_preview_cache_bytes() -> int:
    raw_value = os.getenv("NEWS_SUMMARY_ASSET_PREVIEW_CACHE_MB", "64")
    try:
        megabytes = float(raw_value)
    except ValueError:
        megabytes = DEFAULT_ASSET_PREVIEW_CACHE_BYTES / 1024 / 1024
    if megabytes <= 0:
        return 0
    megabytes = min(megabytes, 256.0)
    return int(megabytes * 1024 * 1024)


def _asset_preview_cache_seconds() -> int:
    raw_value = os.getenv("NEWS_SUMMARY_ASSET_PREVIEW_CACHE_SECONDS", str(DEFAULT_ASSET_PREVIEW_CACHE_SECONDS))
    try:
        seconds = int(raw_value)
    except ValueError:
        seconds = DEFAULT_ASSET_PREVIEW_CACHE_SECONDS
    return max(0, min(seconds, 86400))


def _asset_preview_stale_seconds() -> int:
    raw_value = os.getenv("NEWS_SUMMARY_ASSET_PREVIEW_STALE_SECONDS", str(DEFAULT_ASSET_PREVIEW_STALE_SECONDS))
    try:
        seconds = int(raw_value)
    except ValueError:
        seconds = DEFAULT_ASSET_PREVIEW_STALE_SECONDS
    return max(0, min(seconds, 7 * 86400))


def _asset_preview_cache_get(
    cache: AssetPreviewCache,
    key: tuple[int, str],
    *,
    now: float | None = None,
    allow_stale: bool = False,
) -> tuple[str, bytes] | None:
    cached = cache.get(key)
    if not cached:
        return None
    now = time.time() if now is None else now
    fresh_expires_at, stale_expires_at, content_type, content = cached
    if fresh_expires_at <= now and not allow_stale:
        return None
    if stale_expires_at <= now:
        cache.pop(key, None)
        return None
    cache.move_to_end(key)
    return content_type, content


def _asset_preview_cache_put(
    cache: AssetPreviewCache,
    key: tuple[int, str],
    content_type: str,
    content: bytes,
    *,
    now: float | None = None,
) -> None:
    max_bytes = _max_asset_preview_cache_bytes()
    ttl_seconds = _asset_preview_cache_seconds()
    stale_seconds = _asset_preview_stale_seconds()
    if max_bytes <= 0 or ttl_seconds <= 0 or len(content) > max_bytes:
        cache.pop(key, None)
        return
    now = time.time() if now is None else now
    cache[key] = (now + ttl_seconds, now + ttl_seconds + stale_seconds, content_type, content)
    cache.move_to_end(key)
    _asset_preview_cache_prune(cache, now=now, max_bytes=max_bytes)


def _asset_preview_cache_prune(
    cache: AssetPreviewCache,
    *,
    now: float | None = None,
    max_bytes: int | None = None,
) -> None:
    now = time.time() if now is None else now
    max_bytes = _max_asset_preview_cache_bytes() if max_bytes is None else max_bytes
    for key, (_fresh_expires_at, stale_expires_at, _content_type, _content) in list(cache.items()):
        if stale_expires_at <= now:
            cache.pop(key, None)
    while cache and _asset_preview_cache_size(cache) > max_bytes:
        cache.popitem(last=False)


def _asset_preview_cache_size(cache: AssetPreviewCache) -> int:
    return sum(len(content) for _fresh_expires_at, _stale_expires_at, _content_type, content in cache.values())


def _asset_preview_response(content_type: str, content: bytes, cache_status: str) -> Response:
    etag = _asset_preview_etag(content)
    cache_seconds = _asset_preview_cache_seconds()
    if cache_status == "STALE":
        cache_seconds = min(cache_seconds, 60)
    cache_control = f"public, max-age={cache_seconds}, stale-if-error={_asset_preview_stale_seconds()}"
    if _request_etag_matches(etag):
        return Response(
            status=304,
            headers={
                "Cache-Control": cache_control,
                "ETag": etag,
                "X-News-Express-Preview-Cache": cache_status,
            },
        )
    return Response(
        content,
        headers={
            "Content-Type": str(content_type or "application/octet-stream"),
            "Content-Length": str(len(content)),
            "Cache-Control": cache_control,
            "ETag": etag,
            "X-News-Express-Preview-Cache": cache_status,
        },
    )


def _asset_preview_etag(content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()[:24]
    return f'"asset-preview-{len(content)}-{digest}"'


def _request_etag_matches(etag: str) -> bool:
    header = request.headers.get("If-None-Match", "")
    if not header:
        return False
    tags = [value.strip() for value in header.split(",")]
    return "*" in tags or etag in tags


def _response_content_length(headers) -> int | None:
    value = str(headers.get("content-length") or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _asset_download_content_type(asset, content_type: str, content: bytes) -> str:
    normalized_type = content_type.split(";", 1)[0].strip().lower()
    if normalized_type == "text/html" or _looks_like_html_document(content):
        return ""
    if asset["is_image"]:
        if normalized_type.startswith("image/"):
            return normalized_type
        detected_type = _image_content_type_from_magic(content)
        if detected_type:
            return detected_type
        return ""
    return normalized_type or str(content_type or "application/octet-stream")


def _asset_download_content_allowed(asset, content_type: str, content: bytes) -> bool:
    return bool(_asset_download_content_type(asset, content_type, content))


def _image_content_type_from_magic(content: bytes) -> str:
    head = content[:16]
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"BM"):
        return "image/bmp"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    return ""


def _looks_like_html_document(content: bytes) -> bool:
    head = content[:512].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html", b"<head", b"<body"))


def _db_health_report(store: Store, backup_dir: Path) -> dict[str, object]:
    started = time.perf_counter()
    try:
        with store.connect() as conn:
            conn.execute("SELECT 1").fetchone()
        latency_ms = int((time.perf_counter() - started) * 1000)
        status_label = "정상"
        status_level = "ok"
    except Exception as exc:  # noqa: BLE001 - operations diagnostics should report DB health.
        return {
            "status_label": "DB 오류",
            "status_level": "error",
            "latency_ms": None,
            "database_kind": "PostgreSQL" if store.is_postgres else "SQLite",
            "latest_backup": None,
            "backup_count": 0,
            "note": f"{type(exc).__name__}: {exc}",
        }

    backups = _backup_files(backup_dir)
    latest_backup = backups[0] if backups else None
    storage_warning = _backup_storage_warning(backup_dir)
    if not latest_backup:
        status_level = "warning"
        status_label = "백업 필요"
    elif storage_warning:
        status_level = "warning"
        status_label = "보관 주의"
    if store.is_postgres:
        if storage_warning and latest_backup:
            note = f"{storage_warning} PostgreSQL 백업 ZIP은 확인됐지만 Neon 백업/스냅샷도 함께 유지하는 구성이 안전합니다."
        elif storage_warning:
            note = f"앱 백업 ZIP이 아직 없고, {storage_warning} PostgreSQL 백업은 JSON 덤프로 생성되며 Neon 스냅샷도 함께 확인하는 구성이 안전합니다."
        elif latest_backup:
            note = "최근 앱 백업 ZIP이 확인됐습니다. Neon 백업/스냅샷도 함께 유지하는 구성이 안전합니다."
        else:
            note = "앱 백업 ZIP이 아직 없습니다. PostgreSQL 백업은 JSON 덤프로 생성되며 Neon 스냅샷도 함께 확인하는 구성이 안전합니다."
    elif storage_warning and latest_backup:
        note = f"{storage_warning} 최근 백업 파일은 확인됐지만 영구 보관 경로로 옮기는 구성이 안전합니다."
    elif storage_warning:
        note = f"아직 로컬 백업 파일이 없고, {storage_warning}"
    elif not latest_backup:
        note = "아직 로컬 백업 파일이 없습니다."
    else:
        note = "최근 백업 파일이 확인됐습니다."
    return {
        "status_label": status_label,
        "status_level": status_level,
        "latency_ms": latency_ms,
        "database_kind": "PostgreSQL" if store.is_postgres else "SQLite",
        "latest_backup": latest_backup,
        "backup_count": len(backups),
        "note": note,
    }


def _backup_storage_warning(backup_dir: Path) -> str:
    raw = backup_dir.as_posix().lower()
    resolved = backup_dir if backup_dir.is_absolute() else (PROJECT_ROOT / backup_dir)
    normalized = resolved.resolve().as_posix().lower()
    volatile_prefixes = ("/tmp", "/var/tmp", "/temp")
    if any(
        value == prefix or value.startswith(f"{prefix}/")
        for value in (raw, normalized)
        for prefix in volatile_prefixes
    ):
        return "백업 폴더가 임시 경로라 재시작이나 재배포 때 사라질 수 있습니다."
    if re.match(r"^[a-z]:/(tmp|temp)(/|$)", normalized):
        return "백업 폴더가 임시 경로라 재시작이나 재배포 때 사라질 수 있습니다."
    if "/appdata/local/temp" in normalized:
        return "백업 폴더가 Windows 임시 경로라 정리 작업 때 사라질 수 있습니다."
    return ""


def _operations_cached_report_bundle(
    store: Store,
    config_path: Path,
    backup_dir: Path,
    auto_status: object | None,
    pending_queue: dict[str, object],
) -> dict[str, object]:
    ttl_seconds = _operations_report_cache_seconds()
    cache_key = _operations_report_cache_key(store, config_path, backup_dir, auto_status, pending_queue)
    now = time.monotonic()
    if ttl_seconds > 0:
        with _operations_report_cache_lock:
            cached = _operations_report_cache.get(cache_key)
            if cached and now - cached[0] <= ttl_seconds:
                return dict(cached[1])

    reports = {
        "retention_policy": _retention_policy_summary(),
        "operations_health": _operations_health_report(store, auto_status, pending_queue),
        "recovery_candidate_report": _recovery_candidate_report(store, config_path),
        "deployment_version": _deployment_version_report(),
        "db_health": _db_health_report(store, backup_dir),
        "date_issue_report": _date_issue_report(store),
        "daily_report": _daily_operations_report(store),
        "operations_summary": _operations_summary_report(store),
        "collection_check_coverage_report": _collection_check_coverage_report(store, config_path),
        "draft_conversion_coverage_report": _draft_conversion_coverage_report(store),
        "source_coverage_report": _source_coverage_report(config_path),
        "server_health_report": _server_health_report(store),
        "anomaly_report": _collection_anomaly_report(store),
        "deduplicate_report": _deduplicate_report(store),
        "fallback_report": _fallback_url_report(config_path),
        "url_discovery_report": _url_discovery_report(store),
        "backup_verify_report": _backup_verify_report(store, backup_dir),
        "automation_settings": _automation_settings_report(),
        "cloudflare_tunnel": _cloudflare_quick_tunnel_status(),
    }
    if ttl_seconds > 0:
        with _operations_report_cache_lock:
            if len(_operations_report_cache) >= 32:
                _operations_report_cache.clear()
            _operations_report_cache[cache_key] = (now, reports)
    return dict(reports)


def _operations_report_cache_key(
    store: Store,
    config_path: Path,
    backup_dir: Path,
    auto_status: object | None,
    pending_queue: dict[str, object],
) -> tuple[str, ...]:
    return (
        store.display_location,
        str(config_path),
        str(backup_dir),
        str(_auto_status_value(auto_status, "enabled")),
        str(_auto_status_value(auto_status, "running")),
        str(_auto_status_value(auto_status, "last_auto_finished_at")),
        str(pending_queue.get("total") or 0),
    )


def _operations_report_cache_seconds() -> int:
    raw_value = os.getenv(
        "NEWS_SUMMARY_OPERATIONS_REPORT_CACHE_SECONDS",
        str(DEFAULT_OPERATIONS_REPORT_CACHE_SECONDS),
    )
    try:
        seconds = int(raw_value)
    except ValueError:
        seconds = DEFAULT_OPERATIONS_REPORT_CACHE_SECONDS
    return max(0, min(seconds, 120))


def _source_coverage_report(config_path: Path) -> dict[str, object]:
    required_labels = dict(REQUIRED_SOURCE_COVERAGE)
    required_ids = set(required_labels)
    try:
        sources = load_sources(config_path)
    except Exception as exc:  # noqa: BLE001 - operations page should surface config errors.
        return {
            "status_level": "error",
            "status_label": "확인 필요",
            "expected_total": len(REQUIRED_SOURCE_COVERAGE),
            "configured_required_count": 0,
            "enabled_required_count": 0,
            "configured_total": 0,
            "missing_labels": list(required_labels.values()),
            "disabled_labels": [],
            "duplicate_ids": [],
            "extra_ids": [],
            "message": f"수집 설정을 읽지 못했습니다: {type(exc).__name__}",
        }

    id_counts = Counter(source.id for source in sources)
    configured_ids = set(id_counts)
    enabled_ids = {source.id for source in sources if source.enabled}
    missing_ids = [source_id for source_id, _label in REQUIRED_SOURCE_COVERAGE if source_id not in configured_ids]
    disabled_ids = [
        source_id
        for source_id, _label in REQUIRED_SOURCE_COVERAGE
        if source_id in configured_ids and source_id not in enabled_ids
    ]
    duplicate_ids = sorted(source_id for source_id, count in id_counts.items() if count > 1)
    extra_ids = sorted(configured_ids - required_ids)
    issues = bool(missing_ids or disabled_ids or duplicate_ids)
    if missing_ids:
        message = f"필수 기관 {len(missing_ids)}곳이 설정에서 누락됐습니다."
    elif disabled_ids:
        message = f"필수 기관 {len(disabled_ids)}곳이 비활성화되어 있습니다."
    elif duplicate_ids:
        message = f"중복 소스 ID {len(duplicate_ids)}개를 확인해야 합니다."
    elif extra_ids:
        message = "필수 기관은 모두 포함됐고 추가 소스가 있습니다."
    else:
        message = "광주·전남 필수 수집 대상이 모두 포함되어 있습니다."
    return {
        "status_level": "warning" if issues else "ok",
        "status_label": "확인 필요" if issues else "정상",
        "expected_total": len(REQUIRED_SOURCE_COVERAGE),
        "configured_required_count": len(required_ids & configured_ids),
        "enabled_required_count": len(required_ids & enabled_ids),
        "configured_total": len(sources),
        "missing_labels": [required_labels[source_id] for source_id in missing_ids],
        "disabled_labels": [required_labels[source_id] for source_id in disabled_ids],
        "duplicate_ids": duplicate_ids,
        "extra_ids": extra_ids,
        "message": message,
    }


def _collection_check_coverage_report(store: Store, config_path: Path) -> dict[str, object]:
    try:
        sources = [source for source in load_sources(config_path) if source.enabled]
    except Exception as exc:  # noqa: BLE001 - operations page should surface config errors.
        return {
            "status_level": "warning",
            "status_label": "확인 필요",
            "date": datetime.now(LOCAL_TZ).date().isoformat(),
            "enabled_total": 0,
            "checked_today": 0,
            "success_today": 0,
            "failed_today": 0,
            "today_release_sources": 0,
            "unchecked_labels": [],
            "failed_labels": [],
            "message": f"수집 설정을 읽지 못했습니다: {type(exc).__name__}",
        }

    now = datetime.now(LOCAL_TZ)
    today = now.date()
    holidays = retention_holidays({today.year - 1, today.year, today.year + 1})
    is_business_day = is_collection_business_day(today, holidays)
    check_hour = _collection_coverage_check_hour()
    local_start = datetime.combine(today, datetime.min.time(), tzinfo=LOCAL_TZ).astimezone(timezone.utc).isoformat()
    date_expr = (
        "REPLACE("
        "REPLACE("
        "SUBSTR(TRIM(COALESCE(NULLIF(published_at, ''), collected_at, '')), 1, 10), "
        "'.', '-'"
        "), "
        "'/', '-'"
        ")"
    )
    try:
        with store.connect() as conn:
            status_rows = conn.execute(
                """
                SELECT source_id,
                       SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok_count,
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count
                FROM source_collection_runs
                WHERE checked_at >= ?
                GROUP BY source_id
                """,
                (local_start,),
            ).fetchall()
            release_rows = conn.execute(
                f"""
                SELECT source_id, COUNT(*) AS release_count
                FROM press_releases
                WHERE {date_expr} = ?
                GROUP BY source_id
                """,
                (today.isoformat(),),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 - diagnostics should not break operations page.
        return {
            "status_level": "warning",
            "status_label": "확인 필요",
            "date": today.isoformat(),
            "enabled_total": len(sources),
            "checked_today": 0,
            "success_today": 0,
            "failed_today": 0,
            "today_release_sources": 0,
            "unchecked_labels": [],
            "failed_labels": [],
            "message": f"오늘 수집 점검 현황을 읽지 못했습니다: {type(exc).__name__}",
        }

    source_labels = {source.id: source_display_label(source.name) for source in sources}
    source_ids = set(source_labels)
    checked_ids = {str(row["source_id"]) for row in status_rows if str(row["source_id"]) in source_ids}
    success_ids = {
        str(row["source_id"])
        for row in status_rows
        if str(row["source_id"]) in source_ids and int(row["ok_count"] or 0) > 0
    }
    failed_ids = {
        str(row["source_id"])
        for row in status_rows
        if str(row["source_id"]) in source_ids and int(row["failed_count"] or 0) > 0
    }
    release_ids = {str(row["source_id"]) for row in release_rows if str(row["source_id"]) in source_ids}
    unchecked_ids = [source.id for source in sources if source.id not in checked_ids]
    failed_labels = [source_labels[source_id] for source_id in sorted(failed_ids, key=source_labels.get)]
    unchecked_labels = [source_labels[source_id] for source_id in unchecked_ids]

    if not sources:
        status_level = "warning"
        status_label = "점검 전"
        message = "활성화된 수집 기관이 없습니다."
    elif not is_business_day:
        status_level = "ok"
        status_label = "휴일 대기"
        message = "주말 또는 공휴일이라 오늘 점검 누락을 경고하지 않습니다."
    elif now.hour < check_hour:
        status_level = "ok"
        status_label = "점검 대기"
        message = f"{check_hour}시 이후 오늘 기관별 점검 누락을 판정합니다."
    elif unchecked_ids:
        status_level = "warning"
        status_label = "미점검"
        message = f"오늘 아직 점검되지 않은 기관이 {len(unchecked_ids)}곳 있습니다."
    elif failed_ids:
        status_level = "warning"
        status_label = "실패 포함"
        message = f"전체 기관은 점검됐고 실패 기록 {len(failed_ids)}곳은 자동 복구 대상입니다."
    else:
        status_level = "ok"
        status_label = "정상"
        message = "오늘 활성 기관이 모두 점검됐습니다."

    return {
        "status_level": status_level,
        "status_label": status_label,
        "date": today.isoformat(),
        "enabled_total": len(sources),
        "checked_today": len(checked_ids),
        "success_today": len(success_ids),
        "failed_today": len(failed_ids),
        "today_release_sources": len(release_ids),
        "unchecked_labels": unchecked_labels,
        "failed_labels": failed_labels,
        "message": message,
    }


def _collection_coverage_check_hour() -> int:
    raw_value = os.getenv(
        "NEWS_SUMMARY_COLLECTION_COVERAGE_CHECK_HOUR",
        str(DEFAULT_COLLECTION_COVERAGE_CHECK_HOUR),
    )
    try:
        hour = int(raw_value)
    except ValueError:
        return DEFAULT_COLLECTION_COVERAGE_CHECK_HOUR
    return max(0, min(hour, 23))


def _draft_conversion_coverage_report(store: Store) -> dict[str, object]:
    today = datetime.now(LOCAL_TZ).date().isoformat()
    date_expr = (
        "REPLACE("
        "REPLACE("
        "SUBSTR(TRIM(COALESCE(NULLIF(pr.published_at, ''), pr.collected_at, '')), 1, 10), "
        "'.', '-'"
        "), "
        "'/', '-'"
        ")"
    )
    try:
        with store.connect() as conn:
            summary = conn.execute(
                f"""
                SELECT
                    COUNT(pr.id) AS total_count,
                    SUM(CASE WHEN ad.id IS NULL THEN 0 ELSE 1 END) AS drafted_count,
                    SUM(CASE WHEN ad.id IS NULL THEN 1 ELSE 0 END) AS pending_count
                FROM press_releases pr
                LEFT JOIN article_drafts ad ON ad.press_release_id = pr.id
                WHERE {date_expr} = ?
                """,
                (today,),
            ).fetchone()
            by_source = conn.execute(
                f"""
                SELECT pr.source_name, COUNT(*) AS count
                FROM press_releases pr
                LEFT JOIN article_drafts ad ON ad.press_release_id = pr.id
                WHERE {date_expr} = ?
                  AND ad.id IS NULL
                GROUP BY pr.source_name
                ORDER BY count DESC, pr.source_name ASC
                LIMIT 5
                """,
                (today,),
            ).fetchall()
            latest_pending = conn.execute(
                f"""
                SELECT pr.published_at, pr.collected_at
                FROM press_releases pr
                LEFT JOIN article_drafts ad ON ad.press_release_id = pr.id
                WHERE {date_expr} = ?
                  AND ad.id IS NULL
                ORDER BY COALESCE(NULLIF(pr.published_at, ''), pr.collected_at, '') DESC,
                         pr.id DESC
                LIMIT 1
                """,
                (today,),
            ).fetchone()
            oldest_pending = conn.execute(
                f"""
                SELECT pr.published_at, pr.collected_at
                FROM press_releases pr
                LEFT JOIN article_drafts ad ON ad.press_release_id = pr.id
                WHERE {date_expr} = ?
                  AND ad.id IS NULL
                ORDER BY COALESCE(NULLIF(pr.published_at, ''), pr.collected_at, '') ASC,
                         pr.id ASC
                LIMIT 1
                """,
                (today,),
            ).fetchone()
    except Exception as exc:  # noqa: BLE001 - diagnostics should not break operations page.
        return {
            "status_level": "warning",
            "status_label": "확인 필요",
            "date": today,
            "today_releases": 0,
            "today_drafted": 0,
            "today_pending": 0,
            "drafted_percent": 0,
            "pending_sources": [],
            "latest_pending_at": None,
            "oldest_pending_at": None,
            "message": f"오늘 초안 변환 현황을 읽지 못했습니다: {type(exc).__name__}",
        }

    today_releases = int(summary["total_count"] or 0) if summary else 0
    today_drafted = int(summary["drafted_count"] or 0) if summary else 0
    today_pending = int(summary["pending_count"] or 0) if summary else 0
    drafted_percent = round((today_drafted / today_releases) * 100) if today_releases else 0
    pending_sources = [
        {"source_name": str(row["source_name"]), "count": int(row["count"])}
        for row in by_source
    ]

    if today_releases <= 0:
        status_level = "ok"
        status_label = "대기"
        message = "오늘 수집 원문이 아직 없습니다."
    elif today_pending > 0:
        status_level = "warning"
        status_label = "미변환"
        message = f"오늘 수집 원문 중 초안 미변환 {today_pending}건이 남아 있습니다."
        cooldown_until = gemini_cooldown_until(store)
        if cooldown_until:
            cooldown_label = format_datetime_label(cooldown_until.astimezone(LOCAL_TZ).isoformat())
            message = f"{message} Gemini 재개 예정은 {cooldown_label}입니다."
    else:
        status_level = "ok"
        status_label = "정상"
        message = "오늘 수집 원문이 모두 초안으로 변환됐습니다."

    def pending_time(row) -> str | None:
        if not row:
            return None
        return str(row["published_at"] or row["collected_at"] or "") or None

    return {
        "status_level": status_level,
        "status_label": status_label,
        "date": today,
        "today_releases": today_releases,
        "today_drafted": today_drafted,
        "today_pending": today_pending,
        "drafted_percent": drafted_percent,
        "pending_sources": pending_sources,
        "latest_pending_at": pending_time(latest_pending),
        "oldest_pending_at": pending_time(oldest_pending),
        "message": message,
    }


def _recovery_candidate_report(store: Store, config_path: Path) -> dict[str, object]:
    try:
        limit = int(os.getenv("NEWS_SUMMARY_AUTO_RECOVERY_LIMIT", "5"))
    except ValueError:
        limit = 5
    limit = max(0, limit)
    if limit <= 0:
        return {
            "status_level": "ok",
            "status_label": "꺼짐",
            "count": 0,
            "limit": 0,
            "candidates": [],
            "message": "자동 복구 후보 재검증이 꺼져 있습니다.",
        }
    try:
        candidates = _source_recovery_candidates(store, config_path, limit)
    except Exception as exc:  # noqa: BLE001 - operations page should surface diagnostics without failing.
        return {
            "status_level": "warning",
            "status_label": "확인 필요",
            "count": 0,
            "limit": limit,
            "candidates": [],
            "message": f"자동 복구 후보를 확인하지 못했습니다: {type(exc).__name__}",
        }

    rows = [
        {
            "source_id": candidate.source.id,
            "source_name": candidate.source.name,
            "region": candidate.source.region,
            "reason": candidate.reason,
            "reason_label": RECOVERY_REASON_LABELS.get(candidate.reason, candidate.reason),
        }
        for candidate in candidates
    ]
    if rows:
        message = f"다음 자동 유지보수에서 {len(rows)}곳을 우선 재검증합니다."
        status_level = "warning"
        status_label = "대기"
    else:
        message = "현재 자동 복구 재검증 후보가 없습니다."
        status_level = "ok"
        status_label = "대상 없음"
    return {
        "status_level": status_level,
        "status_label": status_label,
        "count": len(rows),
        "limit": limit,
        "candidates": rows,
        "message": message,
    }


def _clear_operations_report_cache() -> None:
    with _operations_report_cache_lock:
        _operations_report_cache.clear()


def _date_issue_report(store: Store) -> dict[str, object]:
    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, source_name, title, published_at
            FROM press_releases
            ORDER BY id DESC
            LIMIT 1000
            """
        ).fetchall()
    issues = []
    by_source: Counter[str] = Counter()
    for row in rows:
        published_at = str(row["published_at"] or "").strip()
        if not published_at or _parse_date(published_at) is None:
            source_name = source_display_label(row["source_name"])
            by_source[source_name] += 1
            if len(issues) < 5:
                issues.append(
                    {
                        "id": row["id"],
                        "source_name": source_name,
                        "title": row["title"],
                        "published_at": published_at or "게시일 없음",
                    }
                )
    return {
        "status_label": "정상" if not issues else "확인 필요",
        "status_level": "ok" if not issues else "warning",
        "issue_count": sum(by_source.values()),
        "by_source": [{"source_name": name, "count": count} for name, count in by_source.most_common(5)],
        "samples": issues,
    }


def _daily_operations_report(store: Store) -> dict[str, object]:
    raw_value = store.get_app_metadata(AUTO_DAILY_REPORT_KEY)
    if raw_value:
        try:
            payload = json.loads(raw_value)
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict):
            return payload
    today = datetime.now(LOCAL_TZ).date().isoformat()
    with store.connect() as conn:
        releases = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM press_releases
            WHERE SUBSTR(TRIM(COALESCE(NULLIF(published_at, ''), collected_at, '')), 1, 10) = ?
            """,
            (today,),
        ).fetchone()["count"]
        drafts = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM article_drafts
            WHERE SUBSTR(TRIM(COALESCE(updated_at, created_at, '')), 1, 10) = ?
            """,
            (today,),
        ).fetchone()["count"]
    pending = store.pending_press_release_summary(limit=1)
    draft_failures = store.draft_generation_failure_summary(limit=1)
    return {
        "date": today,
        "updated_at": None,
        "today_releases": int(releases or 0),
        "today_drafts": int(drafts or 0),
        "pending_releases": int(pending.get("total") or 0),
        "failed_sources": 0,
        "draft_failures": int(draft_failures.get("total") or 0),
        "draft_retry_due": int(draft_failures.get("due") or 0),
        "messages": [],
    }


def _operations_summary_report(store: Store) -> dict[str, object]:
    return _metadata_json_report(
        store,
        AUTO_OPERATIONS_SUMMARY_STATUS_KEY,
        {
            "date": datetime.now(LOCAL_TZ).date().isoformat(),
            "updated_at": None,
            "source_successes": 0,
            "source_failures": 0,
            "recovery_successes": 0,
            "today_active_sources": 0,
            "draft_failures": int(store.draft_generation_failure_summary(limit=1).get("total") or 0),
            "anomaly_count": 0,
            "dedupe_merged": 0,
            "server_status_level": "unknown",
            "messages": [],
        },
    )


def _server_health_report(store: Store) -> dict[str, object]:
    return _metadata_json_report(
        store,
        AUTO_SERVER_HEALTH_STATUS_KEY,
        {
            "updated_at": None,
            "status_level": "neutral",
            "status_label": "점검 전",
            "message": "아직 자동 서버 점검 기록이 없습니다.",
            "checks": [],
        },
    )


def _collection_anomaly_report(store: Store) -> dict[str, object]:
    return _metadata_json_report(
        store,
        AUTO_COLLECTION_ANOMALY_STATUS_KEY,
        {
            "updated_at": None,
            "status_level": "neutral",
            "status_label": "점검 전",
            "issue_count": 0,
            "issues": [],
        },
    )


def _deduplicate_report(store: Store) -> dict[str, object]:
    return _metadata_json_report(
        store,
        AUTO_DEDUPLICATE_STATUS_KEY,
        {
            "updated_at": None,
            "groups": 0,
            "merged": 0,
            "skipped": 0,
        },
    )


def _metadata_json_report(store: Store, key: str, default: dict[str, object]) -> dict[str, object]:
    raw_value = store.get_app_metadata(key)
    if not raw_value:
        return dict(default)
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError:
        return dict(default)
    if not isinstance(payload, dict):
        return dict(default)
    merged = dict(default)
    merged.update(payload)
    return merged


def _url_discovery_report(store: Store) -> dict[str, object]:
    raw_value = store.get_app_metadata(AUTO_URL_DISCOVERY_STATUS_KEY)
    if not raw_value:
        return {"updated_at": None, "discoveries": []}
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError:
        return {"updated_at": None, "discoveries": []}
    if not isinstance(payload, dict):
        return {"updated_at": None, "discoveries": []}
    discoveries = payload.get("discoveries")
    return {
        "updated_at": payload.get("updated_at"),
        "discoveries": discoveries if isinstance(discoveries, list) else [],
    }


def _backup_verify_report(store: Store, backup_dir: Path) -> dict[str, object]:
    latest_backup = _backup_files(backup_dir)[:1]
    raw_value = store.get_app_metadata(AUTO_BACKUP_VERIFY_STATUS_KEY)
    if raw_value:
        try:
            payload = json.loads(raw_value)
        except json.JSONDecodeError:
            payload = {}
        payload_backup_name = str(payload.get("backup_name") or "") if isinstance(payload, dict) else ""
        latest_backup_name = latest_backup[0]["name"] if latest_backup else ""
        if isinstance(payload, dict) and (not latest_backup_name or payload_backup_name == latest_backup_name):
            return {
                "ok": bool(payload.get("ok")),
                "status_label": str(payload.get("status_label") or "검증 기록"),
                "message": str(payload.get("message") or ""),
                "checked_sqlite": bool(payload.get("checked_sqlite")),
                "checked_database_export": bool(payload.get("checked_database_export")),
                "backup_name": str(payload.get("backup_name") or ""),
                "updated_at": payload.get("updated_at"),
            }
    if not latest_backup:
        return {
            "ok": False,
            "status_label": "백업 없음",
            "message": "검증할 백업 파일이 없습니다.",
            "checked_sqlite": False,
            "checked_database_export": False,
            "backup_name": "",
            "updated_at": None,
        }
    result = verify_backup(latest_backup[0]["path"])
    return {
        "backup_name": latest_backup[0]["name"],
        "updated_at": None,
        **result,
    }


def _persist_backup_verification_result(store: Store, backup_path: Path) -> None:
    result = verify_backup(backup_path)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "backup_name": backup_path.name,
        **result,
    }
    store.set_app_metadata(AUTO_BACKUP_VERIFY_STATUS_KEY, json.dumps(payload, ensure_ascii=False))


def _fallback_url_report(config_path: Path) -> dict[str, object]:
    sources = [source for source in load_sources(config_path) if source.enabled]
    prepared = [source for source in sources if source.fallback_urls]
    return {
        "prepared_count": len(prepared),
        "total_count": len(sources),
        "sources": [
            {"name": source_display_label(source.name), "count": len(source.fallback_urls)}
            for source in prepared[:8]
        ],
    }


def _automation_settings_report() -> dict[str, object]:
    return {
        "queue_drain": os.getenv("NEWS_SUMMARY_AUTO_QUEUE_DRAIN", "1"),
        "queue_interval": os.getenv("NEWS_SUMMARY_AUTO_QUEUE_DRAIN_INTERVAL_SECONDS", "900"),
        "queue_limit": os.getenv("NEWS_SUMMARY_AUTO_QUEUE_DRAIN_LIMIT", "25"),
        "recovery_interval": os.getenv("NEWS_SUMMARY_AUTO_RECOVERY_INTERVAL_SECONDS", "900"),
        "recovery_limit": os.getenv("NEWS_SUMMARY_AUTO_RECOVERY_LIMIT", "5"),
        "network_recheck_cooldown": os.getenv("NEWS_SUMMARY_AUTO_NETWORK_FAILURE_RECHECK_COOLDOWN_SECONDS", "21600"),
        "quiet_recheck": os.getenv("NEWS_SUMMARY_AUTO_QUIET_SOURCE_RECHECK", "1"),
        "quiet_recheck_hour": os.getenv("NEWS_SUMMARY_AUTO_QUIET_SOURCE_RECHECK_HOUR", "9"),
        "focused_recrawl_limit": os.getenv("NEWS_SUMMARY_AUTO_FOCUSED_RECRAWL_LIMIT", "3"),
        "anomaly_check_hour": os.getenv("NEWS_SUMMARY_AUTO_ANOMALY_CHECK_HOUR", "10"),
        "deduplicate_limit": os.getenv("NEWS_SUMMARY_AUTO_DEDUPLICATE_LIMIT", "50"),
        "url_discovery_limit": os.getenv("NEWS_SUMMARY_AUTO_URL_DISCOVERY_LIMIT", "3"),
        "backup_create": os.getenv("NEWS_SUMMARY_AUTO_BACKUP_CREATE", "1"),
        "backup_max_age_hours": os.getenv("NEWS_SUMMARY_AUTO_BACKUP_MAX_AGE_HOURS", "24"),
        "backup_keep_count": os.getenv("NEWS_SUMMARY_AUTO_BACKUP_KEEP_COUNT", "7"),
        "backup_verify": os.getenv("NEWS_SUMMARY_AUTO_BACKUP_VERIFY", "1"),
    }


def _operations_health_report(
    store: Store,
    auto_status: object | None,
    pending_queue: dict[str, object],
) -> dict[str, object]:
    now = datetime.now(LOCAL_TZ)
    since = (now - timedelta(hours=24)).astimezone(timezone.utc).isoformat()
    issues: list[str] = []
    try:
        with store.connect() as conn:
            recent_rows = conn.execute(
                """
                SELECT *
                FROM source_collection_runs
                WHERE checked_at >= ?
                ORDER BY id DESC
                """,
                (since,),
            ).fetchall()
            latest_rows = conn.execute(
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
            status_sequence_rows = conn.execute(
                """
                SELECT source_id, status
                FROM source_collection_runs
                ORDER BY source_id, id DESC
                """
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 - operations page should stay usable during diagnostics.
        return {
            "status_level": "error",
            "status_label": "DB 확인 필요",
            "failure_count": 0,
            "retry_success_count": 0,
            "unresolved_count": 0,
            "temporary_count": 0,
            "pending_total": int(pending_queue.get("total") or 0),
            "last_auto_finished_at": _auto_status_value(auto_status, "last_auto_finished_at"),
            "top_failure_stages": [],
            "issues": [f"운영 점검 DB 조회 실패: {type(exc).__name__}"],
        }

    failure_rows = [row for row in recent_rows if str(row["status"]) == "failed"]
    retry_success_rows = [
        row
        for row in recent_rows
        if str(row["status"]) == "ok" and "자동 재검증 통과" in str(row["message"] or "")
    ]
    stage_counts = Counter(str(row["failure_stage"] or "수집 실패") for row in failure_rows)
    top_failure_stages = [
        {"stage": stage, "count": count}
        for stage, count in stage_counts.most_common(3)
    ]

    consecutive_failures = _consecutive_failure_counts(status_sequence_rows)
    unresolved_rows = [
        row
        for row in latest_rows
        if str(row["status"]) == "failed" and consecutive_failures.get(str(row["source_id"]), 0) >= 3
        and not is_transient_site_failure(str(row["failure_stage"] or ""), str(row["failure_reason"] or ""))
    ]
    temporary_rows = [
        row
        for row in latest_rows
        if str(row["status"]) == "failed"
        and (
            consecutive_failures.get(str(row["source_id"]), 0) < 3
            or is_transient_site_failure(str(row["failure_stage"] or ""), str(row["failure_reason"] or ""))
        )
    ]

    if unresolved_rows:
        labels = [
            f"{row['source_name']} {consecutive_failures.get(str(row['source_id']), 0)}회 연속 실패"
            for row in unresolved_rows[:3]
        ]
        issues.append("미복구 기관: " + ", ".join(labels))
    if temporary_rows:
        issues.append(f"일시 장애 재검증 대상 {len(temporary_rows)}곳")

    auto_enabled = bool(_auto_status_value(auto_status, "enabled"))
    auto_running = bool(_auto_status_value(auto_status, "running"))
    auto_thread_alive = _auto_status_value(auto_status, "thread_alive")
    last_auto_finished_at = _auto_status_value(auto_status, "last_auto_finished_at")
    last_auto_finished = _parse_datetime(last_auto_finished_at)
    stale_running_snapshot_at = _auto_status_value(auto_status, "stale_running_snapshot_at")
    timing_health = _auto_collector_timing_health(auto_status) if auto_status is not None else None
    if auto_status is None:
        issues.append("자동 수집 컨트롤러 미감지")
    elif _auto_status_value(auto_status, "stale_running_snapshot"):
        label = format_datetime_label(stale_running_snapshot_at) if stale_running_snapshot_at else "시각 확인 불가"
        issues.append(f"오래된 자동 수집 실행 표시 자동 보정: {label}")
    elif not auto_enabled:
        issues.append("자동 수집 꺼짐")
    elif auto_thread_alive is False and not auto_running:
        issues.append("자동 수집 백그라운드 스레드 중단")
    elif timing_health and timing_health.get("overdue"):
        issues.append(str(timing_health.get("message") or "자동 수집 실행 지연"))
    elif last_auto_finished:
        minutes_since_auto = int((now - last_auto_finished.astimezone(LOCAL_TZ)).total_seconds() // 60)
        if minutes_since_auto >= 90 and not auto_running:
            issues.append(f"마지막 자동 수집 후 {minutes_since_auto}분 경과")
    elif not auto_running:
        issues.append("자동 수집 완료 기록 없음")

    cooldown_until = gemini_cooldown_until(store)
    if cooldown_until:
        cooldown_issue = f"Gemini 쿨다운 중: {format_datetime_label(cooldown_until)}까지"
        cooldown_reason = store.get_app_metadata(GEMINI_COOLDOWN_REASON_KEY)
        if cooldown_reason:
            reason_label = re.sub(r"\s+", " ", str(cooldown_reason)).strip()
            if len(reason_label) > 140:
                reason_label = reason_label[:137].rstrip() + "..."
            cooldown_issue = f"{cooldown_issue} · 사유: {reason_label}"
        issues.append(cooldown_issue)

    pending_total = int(pending_queue.get("total") or 0)
    if pending_total >= 100:
        issues.append(f"Gemini 미변환 큐 {pending_total}건")
    try:
        draft_failure_summary = store.draft_generation_failure_summary(limit=1)
    except Exception as exc:  # noqa: BLE001 - operations health should surface the problem, not break the page.
        draft_failure_total = 0
        draft_retry_due = 0
        issues.append(f"Gemini 실패 큐 확인 실패: {type(exc).__name__}")
    else:
        draft_failure_total = int(draft_failure_summary.get("total") or 0)
        draft_retry_due = int(draft_failure_summary.get("due") or 0)
        retry_warning_count = _gemini_retry_due_warning_count()
        if draft_retry_due >= retry_warning_count:
            issues.append(f"Gemini 재시도 가능 실패 큐 {draft_retry_due}건")
        elif draft_failure_total >= 100:
            issues.append(f"Gemini 실패 큐 {draft_failure_total}건")

    if unresolved_rows:
        status_level = "error"
        status_label = "확인 필요"
    elif issues:
        status_level = "warning"
        status_label = "주의"
    else:
        status_level = "ok"
        status_label = "정상"

    return {
        "status_level": status_level,
        "status_label": status_label,
        "failure_count": len(failure_rows),
        "retry_success_count": len(retry_success_rows),
        "unresolved_count": len(unresolved_rows),
        "temporary_count": len(temporary_rows),
        "pending_total": pending_total,
        "draft_failure_total": draft_failure_total,
        "draft_retry_due": draft_retry_due,
        "last_auto_finished_at": last_auto_finished_at,
        "top_failure_stages": top_failure_stages,
        "issues": issues[:5],
    }


def _gemini_queue_health_payload(store: Store) -> dict[str, object]:
    try:
        pending_queue = store.pending_press_release_summary(limit=1)
        draft_failure_summary = store.draft_generation_failure_summary(limit=1)
    except Exception as exc:  # noqa: BLE001 - health check should remain readable if queue diagnostics fail.
        logger.warning("gemini queue health check failed error=%s", exc)
        return {
            "gemini_queue_status": "error",
            "gemini_pending_total": None,
            "gemini_failure_total": None,
            "gemini_retry_due": None,
            "gemini_cooldown_active": None,
            "gemini_cooldown_until": None,
            "gemini_cooldown_reason": None,
            "gemini_queue_message": f"Gemini 대기열 확인 실패: {type(exc).__name__}",
        }

    pending_total = int(pending_queue.get("total") or 0)
    failure_total = int(draft_failure_summary.get("total") or 0)
    retry_due = int(draft_failure_summary.get("due") or 0)
    cooldown_until = gemini_cooldown_until(store)
    cooldown_reason = store.get_app_metadata(GEMINI_COOLDOWN_REASON_KEY) if cooldown_until else None
    if cooldown_until:
        status = "warning"
        message = f"Gemini 쿨다운 중: {format_datetime_label(cooldown_until)}까지"
    elif retry_due >= _gemini_retry_due_warning_count():
        status = "warning"
        message = f"Gemini 재시도 가능 실패 큐 {retry_due}건"
    elif failure_total >= 100:
        status = "warning"
        message = f"Gemini 실패 큐 {failure_total}건"
    elif pending_total >= 100:
        status = "warning"
        message = f"Gemini 미변환 큐 {pending_total}건"
    else:
        status = "ok"
        message = "Gemini 대기열 정상 범위"
    return {
        "gemini_queue_status": status,
        "gemini_pending_total": pending_total,
        "gemini_failure_total": failure_total,
        "gemini_retry_due": retry_due,
        "gemini_cooldown_active": bool(cooldown_until),
        "gemini_cooldown_until": cooldown_until.isoformat() if cooldown_until else None,
        "gemini_cooldown_reason": cooldown_reason,
        "gemini_queue_message": message,
    }


def _source_collection_health_payload(store: Store) -> dict[str, object]:
    since = (datetime.now(LOCAL_TZ) - timedelta(hours=24)).astimezone(timezone.utc).isoformat()
    try:
        with store.connect() as conn:
            recent_rows = conn.execute(
                """
                SELECT *
                FROM source_collection_runs
                WHERE checked_at >= ?
                ORDER BY id DESC
                """,
                (since,),
            ).fetchall()
            latest_rows = conn.execute(
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
            status_sequence_rows = conn.execute(
                """
                SELECT source_id, status
                FROM source_collection_runs
                ORDER BY source_id, id DESC
                """
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 - health check should expose collection diagnostics failures.
        logger.warning("source collection health check failed error=%s", exc)
        return {
            "source_collection_status": "error",
            "source_collection_recent_failure_count": None,
            "source_collection_unresolved_count": None,
            "source_collection_temporary_count": None,
            "source_collection_unresolved_sources": [],
            "source_collection_message": f"수집 상태 확인 실패: {type(exc).__name__}",
        }

    failure_rows = [row for row in recent_rows if str(row["status"]) == "failed"]
    consecutive_failures = _consecutive_failure_counts(status_sequence_rows)
    unresolved_rows = [
        row
        for row in latest_rows
        if str(row["status"]) == "failed"
        and consecutive_failures.get(str(row["source_id"]), 0) >= 3
        and not is_transient_site_failure(str(row["failure_stage"] or ""), str(row["failure_reason"] or ""))
    ]
    temporary_rows = [
        row
        for row in latest_rows
        if str(row["status"]) == "failed"
        and (
            consecutive_failures.get(str(row["source_id"]), 0) < 3
            or is_transient_site_failure(str(row["failure_stage"] or ""), str(row["failure_reason"] or ""))
        )
    ]
    if unresolved_rows:
        status = "error"
        message = f"미복구 수집 실패 기관 {len(unresolved_rows)}곳"
    elif temporary_rows:
        status = "warning"
        message = f"일시 장애 재검증 대상 {len(temporary_rows)}곳"
    elif failure_rows:
        status = "warning"
        message = f"최근 24시간 수집 실패 {len(failure_rows)}건"
    else:
        status = "ok"
        message = "지자체 수집 상태 정상 범위"
    return {
        "source_collection_status": status,
        "source_collection_recent_failure_count": len(failure_rows),
        "source_collection_unresolved_count": len(unresolved_rows),
        "source_collection_temporary_count": len(temporary_rows),
        "source_collection_unresolved_sources": [
            {
                "source_id": str(row["source_id"]),
                "source_name": source_display_label(str(row["source_name"])),
                "consecutive_failures": consecutive_failures.get(str(row["source_id"]), 0),
                "failure_stage": str(row["failure_stage"] or "수집 실패"),
            }
            for row in unresolved_rows[:5]
        ],
        "source_collection_message": message,
    }


def _auto_status_value(auto_status: object | None, name: str) -> object | None:
    if auto_status is None:
        return None
    if isinstance(auto_status, dict):
        return auto_status.get(name)
    return getattr(auto_status, name, None)


def _auto_collector_health_label(auto_status: dict[str, object]) -> str:
    if auto_status.get("running"):
        return "running"
    if not auto_status.get("enabled"):
        return "disabled"
    if auto_status.get("thread_alive") is False:
        return "stopped"
    return "enabled"


def _auto_collector_timing_health(auto_status: object | None) -> dict[str, object]:
    if not auto_status or not bool(_auto_status_value(auto_status, "enabled")):
        return {
            "status": "disabled",
            "overdue": False,
            "lag_minutes": None,
            "run_minutes": None,
            "schedule_delay_minutes": None,
            "message": "자동 수집이 꺼져 있습니다.",
        }
    if bool(_auto_status_value(auto_status, "running")):
        run_minutes = _minutes_since(_auto_status_value(auto_status, "last_started_at"))
        running_warn_minutes = _auto_running_warning_minutes()
        if run_minutes is not None and run_minutes >= running_warn_minutes:
            return {
                "status": "warning",
                "overdue": True,
                "lag_minutes": _minutes_since(_auto_status_value(auto_status, "last_auto_finished_at")),
                "run_minutes": run_minutes,
                "schedule_delay_minutes": None,
                "message": f"자동 수집이 {run_minutes}분째 실행 중입니다.",
            }
        return {
            "status": "running",
            "overdue": False,
            "lag_minutes": _minutes_since(_auto_status_value(auto_status, "last_auto_finished_at")),
            "run_minutes": run_minutes,
            "schedule_delay_minutes": None,
            "message": "자동 수집이 실행 중입니다.",
        }

    lag_minutes = _minutes_since(_auto_status_value(auto_status, "last_auto_finished_at"))
    raw_schedule_delay_minutes = _minutes_after(_auto_status_value(auto_status, "next_run_at"))
    schedule_delay_minutes = (
        max(raw_schedule_delay_minutes, 0) if raw_schedule_delay_minutes is not None else None
    )
    finish_overdue_minutes = _auto_finish_overdue_minutes(_auto_status_value(auto_status, "interval_seconds"))
    next_run_grace_minutes = _auto_next_run_grace_minutes()
    finish_overdue = lag_minutes is not None and lag_minutes >= finish_overdue_minutes
    schedule_overdue = raw_schedule_delay_minutes is not None and raw_schedule_delay_minutes >= next_run_grace_minutes
    if finish_overdue:
        return {
            "status": "warning",
            "overdue": True,
            "lag_minutes": lag_minutes,
            "run_minutes": None,
            "schedule_delay_minutes": schedule_delay_minutes,
            "message": f"마지막 자동 수집 후 {lag_minutes}분이 지났습니다.",
        }
    if schedule_overdue:
        return {
            "status": "warning",
            "overdue": True,
            "lag_minutes": lag_minutes,
            "run_minutes": None,
            "schedule_delay_minutes": schedule_delay_minutes,
            "message": f"다음 실행 예정 시각이 {schedule_delay_minutes}분 지났습니다.",
        }
    return {
        "status": "ok",
        "overdue": False,
        "lag_minutes": lag_minutes,
        "run_minutes": None,
        "schedule_delay_minutes": schedule_delay_minutes,
        "message": "자동 수집 시간 상태 정상",
    }


def _minutes_since(value: object) -> int | None:
    parsed = _parse_datetime(value)
    if not parsed:
        return None
    return max(0, int((datetime.now(LOCAL_TZ) - parsed.astimezone(LOCAL_TZ)).total_seconds() // 60))


def _minutes_after(value: object) -> int | None:
    parsed = _parse_datetime(value)
    if not parsed:
        return None
    return int((datetime.now(LOCAL_TZ) - parsed.astimezone(LOCAL_TZ)).total_seconds() // 60)


def _auto_finish_overdue_minutes(interval_seconds: object | None = None) -> int:
    raw_value = os.getenv("NEWS_SUMMARY_AUTO_FINISH_OVERDUE_MINUTES")
    if raw_value:
        try:
            return max(30, int(raw_value))
        except ValueError:
            return DEFAULT_AUTO_FINISH_OVERDUE_MINUTES
    try:
        interval_minutes = int(interval_seconds or 0) // 60
    except (TypeError, ValueError):
        interval_minutes = 0
    return max(DEFAULT_AUTO_FINISH_OVERDUE_MINUTES, interval_minutes + 30)


def _auto_next_run_grace_minutes() -> int:
    raw_value = os.getenv("NEWS_SUMMARY_AUTO_NEXT_RUN_GRACE_MINUTES", str(DEFAULT_AUTO_NEXT_RUN_GRACE_MINUTES))
    try:
        return max(1, int(raw_value))
    except ValueError:
        return DEFAULT_AUTO_NEXT_RUN_GRACE_MINUTES


def _ensure_auto_collector_running(auto_collector: object | None) -> bool:
    ensure_running = getattr(auto_collector, "ensure_running", None)
    if not callable(ensure_running):
        return False
    try:
        return bool(ensure_running())
    except Exception:  # noqa: BLE001 - health display must stay available even if recovery fails.
        logger.exception("auto collector self-heal failed")
        return False


def _cloudflare_quick_tunnel_status(log_path: Path | None = None) -> dict[str, object]:
    log_path = log_path or PROJECT_ROOT / "data" / "tmp" / "cloudflare_quick_tunnel.err.log"
    if os.getenv("RENDER") or os.getenv("RENDER_SERVICE_ID"):
        public_url = os.getenv("NEWS_SUMMARY_PUBLIC_URL", "").strip()
        return {
            "running": False,
            "public_url": public_url,
            "log_path": str(log_path),
            "updated_at": None,
            "label": "Render 공개 URL 사용",
        }
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


def _operations_write_access_unlocked(store: Store) -> bool:
    if not _configured_admin_password_source(store) or not session.get(OPERATIONS_WRITE_UNLOCKED_KEY):
        return False
    expires_at = _operations_write_access_expires_at()
    if not expires_at or datetime.now(LOCAL_TZ) >= expires_at:
        session.pop(OPERATIONS_WRITE_UNLOCKED_KEY, None)
        session.pop(OPERATIONS_WRITE_UNLOCKED_AT_KEY, None)
        return False
    return True


def _unlock_operations_write_session() -> None:
    session[OPERATIONS_WRITE_UNLOCKED_KEY] = True
    session[OPERATIONS_WRITE_UNLOCKED_AT_KEY] = datetime.now(timezone.utc).isoformat()


def _operations_write_access_expires_at() -> datetime | None:
    unlocked_at = _parse_datetime(session.get(OPERATIONS_WRITE_UNLOCKED_AT_KEY))
    if not unlocked_at:
        return None
    return unlocked_at + timedelta(minutes=_operations_write_unlock_minutes())


def _operations_write_unlock_minutes() -> int:
    raw_value = os.getenv(
        "NEWS_SUMMARY_OPERATIONS_WRITE_UNLOCK_MINUTES",
        str(DEFAULT_OPERATIONS_WRITE_UNLOCK_MINUTES),
    )
    try:
        minutes = int(raw_value)
    except ValueError:
        return DEFAULT_OPERATIONS_WRITE_UNLOCK_MINUTES
    return max(5, min(minutes, 240))


def _require_operations_write_access(store: Store):
    if _operations_write_access_unlocked(store):
        return None
    if not _configured_admin_password_source(store):
        flash("운영 변경 기능을 사용하려면 관리자 비밀번호를 먼저 설정하세요.")
        return redirect(url_for("admin_setup"))
    flash("운영 변경 기능은 관리자 비밀번호 확인 후 사용할 수 있습니다.")
    return redirect(url_for("operations"))


def _safe_next(default_endpoint: str = "dashboard") -> str:
    target = request.args.get("next") or request.form.get("next") or url_for(default_endpoint)
    if not target.startswith("/") or target.startswith("//"):
        return url_for(default_endpoint)
    return target


def _auto_collector_status_payload(store: Store, status) -> dict[str, object]:
    payload = {
        "enabled": status.enabled,
        "running": status.running,
        "thread_alive": getattr(status, "thread_alive", None),
        "interval_seconds": status.interval_seconds,
        "collect_limit": status.collect_limit,
        "draft_limit": status.draft_limit,
        "require_gemini": status.require_gemini,
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
    if _stale_running_status(stored_payload, stored_updated_at):
        payload["stale_running_snapshot"] = True
        payload["stale_running_snapshot_at"] = stored_payload.get("status_updated_at") or stored_payload.get("last_started_at")
        if payload.get("enabled") and not payload.get("running"):
            payload["progress_message"] = "이전 실행 상태 만료, 다음 정각 자동 수집 대기 중"
        return payload
    if bool(stored_payload.get("running")) or current_updated_at is None or (
        stored_updated_at and stored_updated_at >= current_updated_at
    ):
        for key in payload:
            if key in stored_payload:
                payload[key] = stored_payload[key]
        payload["status_updated_at"] = stored_payload.get("status_updated_at")
    return payload


def _stale_running_status(payload: dict[str, object], updated_at: datetime | None = None) -> bool:
    if not bool(payload.get("running")):
        return False
    last_update = updated_at or _parse_datetime(payload.get("status_updated_at")) or _parse_datetime(payload.get("last_started_at"))
    if not last_update:
        return False
    stale_minutes = _auto_running_stale_minutes()
    age_seconds = (datetime.now(LOCAL_TZ) - last_update.astimezone(LOCAL_TZ)).total_seconds()
    return age_seconds >= stale_minutes * 60


def _auto_running_stale_minutes() -> int:
    raw_value = os.getenv("NEWS_SUMMARY_AUTO_RUNNING_STALE_MINUTES", str(DEFAULT_AUTO_RUNNING_STALE_MINUTES))
    try:
        minutes = int(raw_value)
    except ValueError:
        return DEFAULT_AUTO_RUNNING_STALE_MINUTES
    return max(30, minutes)


def _auto_running_warning_minutes() -> int:
    raw_value = os.getenv("NEWS_SUMMARY_AUTO_RUNNING_WARN_MINUTES", str(DEFAULT_AUTO_RUNNING_WARN_MINUTES))
    try:
        minutes = int(raw_value)
    except ValueError:
        return DEFAULT_AUTO_RUNNING_WARN_MINUTES
    return max(30, minutes)


def _gemini_retry_due_warning_count() -> int:
    raw_value = os.getenv("NEWS_SUMMARY_GEMINI_RETRY_DUE_WARNING_COUNT", str(DEFAULT_GEMINI_RETRY_DUE_WARNING_COUNT))
    try:
        count = int(raw_value)
    except ValueError:
        return DEFAULT_GEMINI_RETRY_DUE_WARNING_COUNT
    return max(1, count)


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
    if _should_prune_visitor_access_logs():
        store.prune_visitor_access_logs(cutoff_iso)


def _should_prune_visitor_access_logs() -> bool:
    global _visitor_access_last_pruned_at
    now = time.monotonic()
    with _visitor_access_prune_lock:
        if now - _visitor_access_last_pruned_at < VISITOR_ACCESS_PRUNE_INTERVAL_SECONDS:
            return False
        _visitor_access_last_pruned_at = now
        return True


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


def _draft_thumbnail_map(store: Store, drafts) -> dict[int, object]:
    release_ids: list[int] = []
    for draft in drafts:
        release_id = _row_value(draft, "press_release_id")
        if release_id is None:
            continue
        release_ids.append(int(release_id))
    if not release_ids:
        return {}

    thumbnails: dict[int, object] = {}
    for release_id, assets in store.press_release_assets_by_ids(list(dict.fromkeys(release_ids))).items():
        for asset in _display_press_assets(assets):
            if asset["is_image"]:
                thumbnails[release_id] = asset
                break
    return thumbnails


def _display_press_assets(assets) -> list[object]:
    return [asset for asset in assets if not is_display_noise_image_asset(asset)]


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


def _dashboard_source_cache_seconds() -> int:
    raw_value = os.getenv(
        "NEWS_SUMMARY_DASHBOARD_SOURCE_CACHE_SECONDS",
        str(DEFAULT_DASHBOARD_SOURCE_CACHE_SECONDS),
    )
    try:
        seconds = int(raw_value)
    except ValueError:
        seconds = DEFAULT_DASHBOARD_SOURCE_CACHE_SECONDS
    return max(0, seconds)


def _dashboard_source_summaries(store: Store, config_path: Path) -> list[dict[str, object]]:
    ttl_seconds = _dashboard_source_cache_seconds()
    if ttl_seconds <= 0:
        return _source_summaries(store, config_path)

    cache_key = (store.display_location, str(config_path.resolve()))
    now = time.monotonic()
    with _dashboard_source_summary_cache_lock:
        cached = _dashboard_source_summary_cache.get(cache_key)
        if cached and now - cached[0] <= ttl_seconds:
            return [dict(summary) for summary in cached[1]]

    summaries = _source_summaries(store, config_path)
    cached_summaries = [dict(summary) for summary in summaries]
    with _dashboard_source_summary_cache_lock:
        if len(_dashboard_source_summary_cache) >= 16:
            _dashboard_source_summary_cache.clear()
        _dashboard_source_summary_cache[cache_key] = (now, cached_summaries)
    return [dict(summary) for summary in cached_summaries]


def _attention_count_for_dashboard(
    store: Store,
    selected_regions: list[str] | None,
    duplicate_titles: set[str],
) -> int:
    where = ["ad.status = 'needs_review'"]
    params: list[object] = []
    if selected_regions:
        region_condition, region_params = _region_sql_condition("pr.region", selected_regions)
        where.append(f"({region_condition})")
        params.extend(region_params)
    where_sql = "WHERE " + " AND ".join(where)
    with store.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT ad.title, ad.body, ad.review_note, ad.model,
                   '' AS original_content,
                   pr.title AS original_title, pr.published_at,
                   pr.validation_status, pr.validation_note
            FROM article_drafts ad
            JOIN press_releases pr ON pr.id = ad.press_release_id
            {where_sql}
            """,
            tuple(params),
        ).fetchall()
    return sum(1 for draft in rows if review_flags(draft, duplicate_titles))


def _consecutive_failure_counts(rows) -> dict[str, int]:
    counts: dict[str, int] = {}
    stopped_sources: set[str] = set()
    for row in rows:
        source_id = str(row["source_id"])
        if source_id in stopped_sources:
            continue
        if str(row["status"]) == "failed":
            counts[source_id] = counts.get(source_id, 0) + 1
        else:
            stopped_sources.add(source_id)
    return counts


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
        recent_status_rows = conn.execute(
            """
            SELECT source_id, status
            FROM source_collection_runs
            ORDER BY source_id, id DESC
            """
        ).fetchall()
    consecutive_failure_counts = _consecutive_failure_counts(recent_status_rows)

    summaries = []
    for source in sources:
        stat = stats.get(source.id)
        latest_row = latest.get(source.id)
        status_row = source_statuses.get(source.id)
        success_row = latest_success.get(source.id)
        releases = int(stat["releases"]) if stat else 0
        today_releases = int(stat["today_releases"] or 0) if stat else 0
        yesterday_releases = int(stat["yesterday_releases"] or 0) if stat else 0
        last_collected_datetime = _parse_datetime(stat["last_collected"]) if stat else None
        last_collected_date = last_collected_datetime.astimezone(LOCAL_TZ).date() if last_collected_datetime else None
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
        consecutive_failures = consecutive_failure_counts.get(source.id, 0)
        last_success_datetime = _parse_datetime(success_row["checked_at"]) if success_row else None
        last_success_date = last_success_datetime.astimezone(LOCAL_TZ).date() if last_success_datetime else None
        has_success_today = last_success_date == today
        has_current_day_data = today_releases > 0 or last_collected_date == today
        if last_status == "failed":
            transient_site_failure = is_transient_site_failure(failure_stage, failure_reason)
            if consecutive_failures < 3 or has_current_day_data:
                issue = "" if has_current_day_data or has_success_today else "일시 지연"
                status_label = "정상" if has_current_day_data or has_success_today else "일시 지연"
                status_level = "ok" if has_current_day_data or has_success_today else "warning"
                temporary_cause = " · ".join(
                    item for item in (str(failure_stage or ""), str(failure_reason or "")) if item
                )
                cause_suffix = f" 최근 원인: {temporary_cause}" if temporary_cause else ""
                if has_current_day_data:
                    current_data_label = (
                        f"오늘 원문 {today_releases}건이 수집돼"
                        if today_releases > 0
                        else "오늘 원문 보관 기록이 있어"
                    )
                    status_detail = (
                        f"{current_data_label} 정상으로 봅니다. "
                        f"최근 연결 재점검 {consecutive_failures}회 실패 기록은 자동 복구 대상으로 유지합니다.{cause_suffix}"
                    )
                elif has_success_today:
                    status_detail = (
                        f"오늘 점검은 성공했고 최근 {consecutive_failures}회 연결 점검만 실패했습니다. "
                        f"정상 점검 기록을 우선 반영합니다.{cause_suffix}"
                    )
                else:
                    status_detail = (
                        f"최근 {consecutive_failures}회 연결 점검이 실패했습니다. "
                        f"3회 연속 실패 전까지 일시 지연으로 봅니다.{cause_suffix}"
                    )
            elif transient_site_failure:
                issue = "연결 대기"
                status_label = "연결 대기"
                status_level = "warning"
                temporary_cause = " · ".join(
                    item for item in (str(failure_stage or ""), str(failure_reason or "")) if item
                )
                cause_suffix = f" 최근 원인: {temporary_cause}" if temporary_cause else ""
                status_detail = (
                    "외부 사이트 연결 장애가 지속 중입니다. "
                    "자동 수집과 복구 점검이 낮은 빈도로 계속 재시도합니다."
                    f"{cause_suffix}"
                )
            else:
                issue = str(failure_stage or "수집 실패")
                status_label = "수집 실패"
                status_level = "error"
                status_detail = f"{consecutive_failures}회 연속 실패했습니다. {failure_reason or last_message or ''}".strip()
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
                "consecutive_failures": consecutive_failures,
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
        "consecutive_failures": 0,
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
