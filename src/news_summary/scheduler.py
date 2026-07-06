from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .ops_logging import get_logger
from .service import (
    business_days_between,
    classify_collection_failure,
    collect_and_draft_cycle,
    collect_source_with_fallback,
    collection_retention_cutoff_date,
    draft_pending_releases,
    filter_releases_by_retention,
    is_collection_business_day,
    repair_missing_published_dates,
    retention_holidays,
)
from .settings import load_sources
from .storage import Store


DEFAULT_AUTO_INTERVAL_SECONDS = 3600
DEFAULT_AUTO_COLLECT_LIMIT = 30
AUTO_COLLECT_ENABLED_KEY = "auto_collect_enabled"
LAST_AUTO_COLLECT_FINISHED_AT_KEY = "last_auto_collect_finished_at"
AUTO_COLLECT_STATUS_KEY = "auto_collect_status_snapshot"
STARTUP_CATCHUP_ENV = "NEWS_SUMMARY_STARTUP_CATCHUP"
AUTO_QUEUE_DRAIN_ENV = "NEWS_SUMMARY_AUTO_QUEUE_DRAIN"
AUTO_QUEUE_DRAIN_INTERVAL_ENV = "NEWS_SUMMARY_AUTO_QUEUE_DRAIN_INTERVAL_SECONDS"
AUTO_QUEUE_DRAIN_LIMIT_ENV = "NEWS_SUMMARY_AUTO_QUEUE_DRAIN_LIMIT"
AUTO_RECOVERY_INTERVAL_ENV = "NEWS_SUMMARY_AUTO_RECOVERY_INTERVAL_SECONDS"
AUTO_RECOVERY_LIMIT_ENV = "NEWS_SUMMARY_AUTO_RECOVERY_LIMIT"
AUTO_RECOVERY_STATUS_KEY = "auto_recovery_status_snapshot"
AUTO_DAILY_REPORT_KEY = "auto_daily_report_snapshot"
LOCAL_TZ = timezone(timedelta(hours=9))
logger = get_logger("scheduler")


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", "아니오", "끄기"}


def env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        parsed = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(minimum, parsed)


@dataclass
class AutoCollectorStatus:
    enabled: bool = False
    running: bool = False
    interval_seconds: int = DEFAULT_AUTO_INTERVAL_SECONDS
    collect_limit: int = DEFAULT_AUTO_COLLECT_LIMIT
    draft_limit: int = 250
    require_gemini: bool = True
    run_count: int = 0
    last_started_at: str | None = None
    last_finished_at: str | None = None
    last_auto_finished_at: str | None = None
    next_run_at: str | None = None
    last_error: str | None = None
    last_messages: list[str] = field(default_factory=list)
    active_label: str | None = None
    progress_phase: str = "idle"
    progress_current: int = 0
    progress_total: int = 0
    progress_source_name: str | None = None
    progress_message: str = "대기 중"


class AutoCollector:
    def __init__(
        self,
        store: Store,
        config_path: Path,
        interval_seconds: int = DEFAULT_AUTO_INTERVAL_SECONDS,
        collect_limit: int = DEFAULT_AUTO_COLLECT_LIMIT,
        draft_limit: int = 250,
        require_gemini: bool = True,
        enabled: bool = True,
    ) -> None:
        self.store = store
        self.config_path = config_path
        self.interval_seconds = interval_seconds
        self.collect_limit = collect_limit
        self.draft_limit = draft_limit
        self.require_gemini = require_gemini
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._next_maintenance_at: datetime | None = None
        source_count = self._enabled_source_count()
        self._status = AutoCollectorStatus(
            enabled=enabled,
            interval_seconds=interval_seconds,
            collect_limit=collect_limit,
            draft_limit=draft_limit,
            require_gemini=require_gemini,
            progress_total=source_count,
            last_auto_finished_at=self.store.get_app_metadata(LAST_AUTO_COLLECT_FINISHED_AT_KEY),
            progress_message="다음 정각 자동 수집 대기 중" if enabled else "자동 수집 꺼짐",
        )

    def start(self) -> None:
        with self._state_lock:
            if not self._status.enabled:
                logger.info("auto collector start skipped disabled")
                return
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="news-summary-auto-collector", daemon=True)
        self._thread.start()
        logger.info("auto collector thread started interval=%s collect_limit=%s", self.interval_seconds, self.collect_limit)

    def stop(self) -> None:
        self._stop_event.set()
        with self._state_lock:
            self._status.next_run_at = None
            if not self._status.running:
                self._status.progress_message = "자동 수집 꺼짐"
        logger.info("auto collector stop requested")

    def set_enabled(self, enabled: bool) -> None:
        self.store.set_app_metadata(AUTO_COLLECT_ENABLED_KEY, "true" if enabled else "false")
        with self._state_lock:
            self._status.enabled = enabled
            if not enabled:
                self._status.next_run_at = None
                if not self._status.running:
                    self._status.progress_message = "자동 수집 꺼짐"
            elif not self._status.running:
                self._status.progress_message = "다음 정각 자동 수집 대기 중"
        if enabled:
            self.start()
        else:
            self.stop()

    def run_once(
        self,
        collect_limit: int | None = None,
        draft_limit: int | None = None,
        label: str = "자동 수집",
    ) -> list[str]:
        if not self._run_lock.acquire(blocking=False):
            logger.info("collector run skipped already running label=%s", label)
            return ["자동 수집이 이미 실행 중입니다."]

        try:
            return self._execute_once(collect_limit=collect_limit, draft_limit=draft_limit, label=label)
        finally:
            self._run_lock.release()

    def run_async_once(
        self,
        collect_limit: int | None = None,
        draft_limit: int | None = None,
        label: str = "수동 재수집",
    ) -> bool:
        if not self._run_lock.acquire(blocking=False):
            logger.info("async collector run skipped already running label=%s", label)
            return False

        thread = threading.Thread(
            target=self._execute_async,
            kwargs={"collect_limit": collect_limit, "draft_limit": draft_limit, "label": label},
            name="news-summary-manual-recrawl",
            daemon=True,
        )
        thread.start()
        return True

    def _execute_async(self, collect_limit: int | None, draft_limit: int | None, label: str) -> None:
        try:
            self._execute_once(collect_limit=collect_limit, draft_limit=draft_limit, label=label)
        finally:
            self._run_lock.release()

    def _execute_once(
        self,
        collect_limit: int | None = None,
        draft_limit: int | None = None,
        label: str = "자동 수집",
    ) -> list[str]:
        collect_limit = collect_limit or self.collect_limit
        draft_limit = draft_limit or self.draft_limit
        total_sources = self._enabled_source_count()
        started = _now()
        logger.info(
            "collector run started label=%s sources=%s collect_limit=%s draft_limit=%s",
            label,
            total_sources,
            collect_limit,
            draft_limit,
        )
        with self._state_lock:
            self._status.running = True
            self._status.active_label = label
            self._status.last_started_at = started
            self._status.last_error = None
            self._status.progress_phase = "preparing"
            self._status.progress_current = 0
            self._status.progress_total = total_sources
            self._status.progress_source_name = None
            self._status.progress_message = f"{label} 준비 중"
        self._persist_status_snapshot()

        try:
            messages = collect_and_draft_cycle(
                self.store,
                self.config_path,
                collect_limit=collect_limit,
                draft_limit=draft_limit,
                require_gemini=self.require_gemini,
                progress_callback=self._update_progress,
            )
        except Exception as exc:  # noqa: BLE001 - background worker must keep the server alive.
            messages = []
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("collector run failed label=%s", label)
            with self._state_lock:
                self._status.last_error = error
        else:
            error = None
        finally:
            finished = _now()
            if label == "자동 수집" and not error:
                self.store.set_app_metadata(LAST_AUTO_COLLECT_FINISHED_AT_KEY, finished)
            with self._state_lock:
                self._status.running = False
                self._status.run_count += 1
                self._status.last_finished_at = finished
                if label == "자동 수집" and not error:
                    self._status.last_auto_finished_at = finished
                self._status.last_messages = messages[-12:]
                self._status.progress_phase = "error" if error else "done"
                if self._status.progress_total:
                    self._status.progress_current = self._status.progress_total
                self._status.progress_message = "수집 실패" if error else "수집 완료"
                if error:
                    self._status.last_error = error
            self._persist_status_snapshot()
        logger.info("collector run finished label=%s error=%s messages=%s", label, bool(error), len(messages))

        return messages

    def snapshot(self) -> AutoCollectorStatus:
        with self._state_lock:
            return AutoCollectorStatus(
                enabled=self._status.enabled,
                running=self._status.running,
                interval_seconds=self._status.interval_seconds,
                collect_limit=self._status.collect_limit,
                draft_limit=self._status.draft_limit,
                require_gemini=self._status.require_gemini,
                run_count=self._status.run_count,
                last_started_at=self._status.last_started_at,
                last_finished_at=self._status.last_finished_at,
                last_auto_finished_at=self._status.last_auto_finished_at,
                next_run_at=self._status.next_run_at,
                last_error=self._status.last_error,
                last_messages=list(self._status.last_messages),
                active_label=self._status.active_label,
                progress_phase=self._status.progress_phase,
                progress_current=self._status.progress_current,
                progress_total=self._status.progress_total,
                progress_source_name=self._status.progress_source_name,
                progress_message=self._status.progress_message,
            )

    def _loop(self) -> None:
        if self._startup_catchup_needed():
            logger.info("auto collector startup catch-up run requested last_auto_finished_at=%s", self._status.last_auto_finished_at)
            self._set_next_run_at(datetime.now(timezone.utc), message="누락 자동 수집 보정 중")
            self.run_once()

        while not self._stop_event.is_set():
            if not self.snapshot().enabled:
                break
            next_run_at = _next_hourly_run_at()
            wait_seconds = _wait_seconds_until(next_run_at)
            self._set_next_run_at(next_run_at, message="다음 정각 자동 수집 대기 중")
            logger.info("auto collector waiting for hourly run wait_seconds=%s next_run_at=%s", wait_seconds, next_run_at.isoformat())
            while not self._stop_event.is_set():
                remaining = _wait_seconds_until(next_run_at)
                if remaining <= 0:
                    break
                if self._stop_event.wait(min(remaining, self._maintenance_poll_seconds())):
                    break
                self._run_maintenance_if_due()
            if self._stop_event.is_set():
                break

            self.run_once()
            self._run_maintenance_if_due(force=True)

    def _set_next_run_at(self, next_run_at: datetime, message: str | None = None) -> None:
        with self._state_lock:
            self._status.next_run_at = next_run_at.astimezone(timezone.utc).isoformat()
            if message and not self._status.running:
                self._status.progress_message = message
        self._persist_status_snapshot()

    def _update_progress(self, event: dict[str, object]) -> None:
        with self._state_lock:
            if "phase" in event:
                self._status.progress_phase = str(event["phase"])
            if "current" in event:
                self._status.progress_current = max(0, int(event["current"] or 0))
            if "total" in event:
                self._status.progress_total = max(0, int(event["total"] or 0))
            if "source_name" in event:
                self._status.progress_source_name = str(event["source_name"] or "")
            if "message" in event:
                self._status.progress_message = str(event["message"] or "")
        self._persist_status_snapshot()

    def _enabled_source_count(self) -> int:
        try:
            return len([source for source in load_sources(self.config_path) if source.enabled])
        except Exception:  # noqa: BLE001 - progress should still render even if config is temporarily invalid.
            logger.exception("enabled source count failed config=%s", self.config_path)
            return 0

    def _persist_status_snapshot(self) -> None:
        try:
            with self._state_lock:
                payload = {
                    "enabled": self._status.enabled,
                    "running": self._status.running,
                    "active_label": self._status.active_label or "",
                    "progress_current": self._status.progress_current,
                    "progress_total": self._status.progress_total,
                    "progress_message": self._status.progress_message,
                    "progress_source_name": self._status.progress_source_name or "",
                    "progress_phase": self._status.progress_phase,
                    "last_error": self._status.last_error,
                    "last_started_at": self._status.last_started_at,
                    "last_finished_at": self._status.last_finished_at,
                    "last_auto_finished_at": self._status.last_auto_finished_at,
                    "next_run_at": self._status.next_run_at,
                    "run_count": self._status.run_count,
                    "status_updated_at": _now(),
                }
            self.store.set_app_metadata(AUTO_COLLECT_STATUS_KEY, json.dumps(payload, ensure_ascii=False))
        except Exception:  # noqa: BLE001 - status persistence should not stop collection.
            logger.exception("auto collector status snapshot persistence failed")

    def _startup_catchup_needed(self) -> bool:
        if not env_bool(STARTUP_CATCHUP_ENV, True):
            return False
        return _should_run_startup_catchup(
            self._status.last_auto_finished_at,
            interval_seconds=self.interval_seconds,
        )

    def _maintenance_poll_seconds(self) -> int:
        return max(60, min(_auto_queue_drain_interval_seconds(), _auto_recovery_interval_seconds(), 600))

    def _run_maintenance_if_due(self, force: bool = False) -> None:
        now = datetime.now(timezone.utc)
        if self._next_maintenance_at and not force and now < self._next_maintenance_at:
            return
        interval = min(_auto_queue_drain_interval_seconds(), _auto_recovery_interval_seconds())
        self._next_maintenance_at = now + timedelta(seconds=interval)
        if not self._run_lock.acquire(blocking=False):
            logger.info("auto maintenance skipped collector busy")
            return
        try:
            self._execute_maintenance_once(now)
        finally:
            self._run_lock.release()

    def _execute_maintenance_once(self, now: datetime) -> None:
        messages: list[str] = []
        source_messages = self._recover_failed_sources_once()
        if source_messages:
            messages.extend(source_messages)
        queue_messages = self._drain_pending_queue_once()
        if queue_messages:
            messages.extend(queue_messages)
        self._persist_daily_report_snapshot(messages, now)

    def _recover_failed_sources_once(self) -> list[str]:
        limit = env_int(AUTO_RECOVERY_LIMIT_ENV, 5, minimum=0)
        if limit <= 0:
            return []
        candidates = _failed_source_candidates(self.store, self.config_path, limit)
        if not candidates:
            return []
        messages: list[str] = []
        retention_cutoff = collection_retention_cutoff_date()
        logger.info("auto recovery source recheck started sources=%s", len(candidates))
        for source in candidates:
            try:
                releases = collect_source_with_fallback(source, limit=max(5, min(self.collect_limit, 10)))
            except Exception as exc:  # noqa: BLE001 - recovery should record and continue per source.
                failure_stage, failure_reason = classify_collection_failure(exc)
                message = f"{source.name} 자동 복구 재검증 실패: {type(exc).__name__}: {exc}"
                self.store.record_source_collection_status(
                    source.id,
                    source.name,
                    "failed",
                    message,
                    failure_stage=failure_stage,
                    failure_reason=failure_reason,
                )
                messages.append(message)
                logger.warning("auto recovery source recheck failed source_id=%s error=%s", source.id, exc)
                continue

            retained_releases, source_skipped = filter_releases_by_retention(releases, retention_cutoff)
            inserted = 0
            for release in retained_releases:
                if self.store.add_press_release(release):
                    inserted += 1
            repaired_dates = repair_missing_published_dates(self.store, source, limit=max(5, min(self.collect_limit, 10)))
            message = (
                f"{source.name} 자동 복구 재검증 통과: "
                f"원문 {len(releases)}건, 새로 저장 {inserted}건"
            )
            if source_skipped:
                message += f", 보관 제외 {source_skipped}건"
            if repaired_dates:
                message += f", 게시일 보정 {repaired_dates}건"
            self.store.record_source_collection_status(
                source.id,
                source.name,
                "ok",
                message.removeprefix(f"{source.name} "),
                releases_found=len(releases),
                inserted_count=inserted,
                repaired_dates=repaired_dates,
            )
            messages.append(message)
        if messages:
            self.store.set_app_metadata(
                AUTO_RECOVERY_STATUS_KEY,
                json.dumps({"updated_at": _now(), "messages": messages[-10:]}, ensure_ascii=False),
            )
        return messages

    def _drain_pending_queue_once(self) -> list[str]:
        if not env_bool(AUTO_QUEUE_DRAIN_ENV, True):
            return []
        limit = env_int(AUTO_QUEUE_DRAIN_LIMIT_ENV, 25, minimum=0)
        if limit <= 0:
            return []
        pending_total = int(self.store.pending_press_release_summary(limit=1).get("total") or 0)
        if pending_total <= 0:
            return []
        logger.info("auto queue drain started pending=%s limit=%s", pending_total, limit)
        messages = draft_pending_releases(self.store, limit=limit, require_gemini=self.require_gemini)
        self.store.set_app_metadata(
            AUTO_RECOVERY_STATUS_KEY,
            json.dumps(
                {
                    "updated_at": _now(),
                    "queue_pending_before": pending_total,
                    "queue_drain_messages": messages[-10:],
                },
                ensure_ascii=False,
            ),
        )
        return [f"Gemini 미변환 큐 자동 소진: 대기 {pending_total}건, 처리 한도 {limit}건"] + messages

    def _persist_daily_report_snapshot(self, messages: list[str], now: datetime) -> None:
        report = _daily_report_snapshot(self.store, now=now, messages=messages)
        self.store.set_app_metadata(AUTO_DAILY_REPORT_KEY, json.dumps(report, ensure_ascii=False))


def build_auto_collector_from_env(store: Store, config_path: Path) -> AutoCollector | None:
    collect_limit = env_int("NEWS_SUMMARY_AUTO_COLLECT_LIMIT", DEFAULT_AUTO_COLLECT_LIMIT)
    source_count = max(1, len([source for source in load_sources(config_path) if source.enabled]))
    default_draft_limit = max(collect_limit * source_count, 250)
    return AutoCollector(
        store=store,
        config_path=config_path,
        interval_seconds=DEFAULT_AUTO_INTERVAL_SECONDS,
        collect_limit=collect_limit,
        draft_limit=env_int("NEWS_SUMMARY_AUTO_DRAFT_LIMIT", default_draft_limit),
        require_gemini=env_bool("NEWS_SUMMARY_AUTO_REQUIRE_GEMINI", True),
        enabled=auto_collect_enabled(store),
    )


def auto_collect_enabled(store: Store) -> bool:
    stored = store.get_app_metadata(AUTO_COLLECT_ENABLED_KEY)
    if stored is not None:
        return stored.strip().lower() in {"1", "true", "yes", "on", "예", "켜기"}
    return env_bool("NEWS_SUMMARY_AUTO_COLLECT", True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _auto_queue_drain_interval_seconds() -> int:
    return env_int(AUTO_QUEUE_DRAIN_INTERVAL_ENV, 900, minimum=60)


def _auto_recovery_interval_seconds() -> int:
    return env_int(AUTO_RECOVERY_INTERVAL_ENV, 900, minimum=60)


def _failed_source_candidates(store: Store, config_path: Path, limit: int):
    source_map = {source.id: source for source in load_sources(config_path) if source.enabled}
    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT scr.source_id, scr.source_name, scr.failure_stage, scr.failure_reason, scr.checked_at
            FROM source_collection_runs scr
            JOIN (
                SELECT source_id, MAX(id) AS max_id
                FROM source_collection_runs
                GROUP BY source_id
            ) latest ON latest.max_id = scr.id
            WHERE scr.status = 'failed'
            ORDER BY scr.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [source_map[str(row["source_id"])] for row in rows if str(row["source_id"]) in source_map]


def _daily_report_snapshot(store: Store, *, now: datetime, messages: list[str]) -> dict[str, object]:
    today = now.astimezone(LOCAL_TZ).date().isoformat()
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
        pending = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM press_releases pr
            LEFT JOIN article_drafts ad ON ad.press_release_id = pr.id
            WHERE ad.id IS NULL
            """
        ).fetchone()["count"]
        failed_sources = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM source_collection_runs scr
            JOIN (
                SELECT source_id, MAX(id) AS max_id
                FROM source_collection_runs
                GROUP BY source_id
            ) latest ON latest.max_id = scr.id
            WHERE scr.status = 'failed'
            """
        ).fetchone()["count"]
    return {
        "date": today,
        "updated_at": now.astimezone(timezone.utc).isoformat(),
        "today_releases": int(releases or 0),
        "today_drafts": int(drafts or 0),
        "pending_releases": int(pending or 0),
        "failed_sources": int(failed_sources or 0),
        "messages": messages[-12:],
    }


def _next_hourly_run_at(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    if now.minute == 0 and now.second == 0 and now.microsecond == 0:
        return now
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def _wait_seconds_until(target: datetime, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    remaining = (target.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()
    if remaining <= 0:
        return 0
    return max(1, int(remaining + 0.999))


def _should_run_startup_catchup(
    last_finished_at: str | None,
    *,
    now: datetime | None = None,
    interval_seconds: int = DEFAULT_AUTO_INTERVAL_SECONDS,
) -> bool:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local_today = now.astimezone(LOCAL_TZ).date()
    holidays = retention_holidays({local_today.year - 1, local_today.year, local_today.year + 1})
    if not is_collection_business_day(local_today, holidays):
        return False
    if not last_finished_at:
        return True

    last_finished = _parse_datetime(last_finished_at)
    if last_finished is None:
        return True
    elapsed = now.astimezone(timezone.utc) - last_finished.astimezone(timezone.utc)
    if elapsed >= timedelta(seconds=max(interval_seconds * 1.5, interval_seconds + 900)):
        return True

    last_local_date = last_finished.astimezone(LOCAL_TZ).date()
    return business_days_between(last_local_date, local_today, holidays) >= 1


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed
