from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


def collect_enabled_sources(
    store: Store,
    config_path: Path,
    limit: int = 10,
    progress_callback: ProgressCallback | None = None,
) -> list[str]:
    messages: list[str] = []
    sources = [source for source in load_sources(config_path) if source.enabled]
    if not sources:
        _report_progress(progress_callback, phase="done", current=0, total=0, message="수집 완료")
        logger.warning("collect skipped no enabled sources config=%s", config_path)
        return ["켜진 수집 소스가 없습니다. config/municipalities.yaml을 확인하세요."]

    inserted = 0
    total = len(sources)
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
            message = f"{source.name} 수집 실패: {exc}"
            messages.append(message)
            store.record_source_collection_status(source.id, source.name, "failed", message)
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
            message = f"{source.name} 수집 실패: {type(exc).__name__}: {exc}"
            messages.append(message)
            store.record_source_collection_status(source.id, source.name, "failed", message)
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

        source_inserted = 0
        for release in releases:
            if store.add_press_release(release):
                inserted += 1
                source_inserted += 1
        repaired_dates = repair_missing_published_dates(store, source, limit=max(5, limit))
        messages.append(f"{source.name}: 원문 검증 통과 {len(releases)}건, 새로 저장 {source_inserted}건")
        if repaired_dates:
            messages.append(f"{source.name}: 누락 게시일 {repaired_dates}건 보정")
        store.record_source_collection_status(
            source.id,
            source.name,
            "ok",
            f"원문 검증 통과 {len(releases)}건, 새로 저장 {source_inserted}건",
            releases_found=len(releases),
            inserted_count=source_inserted,
            repaired_dates=repaired_dates,
        )
        logger.info(
            "source collection succeeded source_id=%s source_name=%s releases=%s inserted=%s repaired_dates=%s",
            source.id,
            source.name,
            len(releases),
            source_inserted,
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

    messages.append(f"새 원문 {inserted}건을 저장했습니다.")
    logger.info("collect finished sources=%s inserted=%s", total, inserted)
    _report_progress(progress_callback, phase="collected", current=total, total=total, message="수집 완료")
    return messages


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
    collect_limit: int = 10,
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
            return [_gemini_cooldown_message(cooldown_until)]

    rows = store.pending_press_releases(limit)
    if not rows:
        logger.info("draft skipped no pending releases")
        return ["초안을 만들 새 원문이 없습니다."]

    messages = []
    logger.info("draft pending started rows=%s limit=%s require_gemini=%s", len(rows), limit, require_gemini)
    for row in rows:
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
                messages.append(_gemini_cooldown_message(cooldown_until))
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
    return messages


def _report_progress(progress_callback: ProgressCallback | None, **event: object) -> None:
    if progress_callback:
        progress_callback(event)


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


def _gemini_cooldown_message(cooldown_until: datetime) -> str:
    return f"Gemini 요청 한도 감지로 {cooldown_until.astimezone(timezone.utc).strftime('%H:%M')} UTC까지 초안 생성을 보류합니다."


def _is_gemini_quota_message(message: str) -> bool:
    lowered = message.lower()
    return "429" in message or "resource_exhausted" in lowered or "quota" in lowered or "요청 한도" in message
