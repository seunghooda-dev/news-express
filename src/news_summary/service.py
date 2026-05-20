from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .collectors import CollectionError, collect_source
from .models import PressRelease
from .ops_logging import get_logger
from .settings import load_sources
from .storage import Store
from .writer import GeminiDraftError, generate_draft


ProgressCallback = Callable[[dict[str, object]], None]
logger = get_logger("service")


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
            messages.append(f"{source.name} 수집 실패: {exc}")
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
            messages.append(f"{source.name} 수집 실패: {type(exc).__name__}: {exc}")
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
        messages.append(f"{source.name}: 원문 검증 통과 {len(releases)}건, 새로 저장 {source_inserted}건")
        logger.info(
            "source collection succeeded source_id=%s source_name=%s releases=%s inserted=%s",
            source.id,
            source.name,
            len(releases),
            source_inserted,
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
