from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from flask import Flask, Response, flash, jsonify, redirect, render_template, request, url_for
from werkzeug.exceptions import HTTPException

from .exporter import export_approved
from .ops_logging import configure_logging, get_logger
from .service import collect_and_draft_cycle, collect_enabled_sources, draft_pending_releases
from .settings import env_path, load_environment, load_sources
from .storage import Store
from .writing_settings import DEFAULT_WRITING_SETTINGS, custom_prompt_section, load_writing_settings, save_writing_settings
from .writer import GeminiRefineError, refine_draft_with_gemini


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


def create_app() -> Flask:
    load_environment()
    configure_logging()
    logger = get_logger("web")
    app = Flask(__name__)
    app.secret_key = "local-news-summary-review"
    app.jinja_env.globals["status_label"] = status_label
    app.jinja_env.globals["status_badge_class"] = status_badge_class
    app.jinja_env.globals["model_label"] = model_label
    app.jinja_env.globals["model_badge_class"] = model_badge_class
    app.jinja_env.globals["interval_label"] = interval_label
    app.jinja_env.globals["review_flags"] = review_flags
    app.jinja_env.globals["approval_checks"] = approval_checks
    app.jinja_env.filters["date_label"] = format_datetime_label

    store = Store(env_path("NEWS_SUMMARY_DB", "data/news_summary.sqlite"))
    config_path = env_path("NEWS_SUMMARY_CONFIG", "config/municipalities.yaml")
    export_dir = env_path("NEWS_SUMMARY_EXPORT_DIR", "exports")
    store.init_db()

    @app.errorhandler(Exception)
    def handle_unexpected_error(exc: Exception):
        if isinstance(exc, HTTPException):
            return exc
        logger.exception("unhandled web error method=%s path=%s", request.method, request.path)
        return "서버 오류가 발생했습니다. 운영 로그를 확인하세요.", 500

    @app.get("/")
    def dashboard():
        pending_drafts = store.drafts(status="needs_review", limit=300)
        draft_groups = _group_drafts_by_recent_dates(pending_drafts)
        auto_collector = app.config.get("AUTO_COLLECTOR")
        duplicate_titles = _duplicate_titles(store)
        attention_count = sum(1 for draft in pending_drafts if review_flags(draft, duplicate_titles))
        auto_status = auto_collector.snapshot() if auto_collector else None
        return render_template(
            "dashboard.html",
            counts=store.counts(),
            draft_groups=draft_groups,
            approved_drafts=store.approved_drafts(limit=200),
            auto_collector_status=auto_status,
            source_summaries=_source_summaries(store, config_path),
            duplicate_titles=duplicate_titles,
            attention_count=attention_count,
        )

    @app.get("/favicon.ico")
    def favicon():
        return Response(status=204)

    @app.get("/gemini-usage")
    def gemini_usage():
        auto_collector = app.config.get("AUTO_COLLECTOR")
        auto_status = auto_collector.snapshot() if auto_collector else None
        return render_template(
            "gemini_usage.html",
            gemini_usage=_gemini_usage_summary(store, auto_status),
        )

    @app.get("/drafts")
    def drafts():
        status = request.args.get("status") or None
        if status and status not in VALID_STATUSES:
            status = None
        target_date = _parse_date(request.args.get("date"))
        query = (request.args.get("q") or "").strip()
        review_filter = (request.args.get("review") or "").strip()
        source_filter = (request.args.get("source") or "").strip()
        has_filter = bool(target_date or query or review_filter or source_filter)
        draft_rows = store.drafts(status=status, limit=1000 if has_filter else 120)
        duplicate_titles = _duplicate_titles(store)
        if target_date:
            draft_rows = _filter_drafts_by_date(draft_rows, target_date)
        if query:
            draft_rows = _filter_drafts_by_query(draft_rows, query)
        if source_filter:
            draft_rows = [draft for draft in draft_rows if _row_value(draft, "source_id") == source_filter]
        if review_filter:
            draft_rows = _filter_drafts_by_review(draft_rows, review_filter, duplicate_titles)
        if source_filter:
            draft_rows = _sort_drafts_latest_first(draft_rows)
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
            page_title=_drafts_page_title(status, target_date, review_filter, source_filter, config_path),
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
        status = request.form.get("action") or request.form.get("status", "needs_review")
        if status not in VALID_STATUSES:
            status = "needs_review"
        store.update_draft(
            draft_id=draft_id,
            title=request.form.get("title", "").strip(),
            body=request.form.get("body", "").strip(),
            review_note=request.form.get("review_note", "").strip(),
            status=status,
        )
        logger.info("draft updated draft_id=%s status=%s", draft_id, status)
        flash("초안을 저장했습니다.")
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
        limit = _positive_int(request.form.get("limit"), default=10)
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
                }
            )
        status = auto_collector.snapshot()
        return jsonify(
            {
                "enabled": status.enabled,
                "running": status.running,
                "active_label": status.active_label or "",
                "progress_current": status.progress_current,
                "progress_total": status.progress_total,
                "progress_message": status.progress_message,
                "progress_source_name": status.progress_source_name or "",
                "progress_phase": status.progress_phase,
                "last_error": status.last_error,
                "last_finished_at": status.last_finished_at,
                "last_auto_finished_at": status.last_auto_finished_at,
                "run_count": status.run_count,
            }
        )

    @app.post("/draft")
    def draft():
        limit = _positive_int(request.form.get("limit"), default=5)
        for message in draft_pending_releases(store, limit):
            flash(message)
        return redirect(url_for("dashboard"))

    @app.post("/export")
    def export():
        markdown_path, csv_path, count = export_approved(store, Path(export_dir))
        logger.info("approved drafts exported count=%s markdown=%s csv=%s", count, markdown_path, csv_path)
        flash(f"승인 기사 {count}건을 내보냈습니다.")
        flash(f"마크다운 파일: {markdown_path}")
        flash(f"표 파일: {csv_path}")
        return redirect(url_for("dashboard"))

    return app


def _positive_int(value: str | None, default: int) -> int:
    try:
        parsed = int(value or default)
    except ValueError:
        return default
    return max(1, min(parsed, 100))


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def status_badge_class(status: str | None) -> str:
    if status == "approved":
        return "badge-approved"
    if status == "rejected":
        return "badge-warning"
    return "badge-neutral"


def model_label(model: str | None) -> str:
    if not model:
        return "모델 미상"
    if ":gemini" in model:
        return "Gemini"
    if ":rule-based" in model:
        return "규칙 기반"
    if "gpt" in model.lower():
        return "OpenAI"
    return model.split(":", 1)[0]


def model_badge_class(model: str | None) -> str:
    if model and ":gemini" in model:
        return "badge-gemini"
    if model and ":rule-based" in model:
        return "badge-warning"
    return "badge-neutral"


def interval_label(seconds: int | None) -> str:
    if not seconds:
        return "주기 미상"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return "1시간마다" if hours == 1 else f"{hours}시간마다"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return "1분마다" if minutes == 1 else f"{minutes}분마다"
    return f"{seconds}초마다"


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

    if _is_media_like(original_title, original_content):
        flags.append("사진·카드뉴스")
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
) -> list[dict[str, object]]:
    today = today or datetime.now(LOCAL_TZ).date()
    dates = [today - timedelta(days=offset) for offset in range(days)]
    buckets = {target_date: [] for target_date in dates}

    for draft in drafts:
        draft_date = _draft_date(draft)
        if draft_date in buckets:
            buckets[draft_date].append(draft)

    return [
        {
            "date": target_date,
            "iso_date": target_date.isoformat(),
            "label": _date_group_label(target_date, today),
            "drafts": buckets[target_date],
        }
        for target_date in dates
    ]


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


def _filter_drafts_by_review(drafts, review_filter: str, duplicate_titles: set[str]):
    today = datetime.now(LOCAL_TZ).date()
    if review_filter == "today":
        return [draft for draft in drafts if _draft_date(draft) == today]
    if review_filter == "attention":
        return [draft for draft in drafts if review_flags(draft, duplicate_titles)]
    if review_filter == "date_issue":
        return [draft for draft in drafts if _date_warning(draft)]
    if review_filter == "media":
        return [
            draft
            for draft in drafts
            if _is_media_like(str(_row_value(draft, "original_title") or ""), str(_row_value(draft, "original_content") or ""))
        ]
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
        model_counts[model_name] = model_counts.get(model_name, 0) + 1
        event_date = _parse_datetime(row["updated_at"] if is_refine else row["created_at"])
        if is_refine:
            total_refines += 1
            if event_date and event_date.date() == today:
                today_refines += 1
        else:
            total_drafts += 1
            if event_date and event_date.date() == today:
                today_drafts += 1

    top_models = sorted(model_counts.items(), key=lambda item: item[1], reverse=True)[:3]
    return {
        "today_total": today_drafts + today_refines,
        "today_drafts": today_drafts,
        "today_refines": today_refines,
        "total": total_drafts + total_refines,
        "total_drafts": total_drafts,
        "total_refines": total_refines,
        "top_models": top_models,
        "last_error": getattr(auto_status, "last_error", None) if auto_status else None,
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
    with store.connect() as conn:
        stats = {
            row["source_id"]: row
            for row in conn.execute(
                """
                SELECT source_id, COUNT(*) AS releases, MAX(collected_at) AS last_collected
                FROM press_releases
                GROUP BY source_id
                """
            ).fetchall()
        }
        latest = {
            row["source_id"]: row
            for row in conn.execute(
                """
                SELECT pr.source_id, pr.title, pr.published_at
                FROM press_releases pr
                JOIN (
                    SELECT source_id, MAX(id) AS max_id
                    FROM press_releases
                    GROUP BY source_id
                ) latest ON latest.max_id = pr.id
                """
            ).fetchall()
        }

    summaries = []
    for source in sources:
        stat = stats.get(source.id)
        latest_row = latest.get(source.id)
        releases = int(stat["releases"]) if stat else 0
        issue = ""
        if releases == 0:
            issue = "수집 없음"
        elif releases < 3:
            issue = "수집량 적음"
        elif latest_row and _parse_date(latest_row["published_at"]) is None:
            issue = "게시일 확인"
        summaries.append(
            {
                "id": source.id,
                "name": source.name,
                "region": source.region,
                "releases": releases,
                "last_collected": stat["last_collected"] if stat else None,
                "latest_title": latest_row["title"] if latest_row else "",
                "latest_published_at": latest_row["published_at"] if latest_row else None,
                "issue": issue,
            }
        )
    return summaries


def _draft_date(draft) -> date | None:
    return _parse_date(_row_value(draft, "published_at")) or _parse_date(_row_value(draft, "created_at"))


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
            "media": "사진·카드뉴스 기사",
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


def _is_media_like(title: str, content: str) -> bool:
    text = f"{title} {content}"
    return any(token in text for token in ("사진뉴스", "카드뉴스", "카드 뉴스", "포토뉴스", "〈사진뉴스〉", "[카드뉴스]"))


def _date_warning(draft) -> str:
    raw = str(_row_value(draft, "published_at") or "").strip()
    if not raw:
        return "게시일 없음"
    if _parse_date(raw) is None:
        return "게시일 파싱 실패"
    if not re.match(r"^\s*20\d{2}[./-]\d{1,2}[./-]\d{1,2}", raw):
        return "게시일 앞 문구 확인"
    return ""
