from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from json import JSONDecodeError
import os
from pathlib import Path
import re
import time

import httpx
from bs4 import BeautifulSoup

from .collectors import DEFAULT_HEADERS, CollectionError, _clean_text, _normalize_published_at, collect_source
from .models import PressRelease, Source
from .ops_logging import get_logger
from .settings import load_sources
from .storage import Store
from .writer import GeminiDraftError, generate_draft


ProgressCallback = Callable[[dict[str, object]], None]
logger = get_logger("service")
GEMINI_COOLDOWN_UNTIL_KEY = "gemini_cooldown_until"
GEMINI_COOLDOWN_REASON_KEY = "gemini_cooldown_reason"
DEFAULT_GEMINI_COOLDOWN_SECONDS = 30 * 60
TRANSIENT_DNS_RETRY_DELAY_SECONDS = 5.0
RETENTION_DAYS_ENV = "NEWS_SUMMARY_RETENTION_DAYS"
RETENTION_HOLIDAYS_ENV = "NEWS_SUMMARY_RETENTION_HOLIDAYS"
DEFAULT_RETENTION_DAYS = 3
LOCAL_TZ = timezone(timedelta(hours=9))


DATE_RE = re.compile(r"(20\d{2})[./-](\d{1,2})[./-](\d{1,2})")


BUILT_IN_KOREA_PUBLIC_HOLIDAYS = {
    # 2026년 운영 기준. 추가/수정은 NEWS_SUMMARY_RETENTION_HOLIDAYS=YYYY-MM-DD,... 로 보강할 수 있습니다.
    date(2026, 1, 1),
    date(2026, 2, 16),
    date(2026, 2, 17),
    date(2026, 2, 18),
    date(2026, 3, 1),
    date(2026, 3, 2),
    date(2026, 5, 5),
    date(2026, 5, 24),
    date(2026, 5, 25),
    date(2026, 6, 3),
    date(2026, 6, 6),
    date(2026, 8, 15),
    date(2026, 8, 17),
    date(2026, 10, 3),
    date(2026, 10, 5),
    date(2026, 10, 6),
    date(2026, 10, 7),
    date(2026, 10, 9),
    date(2026, 12, 25),
}


DEFAULT_COLLECT_LIMIT = 30


def collect_enabled_sources(
    store: Store,
    config_path: Path,
    limit: int = DEFAULT_COLLECT_LIMIT,
    progress_callback: ProgressCallback | None = None,
) -> list[str]:
    messages: list[str] = []
    sources = [source for source in load_sources(config_path) if source.enabled]
    retention_cutoff = collection_retention_cutoff_date()
    if not sources:
        _report_progress(progress_callback, phase="done", current=0, total=0, message="수집 완료")
        logger.warning("collect skipped no enabled sources config=%s", config_path)
        return ["켜진 수집 소스가 없습니다. config/municipalities.yaml을 확인하세요."]

    inserted = 0
    total = len(sources)
    transient_dns_retry_sources: list[Source] = []
    logger.info("collect started sources=%s limit=%s config=%s", total, limit, config_path)
    _report_progress(progress_callback, phase="collecting", current=0, total=total, message="수집 준비 중")
    for index, source in enumerate(sources, start=1):
        _report_progress(
            progress_callback,
            phase="collecting",
            current=index,
            total=total,
            source_name=source.name,
            message=f"{index}/{total} {source.name} 연결 확인 중",
        )
        try:
            releases = collect_source(source, limit=limit)
        except CollectionError as exc:
            failure_stage, failure_reason = classify_collection_failure(exc)
            message = f"{source.name} 수집 실패: {exc}"
            messages.append(message)
            store.record_source_collection_status(
                source.id,
                source.name,
                "failed",
                message,
                failure_stage=failure_stage,
                failure_reason=failure_reason,
            )
            if _should_retry_transient_dns_failure(failure_stage):
                transient_dns_retry_sources.append(source)
            logger.warning("source collection failed source_id=%s source_name=%s error=%s", source.id, source.name, exc)
            _report_progress(
                progress_callback,
                phase="source_failed",
                current=index,
                total=total,
                source_name=source.name,
                message=f"{index}/{total} {source.name} 수집 실패",
            )
            continue
        except Exception as exc:
            failure_stage, failure_reason = classify_collection_failure(exc)
            message = f"{source.name} 수집 실패: {type(exc).__name__}: {exc}"
            messages.append(message)
            store.record_source_collection_status(
                source.id,
                source.name,
                "failed",
                message,
                failure_stage=failure_stage,
                failure_reason=failure_reason,
            )
            if _should_retry_transient_dns_failure(failure_stage):
                transient_dns_retry_sources.append(source)
            logger.exception("source collection unexpected failure source_id=%s source_name=%s", source.id, source.name)
            _report_progress(
                progress_callback,
                phase="source_failed",
                current=index,
                total=total,
                source_name=source.name,
                message=f"{index}/{total} {source.name} 수집 실패",
            )
            continue

        retained_releases, source_skipped = filter_releases_by_retention(releases, retention_cutoff)
        source_inserted = 0
        for release in retained_releases:
            if store.add_press_release(release):
                inserted += 1
                source_inserted += 1
        repaired_dates = repair_missing_published_dates(store, source, limit=max(5, limit))
        source_message = _collection_source_message(
            source.name,
            len(releases),
            source_inserted,
            source_skipped,
        )
        messages.append(source_message)
        if repaired_dates:
            messages.append(f"{source.name}: 누락 게시일 {repaired_dates}건 보정")
        store.record_source_collection_status(
            source.id,
            source.name,
            "ok",
            source_message.removeprefix(f"{source.name}: "),
            releases_found=len(releases),
            inserted_count=source_inserted,
            repaired_dates=repaired_dates,
        )
        logger.info(
            "source collection succeeded source_id=%s source_name=%s releases=%s inserted=%s skipped_retention=%s repaired_dates=%s",
            source.id,
            source.name,
            len(releases),
            source_inserted,
            source_skipped,
            repaired_dates,
        )
        _report_progress(
            progress_callback,
            phase="source_done",
            current=index,
            total=total,
            source_name=source.name,
            message=f"{index}/{total} {source.name} 수집 완료",
        )

    if transient_dns_retry_sources:
        retry_inserted, retry_messages = retry_transient_dns_failures(
            store,
            transient_dns_retry_sources,
            limit=limit,
            progress_callback=progress_callback,
        )
        inserted += retry_inserted
        messages.extend(retry_messages)

    pruned = prune_press_releases_outside_retention(store, retention_cutoff)
    if pruned["press_releases"]:
        messages.append(
            "보관 기준 이전 원문 "
            f"{pruned['press_releases']}건, 초안 {pruned['drafts']}건을 정리했습니다."
        )
    messages.append(f"새 원문 {inserted}건을 저장했습니다.")
    logger.info("collect finished sources=%s inserted=%s pruned=%s cutoff=%s", total, inserted, pruned, retention_cutoff)
    _report_progress(progress_callback, phase="collected", current=total, total=total, message="수집 완료")
    return messages


def retry_transient_dns_failures(
    store: Store,
    sources: list[Source],
    limit: int = 10,
    progress_callback: ProgressCallback | None = None,
) -> tuple[int, list[str]]:
    if not sources:
        return 0, []

    logger.info("transient dns retry scheduled sources=%s delay=%s", len(sources), TRANSIENT_DNS_RETRY_DELAY_SECONDS)
    _report_progress(progress_callback, phase="dns_retry_waiting", message="DNS 실패 기관 자동 재검증 대기 중")
    if TRANSIENT_DNS_RETRY_DELAY_SECONDS > 0:
        time.sleep(TRANSIENT_DNS_RETRY_DELAY_SECONDS)

    inserted_total = 0
    messages: list[str] = []
    total = len(sources)
    for index, source in enumerate(sources, start=1):
        _report_progress(
            progress_callback,
            phase="dns_retrying",
            current=index,
            total=total,
            source_name=source.name,
            message=f"DNS 재검증 중: {source.name}",
        )
        try:
            releases = collect_source(source, limit=limit)
        except CollectionError as exc:
            failure_stage, failure_reason = classify_collection_failure(exc)
            message = f"{source.name} DNS 자동 재검증 실패: {exc}"
            store.record_source_collection_status(
                source.id,
                source.name,
                "failed",
                message,
                failure_stage=failure_stage,
                failure_reason=failure_reason,
            )
            messages.append(message)
            logger.warning("transient dns retry failed source_id=%s source_name=%s error=%s", source.id, source.name, exc)
            continue
        except Exception as exc:
            failure_stage, failure_reason = classify_collection_failure(exc)
            message = f"{source.name} DNS 자동 재검증 실패: {type(exc).__name__}: {exc}"
            store.record_source_collection_status(
                source.id,
                source.name,
                "failed",
                message,
                failure_stage=failure_stage,
                failure_reason=failure_reason,
            )
            messages.append(message)
            logger.exception("transient dns retry unexpected failure source_id=%s source_name=%s", source.id, source.name)
            continue

        retention_cutoff = collection_retention_cutoff_date()
        retained_releases, source_skipped = filter_releases_by_retention(releases, retention_cutoff)
        source_inserted = 0
        for release in retained_releases:
            if store.add_press_release(release):
                source_inserted += 1
                inserted_total += 1
        repaired_dates = repair_missing_published_dates(store, source, limit=max(5, limit))
        message = _collection_source_message(
            source.name,
            len(releases),
            source_inserted,
            source_skipped,
            prefix="DNS 자동 재검증 통과",
        )
        messages.append(message)
        if repaired_dates:
            messages.append(f"{source.name}: 누락 게시일 {repaired_dates}건 보정")
        store.record_source_collection_status(
            source.id,
            source.name,
            "ok",
            message.removeprefix(f"{source.name}: "),
            releases_found=len(releases),
            inserted_count=source_inserted,
            repaired_dates=repaired_dates,
        )
        logger.info(
            "transient dns retry succeeded source_id=%s source_name=%s releases=%s inserted=%s skipped_retention=%s repaired_dates=%s",
            source.id,
            source.name,
            len(releases),
            source_inserted,
            source_skipped,
            repaired_dates,
        )

    return inserted_total, messages


def collection_retention_days() -> int:
    try:
        return max(1, int(os.getenv(RETENTION_DAYS_ENV, str(DEFAULT_RETENTION_DAYS))))
    except ValueError:
        return DEFAULT_RETENTION_DAYS


def collection_retention_cutoff_date(today: date | None = None) -> date:
    today = today or datetime.now(timezone(timedelta(hours=9))).date()
    holidays = retention_holidays({today.year - 1, today.year, today.year + 1})
    remaining = collection_retention_days()
    cursor = today
    while True:
        if is_collection_business_day(cursor, holidays):
            remaining -= 1
            if remaining <= 0:
                return cursor
        cursor -= timedelta(days=1)


def retention_holidays(years: set[int] | None = None) -> set[date]:
    years = years or set()
    holidays = {holiday for holiday in BUILT_IN_KOREA_PUBLIC_HOLIDAYS if not years or holiday.year in years}
    holidays.update(_env_holidays())
    return holidays


def is_collection_business_day(target: date, holidays: set[date] | None = None) -> bool:
    holidays = holidays or retention_holidays({target.year})
    return target.weekday() < 5 and target not in holidays


def filter_releases_by_retention(
    releases: list[PressRelease],
    cutoff_date: date,
) -> tuple[list[PressRelease], int]:
    retained: list[PressRelease] = []
    skipped = 0
    for release in releases:
        published_date = press_release_published_date(release)
        if published_date and published_date < cutoff_date:
            skipped += 1
            continue
        retained.append(release)
    return retained, skipped


def prune_press_releases_outside_retention(store: Store, cutoff_date: date) -> dict[str, int]:
    return store.delete_press_releases_before(cutoff_date.isoformat())


def press_release_published_date(release: PressRelease) -> date | None:
    raw_value = _normalize_published_at(release.published_at)
    if not raw_value:
        raw_value = release.published_at
    if not raw_value:
        return None
    match = DATE_RE.search(str(raw_value))
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _env_holidays() -> set[date]:
    values = os.getenv(RETENTION_HOLIDAYS_ENV, "")
    holidays: set[date] = set()
    for value in values.split(","):
        value = value.strip()
        if not value:
            continue
        try:
            holidays.add(date.fromisoformat(value))
        except ValueError:
            logger.warning("invalid retention holiday ignored value=%s", value)
    return holidays


def _collection_source_message(
    source_name: str,
    releases_found: int,
    inserted_count: int,
    skipped_count: int,
    *,
    prefix: str = "원문 검증 통과",
) -> str:
    skipped_part = f", 보관 기준 제외 {skipped_count}건" if skipped_count else ""
    return f"{source_name}: {prefix} {releases_found}건{skipped_part}, 새로 저장 {inserted_count}건"


def _should_retry_transient_dns_failure(failure_stage: str) -> bool:
    return failure_stage == "DNS 조회"


def repair_missing_published_dates(store: Store, source: Source, limit: int = 20) -> int:
    if source.type != "html_board":
        return 0

    rows = store.press_releases_missing_published_at(source.id, limit=limit)
    if not rows:
        return 0

    repaired = 0
    selectors = source.selectors or {}
    with httpx.Client(
        headers=DEFAULT_HEADERS,
        timeout=20,
        follow_redirects=True,
        verify=source.verify_ssl,
    ) as client:
        for row in rows:
            try:
                response = client.get(str(row["url"]))
                response.raise_for_status()
            except Exception as exc:  # noqa: BLE001 - one broken detail page should not stop collection.
                logger.warning(
                    "published date repair failed source_id=%s release_id=%s error=%s",
                    source.id,
                    row["id"],
                    exc,
                )
                continue

            published_at = _extract_detail_published_at(response.text, selectors)
            if published_at:
                store.update_press_release_published_at(int(row["id"]), published_at)
                repaired += 1
                logger.info(
                    "published date repaired source_id=%s release_id=%s published_at=%s",
                    source.id,
                    row["id"],
                    published_at,
                )
    return repaired


def _extract_detail_published_at(html: str, selectors: dict) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    selector = selectors.get("detail_published_at") or selectors.get("published_at")
    if selector:
        node = soup.select_one(str(selector))
        published_at = _normalize_published_at(_clean_text(node.get_text(" ")) if node else "")
        if published_at:
            return published_at
    return _normalize_published_at(_clean_text(soup.get_text(" ")))


def collect_and_draft_cycle(
    store: Store,
    config_path: Path,
    collect_limit: int = DEFAULT_COLLECT_LIMIT,
    draft_limit: int = 250,
    require_gemini: bool = False,
    progress_callback: ProgressCallback | None = None,
) -> list[str]:
    messages = collect_enabled_sources(store, config_path, collect_limit, progress_callback=progress_callback)
    _report_progress(progress_callback, phase="drafting", message="Gemini 기사화 중")
    logger.info("draft cycle started draft_limit=%s require_gemini=%s", draft_limit, require_gemini)
    messages.extend(draft_pending_releases(store, draft_limit, require_gemini=require_gemini))
    _report_progress(progress_callback, phase="done", message="수집 완료")
    return messages


def draft_pending_releases(store: Store, limit: int = 5, require_gemini: bool = False) -> list[str]:
    if require_gemini:
        cooldown_until = gemini_cooldown_until(store)
        if cooldown_until:
            logger.info("draft skipped gemini cooldown until=%s", cooldown_until.isoformat())
            return [gemini_cooldown_message(cooldown_until)]

    rows = store.pending_press_releases(limit)
    if not rows:
        logger.info("draft skipped no pending releases")
        return ["초안을 만들 새 원문이 없습니다."]

    return _draft_rows(store, rows, limit=limit, require_gemini=require_gemini)


def draft_pending_releases_for_date(
    store: Store,
    published_date: str,
    limit: int = 250,
    require_gemini: bool = False,
    *,
    oldest_first: bool = False,
    sleep_seconds: float = 0.0,
) -> list[str]:
    if require_gemini:
        cooldown_until = gemini_cooldown_until(store)
        if cooldown_until:
            logger.info("date draft skipped gemini cooldown until=%s date=%s", cooldown_until.isoformat(), published_date)
            return [gemini_cooldown_message(cooldown_until)]

    rows = store.pending_press_releases_for_date(published_date, limit, oldest_first=oldest_first)
    if not rows:
        logger.info("date draft skipped no pending releases date=%s", published_date)
        return [f"{published_date} 초안을 만들 새 원문이 없습니다."]

    return _draft_rows(
        store,
        rows,
        limit=limit,
        require_gemini=require_gemini,
        published_date=published_date,
        sleep_seconds=sleep_seconds,
    )


def _draft_rows(
    store: Store,
    rows,
    *,
    limit: int,
    require_gemini: bool,
    published_date: str | None = None,
    sleep_seconds: float = 0.0,
) -> list[str]:
    messages = []
    logger.info(
        "draft pending started rows=%s limit=%s require_gemini=%s published_date=%s sleep_seconds=%s",
        len(rows),
        limit,
        require_gemini,
        published_date,
        sleep_seconds,
    )
    for index, row in enumerate(rows, start=1):
        item = PressRelease(
            source_id=row["source_id"],
            source_name=row["source_name"],
            region=row["region"],
            title=row["title"],
            url=row["url"],
            content=row["content"],
            published_at=row["published_at"],
            collected_at=row["collected_at"],
        )
        try:
            draft = generate_draft(row["id"], item, require_gemini=require_gemini)
        except GeminiDraftError as exc:
            models = ", ".join(exc.attempted_models)
            suffix = f" 시도한 모델: {models}" if models else ""
            messages.append(f"{row['source_name']} 초안 보류: {exc}{suffix}")
            logger.warning(
                "draft held source_name=%s press_release_id=%s models=%s error=%s",
                row["source_name"],
                row["id"],
                models,
                exc,
            )
            if _is_gemini_quota_message(str(exc)):
                cooldown_until = mark_gemini_cooldown(store, reason=f"자동 초안 생성 한도 초과: {exc}")
                messages.append(gemini_cooldown_message(cooldown_until))
                logger.warning("gemini cooldown started until=%s", cooldown_until.isoformat())
                break
            continue
        draft_id = store.add_article_draft(draft)
        logger.info(
            "draft created draft_id=%s press_release_id=%s source_name=%s model=%s",
            draft_id,
            row["id"],
            row["source_name"],
            draft.model,
        )
        messages.append(f"초안 #{draft_id} 생성: {draft.title}")
        if sleep_seconds > 0 and index < len(rows):
            time.sleep(sleep_seconds)
    return messages


def _report_progress(progress_callback: ProgressCallback | None, **event: object) -> None:
    if progress_callback:
        progress_callback(event)


def classify_collection_failure(exc: Exception) -> tuple[str, str]:
    message = str(exc)
    lowered = message.lower()
    if "certificate_verify_failed" in lowered or "certificate verify failed" in lowered:
        return "SSL 인증서", "인증서 검증 실패"
    if "getaddrinfo failed" in lowered or "could not resolve" in lowered:
        return "DNS 조회", "도메인 주소를 찾지 못함"
    if isinstance(exc, httpx.ConnectTimeout) or "handshake operation timed out" in lowered:
        return "외부 사이트 응답 지연", "TLS 연결 시간 초과"
    if isinstance(exc, httpx.TimeoutException) or "timeout" in lowered or "timed out" in lowered or "타임아웃" in message:
        return "외부 사이트 응답 지연", "응답 지연 또는 타임아웃"
    if "10054" in message or "강제로 끊겼습니다" in message:
        return "연결 강제 종료", "원격 서버가 연결을 끊음"
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code if exc.response else ""
        return "HTTP 상태 오류", f"응답 코드 {status_code}".strip()
    if isinstance(exc, httpx.RequestError):
        return "사이트 접속", "요청 실패 또는 연결 오류"
    if isinstance(exc, JSONDecodeError) or "json" in lowered:
        return "자료 파싱", "JSON 응답 해석 실패"
    if isinstance(exc, CollectionError):
        if "설정" in message or "지원하지 않는" in message or "주소가 없습니다" in message:
            return "수집 설정", "수집 소스 설정 확인 필요"
        return "수집 처리", "수집 규칙 또는 사이트 구조 확인 필요"
    if isinstance(exc, (KeyError, TypeError, ValueError)):
        return "자료 파싱", "목록/본문 구조 해석 실패"
    return "기타 오류", type(exc).__name__


def gemini_cooldown_until(store: Store) -> datetime | None:
    raw_value = store.get_app_metadata(GEMINI_COOLDOWN_UNTIL_KEY)
    if not raw_value:
        return None
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return parsed if parsed > datetime.now(timezone.utc) else None


def mark_gemini_cooldown(
    store: Store,
    reason: str = "Gemini 요청 한도 감지",
    seconds: int = DEFAULT_GEMINI_COOLDOWN_SECONDS,
) -> datetime:
    cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    store.set_app_metadata(GEMINI_COOLDOWN_UNTIL_KEY, cooldown_until.isoformat())
    store.set_app_metadata(GEMINI_COOLDOWN_REASON_KEY, reason)
    return cooldown_until


def gemini_cooldown_message(cooldown_until: datetime) -> str:
    return f"Gemini 요청 한도 감지로 한국 시간 {_format_local_datetime(cooldown_until)}까지 초안 생성을 보류합니다."


def _format_local_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=LOCAL_TZ)
    return value.astimezone(LOCAL_TZ).strftime("%Y.%m.%d %H:%M")


def _is_gemini_quota_message(message: str) -> bool:
    lowered = message.lower()
    return "429" in message or "resource_exhausted" in lowered or "quota" in lowered or "요청 한도" in message
