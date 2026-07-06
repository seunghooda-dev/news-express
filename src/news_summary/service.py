from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
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

try:
    import holidays as holidays_lib
except ModuleNotFoundError:  # pragma: no cover - Render installs this, local editable env may not be refreshed yet.
    holidays_lib = None


ProgressCallback = Callable[[dict[str, object]], None]
logger = get_logger("service")
GEMINI_COOLDOWN_UNTIL_KEY = "gemini_cooldown_until"
GEMINI_COOLDOWN_REASON_KEY = "gemini_cooldown_reason"
DEFAULT_GEMINI_COOLDOWN_SECONDS = 30 * 60
GEMINI_DRAFT_FAILURE_RETRY_SECONDS_ENV = "NEWS_SUMMARY_GEMINI_FAILURE_RETRY_SECONDS"
DEFAULT_GEMINI_DRAFT_FAILURE_RETRY_SECONDS = 15 * 60
TRANSIENT_COLLECTION_RETRY_DELAY_SECONDS = 5.0
DEFAULT_TRANSIENT_COLLECTION_RETRY_DELAYS = (5.0, 30.0)
TRANSIENT_COLLECTION_RETRY_DELAYS_ENV = "NEWS_SUMMARY_TRANSIENT_RETRY_DELAYS"
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
    transient_retry_sources: list[Source] = []
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
            releases = collect_source_with_fallback(source, limit=limit)
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
            if _should_retry_transient_collection_failure(failure_stage, failure_reason):
                transient_retry_sources.append(source)
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
            if _should_retry_transient_collection_failure(failure_stage, failure_reason):
                transient_retry_sources.append(source)
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

    if transient_retry_sources:
        retry_inserted, retry_messages = retry_transient_collection_failures(
            store,
            transient_retry_sources,
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


def retry_transient_collection_failures(
    store: Store,
    sources: list[Source],
    limit: int = 10,
    progress_callback: ProgressCallback | None = None,
) -> tuple[int, list[str]]:
    if not sources:
        return 0, []

    inserted_total = 0
    messages: list[str] = []
    remaining_sources = list(sources)
    retry_delays = transient_collection_retry_delays()
    logger.info(
        "transient collection retry scheduled sources=%s delays=%s",
        len(sources),
        retry_delays,
    )

    for attempt_index, delay_seconds in enumerate(retry_delays, start=1):
        if not remaining_sources:
            break
        _report_progress(
            progress_callback,
            phase="transient_retry_waiting",
            message=f"일시 연결 실패 기관 {attempt_index}차 자동 재검증 대기 중",
        )
        if delay_seconds > 0:
            time.sleep(delay_seconds)

        total = len(remaining_sources)
        next_remaining: list[Source] = []
        for index, source in enumerate(remaining_sources, start=1):
            source_inserted, retry_message, should_retry_again = _retry_transient_source_once(
                store,
                source,
                limit=limit,
                progress_callback=progress_callback,
                current=index,
                total=total,
                attempt_index=attempt_index,
                is_final_attempt=attempt_index >= len(retry_delays),
            )
            inserted_total += source_inserted
            if retry_message:
                messages.append(retry_message)
            if should_retry_again:
                next_remaining.append(source)
        remaining_sources = next_remaining

    return inserted_total, messages


def _retry_transient_source_once(
    store: Store,
    source: Source,
    *,
    limit: int,
    progress_callback: ProgressCallback | None,
    current: int,
    total: int,
    attempt_index: int,
    is_final_attempt: bool,
) -> tuple[int, str, bool]:
    _report_progress(
        progress_callback,
        phase="transient_retrying",
        current=current,
        total=total,
        source_name=source.name,
        message=f"일시 연결 실패 {attempt_index}차 재검증 중: {source.name}",
    )
    try:
        releases = collect_source_with_fallback(source, limit=limit)
    except CollectionError as exc:
        failure_stage, failure_reason = classify_collection_failure(exc)
        message = f"{source.name} 일시 장애 {attempt_index}차 자동 재검증 실패: {exc}"
        store.record_source_collection_status(
            source.id,
            source.name,
            "failed",
            message,
            failure_stage=failure_stage,
            failure_reason=failure_reason,
        )
        logger.warning("transient collection retry failed source_id=%s source_name=%s error=%s", source.id, source.name, exc)
        return 0, message, (not is_final_attempt and _should_retry_transient_collection_failure(failure_stage, failure_reason))
    except Exception as exc:
        failure_stage, failure_reason = classify_collection_failure(exc)
        message = f"{source.name} 일시 장애 {attempt_index}차 자동 재검증 실패: {type(exc).__name__}: {exc}"
        store.record_source_collection_status(
            source.id,
            source.name,
            "failed",
            message,
            failure_stage=failure_stage,
            failure_reason=failure_reason,
        )
        logger.exception("transient collection retry unexpected failure source_id=%s source_name=%s", source.id, source.name)
        return 0, message, (not is_final_attempt and _should_retry_transient_collection_failure(failure_stage, failure_reason))

    retention_cutoff = collection_retention_cutoff_date()
    retained_releases, source_skipped = filter_releases_by_retention(releases, retention_cutoff)
    source_inserted = 0
    for release in retained_releases:
        if store.add_press_release(release):
            source_inserted += 1
    repaired_dates = repair_missing_published_dates(store, source, limit=max(5, limit))
    message = _collection_source_message(
        source.name,
        len(releases),
        source_inserted,
        source_skipped,
        prefix=f"일시 장애 {attempt_index}차 자동 재검증 통과",
    )
    if repaired_dates:
        message = f"{message} · 누락 게시일 {repaired_dates}건 보정"
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
        "transient collection retry succeeded source_id=%s source_name=%s attempt=%s releases=%s inserted=%s skipped_retention=%s repaired_dates=%s",
        source.id,
        source.name,
        attempt_index,
        len(releases),
        source_inserted,
        source_skipped,
        repaired_dates,
    )
    return source_inserted, message, False


def transient_collection_retry_delays() -> tuple[float, ...]:
    raw_value = os.getenv(TRANSIENT_COLLECTION_RETRY_DELAYS_ENV)
    if raw_value is not None:
        delays = []
        for chunk in raw_value.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                delays.append(max(0.0, float(chunk)))
            except ValueError:
                logger.warning("invalid transient retry delay ignored value=%s", chunk)
        return tuple(delays) or (0.0,)
    if TRANSIENT_COLLECTION_RETRY_DELAY_SECONDS != DEFAULT_TRANSIENT_COLLECTION_RETRY_DELAYS[0]:
        return (max(0.0, float(TRANSIENT_COLLECTION_RETRY_DELAY_SECONDS)),)
    return DEFAULT_TRANSIENT_COLLECTION_RETRY_DELAYS


def collect_source_with_fallback(source: Source, limit: int = DEFAULT_COLLECT_LIMIT) -> list[PressRelease]:
    candidate_sources = _source_collection_candidates(source)
    last_error: Exception | None = None
    for index, candidate in enumerate(candidate_sources):
        try:
            releases = collect_source(candidate, limit=limit)
        except Exception as exc:  # noqa: BLE001 - fallback should preserve the final useful failure.
            last_error = exc
            failure_stage, failure_reason = classify_collection_failure(exc)
            if index < len(candidate_sources) - 1 and _should_try_fallback_url(failure_stage, failure_reason):
                logger.warning(
                    "source fallback url retry source_id=%s source_name=%s failed_url=%s stage=%s reason=%s next_url=%s",
                    source.id,
                    source.name,
                    candidate.list_url or candidate.feed_url,
                    failure_stage,
                    failure_reason,
                    candidate_sources[index + 1].list_url or candidate_sources[index + 1].feed_url,
                )
                continue
            raise
        if index > 0:
            logger.info(
                "source fallback url succeeded source_id=%s source_name=%s url=%s releases=%s",
                source.id,
                source.name,
                candidate.list_url or candidate.feed_url,
                len(releases),
            )
        return releases
    assert last_error is not None
    raise last_error


def _source_collection_candidates(source: Source) -> list[Source]:
    urls = []
    if source.list_url:
        urls.append(source.list_url)
    urls.extend(source.fallback_urls)
    if not urls:
        return [source]

    candidates: list[Source] = []
    seen = set()
    for url in urls:
        normalized = url.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        if normalized == source.list_url:
            candidates.append(source)
        else:
            candidates.append(replace(source, list_url=normalized, base_url=normalized))
    return candidates or [source]


def _should_try_fallback_url(failure_stage: str, failure_reason: str = "") -> bool:
    return failure_stage in {
        "DNS 조회",
        "외부 사이트 응답 지연",
        "연결 강제 종료",
        "사이트 접속",
        "사이트 구조 변경",
        "HTTP 상태 오류",
    }


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
    holidays.update(_library_korea_holidays(years))
    holidays.update(_env_holidays())
    return holidays


def is_collection_business_day(target: date, holidays: set[date] | None = None) -> bool:
    holidays = holidays or retention_holidays({target.year})
    return target.weekday() < 5 and target not in holidays


def business_days_between(start_date: date, end_date: date, holidays: set[date] | None = None) -> int:
    if end_date <= start_date:
        return 0
    holidays = holidays or retention_holidays({start_date.year, end_date.year})
    count = 0
    cursor = start_date + timedelta(days=1)
    while cursor <= end_date:
        if is_collection_business_day(cursor, holidays):
            count += 1
        cursor += timedelta(days=1)
    return count


def has_collection_non_business_day_between(start_date: date, end_date: date, holidays: set[date] | None = None) -> bool:
    if end_date <= start_date:
        return False
    holidays = holidays or retention_holidays({start_date.year, end_date.year})
    cursor = start_date + timedelta(days=1)
    while cursor <= end_date:
        if not is_collection_business_day(cursor, holidays):
            return True
        cursor += timedelta(days=1)
    return False


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


def _library_korea_holidays(years: set[int]) -> set[date]:
    if holidays_lib is None or not years:
        return set()
    try:
        return {item for item in holidays_lib.country_holidays("KR", years=sorted(years))}
    except Exception as exc:  # noqa: BLE001 - holiday fallback should keep collection running.
        logger.warning("korea holiday library failed years=%s error=%s", sorted(years), exc)
        return set()


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


def _should_retry_transient_collection_failure(failure_stage: str, failure_reason: str = "") -> bool:
    if failure_stage in {"DNS 조회", "외부 사이트 응답 지연", "연결 강제 종료", "사이트 접속"}:
        return True
    if failure_stage == "HTTP 상태 오류":
        return any(code in failure_reason for code in ("429", "500", "502", "503", "504"))
    return False


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

    rows = store.pending_press_releases_ready_for_retry(limit)
    if not rows:
        pending_total = int(store.pending_press_release_summary(limit=1).get("total") or 0)
        failure_total = int(store.draft_generation_failure_summary(limit=1).get("total") or 0)
        if pending_total and failure_total:
            logger.info("draft skipped pending releases waiting for retry pending=%s failures=%s", pending_total, failure_total)
            return [f"Gemini 실패 큐 재시도 대기 중입니다. 대기 원문 {pending_total}건, 실패 큐 {failure_total}건"]
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
            is_quota_error = _is_gemini_quota_message(str(exc))
            next_retry_at = (
                datetime.now(timezone.utc) + timedelta(seconds=gemini_draft_failure_retry_seconds())
            ).isoformat()
            logger.warning(
                "draft held source_name=%s press_release_id=%s models=%s error=%s",
                row["source_name"],
                row["id"],
                models,
                exc,
            )
            if is_quota_error:
                cooldown_until = mark_gemini_cooldown(store, reason=f"자동 초안 생성 한도 초과: {exc}")
                next_retry_at = cooldown_until.isoformat()
                messages.append(gemini_cooldown_message(cooldown_until))
                logger.warning("gemini cooldown started until=%s", cooldown_until.isoformat())
            store.record_draft_generation_failure(
                int(row["id"]),
                "quota" if is_quota_error else "generation_error",
                str(exc),
                models,
                next_retry_at,
            )
            if is_quota_error:
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


def gemini_draft_failure_retry_seconds() -> int:
    try:
        return max(60, int(os.getenv(GEMINI_DRAFT_FAILURE_RETRY_SECONDS_ENV, str(DEFAULT_GEMINI_DRAFT_FAILURE_RETRY_SECONDS))))
    except ValueError:
        return DEFAULT_GEMINI_DRAFT_FAILURE_RETRY_SECONDS


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
        if "구조" in message or "후보를 찾지 못" in message or "수집 결과 0건" in message:
            return "사이트 구조 변경", "목록/본문 선택자 확인 필요"
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
