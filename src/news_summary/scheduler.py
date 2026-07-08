from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .backup import verify_backup
from .models import Source
from .ops_logging import get_logger
from .service import (
    business_days_between,
    classify_collection_failure,
    collect_and_draft_cycle,
    collect_source_with_fallback,
    collection_retention_cutoff_date,
    draft_pending_releases,
    filter_releases_by_retention,
    is_transient_site_failure,
    is_collection_business_day,
    prune_decorative_press_release_assets,
    repair_missing_published_dates,
    retention_holidays,
)
from .settings import env_path, load_sources
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
AUTO_NETWORK_FAILURE_RECHECK_COOLDOWN_ENV = "NEWS_SUMMARY_AUTO_NETWORK_FAILURE_RECHECK_COOLDOWN_SECONDS"
AUTO_QUIET_SOURCE_RECHECK_ENV = "NEWS_SUMMARY_AUTO_QUIET_SOURCE_RECHECK"
AUTO_QUIET_SOURCE_RECHECK_HOUR_ENV = "NEWS_SUMMARY_AUTO_QUIET_SOURCE_RECHECK_HOUR"
AUTO_FOCUSED_RECRAWL_LIMIT_ENV = "NEWS_SUMMARY_AUTO_FOCUSED_RECRAWL_LIMIT"
AUTO_ANOMALY_CHECK_HOUR_ENV = "NEWS_SUMMARY_AUTO_ANOMALY_CHECK_HOUR"
AUTO_DEDUPLICATE_ENV = "NEWS_SUMMARY_AUTO_DEDUPLICATE"
AUTO_DEDUPLICATE_LIMIT_ENV = "NEWS_SUMMARY_AUTO_DEDUPLICATE_LIMIT"
AUTO_URL_DISCOVERY_LIMIT_ENV = "NEWS_SUMMARY_AUTO_URL_DISCOVERY_LIMIT"
AUTO_BACKUP_VERIFY_ENV = "NEWS_SUMMARY_AUTO_BACKUP_VERIFY"
PUBLIC_URL_ENV = "NEWS_SUMMARY_PUBLIC_URL"
AUTO_RECOVERY_STATUS_KEY = "auto_recovery_status_snapshot"
AUTO_DAILY_REPORT_KEY = "auto_daily_report_snapshot"
AUTO_URL_DISCOVERY_STATUS_KEY = "auto_url_discovery_snapshot"
AUTO_BACKUP_VERIFY_STATUS_KEY = "auto_backup_verify_snapshot"
AUTO_SERVER_HEALTH_STATUS_KEY = "auto_server_health_snapshot"
AUTO_COLLECTION_ANOMALY_STATUS_KEY = "auto_collection_anomaly_snapshot"
AUTO_DEDUPLICATE_STATUS_KEY = "auto_deduplicate_snapshot"
AUTO_OPERATIONS_SUMMARY_STATUS_KEY = "auto_operations_summary_snapshot"
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
    thread_alive: bool | None = None
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


@dataclass(frozen=True)
class SourceRecoveryCandidate:
    source: Source
    reason: str


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
            report_messages = list(messages[-12:])
            if error:
                report_messages.append(f"{label} 실패: {error}")
            self._refresh_collection_report_snapshots(report_messages, finished)
        logger.info("collector run finished label=%s error=%s messages=%s", label, bool(error), len(messages))

        return messages

    def _refresh_collection_report_snapshots(self, messages: list[str], finished_at: str) -> None:
        """Keep operations reports aligned with the latest collection result."""
        refreshed_at = _parse_datetime(finished_at) or datetime.now(timezone.utc)
        if refreshed_at.tzinfo is None:
            refreshed_at = refreshed_at.replace(tzinfo=timezone.utc)
        try:
            self._persist_server_health_snapshot(refreshed_at)
            self._persist_collection_anomaly_snapshot(refreshed_at)
            self._persist_daily_report_snapshot(messages, refreshed_at)
            self._persist_operations_summary_snapshot(messages, refreshed_at)
        except Exception:  # noqa: BLE001 - report refresh must not break collection.
            logger.exception("collection report snapshot refresh failed")

    def snapshot(self) -> AutoCollectorStatus:
        thread_alive = bool(self._thread and self._thread.is_alive())
        with self._state_lock:
            return AutoCollectorStatus(
                enabled=self._status.enabled,
                running=self._status.running,
                thread_alive=thread_alive,
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
                    "thread_alive": bool(self._thread and self._thread.is_alive()),
                    "interval_seconds": self._status.interval_seconds,
                    "collect_limit": self._status.collect_limit,
                    "draft_limit": self._status.draft_limit,
                    "require_gemini": self._status.require_gemini,
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
        server_health = self._persist_server_health_snapshot(now)
        if server_health.get("status_level") == "error":
            messages.append(f"서버 상태 확인 필요: {server_health.get('message')}")
        anomaly_report = self._persist_collection_anomaly_snapshot(now)
        if anomaly_report.get("issue_count"):
            messages.append(f"수집 이상치 {anomaly_report.get('issue_count')}건 감지")
        dedupe_message = self._deduplicate_press_releases_once()
        if dedupe_message:
            messages.append(dedupe_message)
        asset_cleanup = prune_decorative_press_release_assets(self.store)
        if asset_cleanup["deleted"]:
            messages.append(f"보도자료 장식 이미지 {asset_cleanup['deleted']}건 정리")
        source_messages = self._recover_failed_sources_once()
        if source_messages:
            messages.extend(source_messages)
        queue_messages = self._drain_pending_queue_once()
        if queue_messages:
            messages.extend(queue_messages)
        discovery_messages = self._discover_fallback_url_candidates_once()
        if discovery_messages:
            messages.extend(discovery_messages)
        backup_message = self._verify_latest_backup_once()
        if backup_message:
            messages.append(backup_message)
        self._persist_daily_report_snapshot(messages, now)
        self._persist_operations_summary_snapshot(messages, now)

    def _persist_server_health_snapshot(self, now: datetime) -> dict[str, object]:
        started = now
        checks: list[dict[str, object]] = []
        status_level = "ok"
        message = "서버 내부 점검 정상"
        try:
            with self.store.connect() as conn:
                conn.execute("SELECT 1").fetchone()
            checks.append({"name": "database", "ok": True, "message": "DB 연결 정상"})
        except Exception as exc:  # noqa: BLE001 - health snapshot should report errors without stopping maintenance.
            status_level = "error"
            message = f"DB 연결 실패: {type(exc).__name__}"
            checks.append({"name": "database", "ok": False, "message": message})

        with self._state_lock:
            status_updated_at = _parse_datetime(self._status.last_finished_at or self._status.last_started_at)
            collector_enabled = self._status.enabled
            collector_running = self._status.running
            collector_thread_alive = bool(self._thread and self._thread.is_alive())
        if collector_enabled and not collector_running and status_updated_at:
            stale_minutes = int((started - status_updated_at.astimezone(timezone.utc)).total_seconds() // 60)
            if stale_minutes >= 120:
                status_level = "warning" if status_level == "ok" else status_level
                checks.append({"name": "auto_collector", "ok": False, "message": f"자동 수집 최근 실행 후 {stale_minutes}분 경과"})
            elif not collector_thread_alive:
                status_level = "warning" if status_level == "ok" else status_level
                checks.append({"name": "auto_collector", "ok": False, "message": "자동 수집 백그라운드 스레드 중단"})
            else:
                checks.append({"name": "auto_collector", "ok": True, "message": "자동 수집 상태 정상"})
        elif collector_enabled and collector_running:
            checks.append({"name": "auto_collector", "ok": True, "message": "자동 수집 실행 중"})
        elif collector_enabled and not collector_thread_alive:
            status_level = "warning" if status_level == "ok" else status_level
            checks.append({"name": "auto_collector", "ok": False, "message": "자동 수집 백그라운드 스레드 미시작"})
        elif not collector_enabled:
            status_level = "warning" if status_level == "ok" else status_level
            checks.append({"name": "auto_collector", "ok": False, "message": "자동 수집 꺼짐"})

        public_url = os.getenv(PUBLIC_URL_ENV, "").strip().rstrip("/")
        if public_url:
            health_url = f"{public_url}/healthz"
            try:
                response = httpx.get(health_url, timeout=5)
                response.raise_for_status()
                checks.append({"name": "public_url", "ok": True, "message": f"외부 healthz {response.status_code}"})
            except Exception as exc:  # noqa: BLE001 - public check is diagnostic only.
                status_level = "warning" if status_level == "ok" else status_level
                checks.append({"name": "public_url", "ok": False, "message": f"외부 healthz 실패: {type(exc).__name__}"})

        payload = {
            "updated_at": started.isoformat(),
            "status_level": status_level,
            "status_label": "정상" if status_level == "ok" else ("확인 필요" if status_level == "error" else "주의"),
            "message": message,
            "checks": checks,
        }
        self.store.set_app_metadata(AUTO_SERVER_HEALTH_STATUS_KEY, json.dumps(payload, ensure_ascii=False))
        return payload

    def _persist_collection_anomaly_snapshot(self, now: datetime) -> dict[str, object]:
        report = _collection_anomaly_snapshot(self.store, self.config_path, now=now)
        self.store.set_app_metadata(AUTO_COLLECTION_ANOMALY_STATUS_KEY, json.dumps(report, ensure_ascii=False))
        return report

    def _deduplicate_press_releases_once(self) -> str | None:
        if not env_bool(AUTO_DEDUPLICATE_ENV, True):
            return None
        limit = env_int(AUTO_DEDUPLICATE_LIMIT_ENV, 50, minimum=0)
        if limit <= 0:
            return None
        result = self.store.deduplicate_press_releases(limit=limit)
        payload = {"updated_at": _now(), **result}
        self.store.set_app_metadata(AUTO_DEDUPLICATE_STATUS_KEY, json.dumps(payload, ensure_ascii=False))
        merged = int(result.get("merged") or 0)
        skipped = int(result.get("skipped") or 0)
        if merged:
            return f"중복 원문 자동 정리 {merged}건"
        if skipped:
            return f"중복 원문 {skipped}건은 초안 충돌로 보류"
        return None

    def _recover_failed_sources_once(self) -> list[str]:
        limit = env_int(AUTO_RECOVERY_LIMIT_ENV, 5, minimum=0)
        if limit <= 0:
            return []
        candidates = _source_recovery_candidates(self.store, self.config_path, limit)
        if not candidates:
            return []
        messages: list[str] = []
        retention_cutoff = collection_retention_cutoff_date()
        logger.info("auto recovery source recheck started sources=%s", len(candidates))
        for candidate in candidates:
            source = candidate.source
            prefix = {
                "quiet": "업무일 무수집 보정 점검",
                "focused": "이상치 집중 재수집",
            }.get(candidate.reason, "자동 복구 재검증")
            try:
                releases = collect_source_with_fallback(source, limit=max(5, min(self.collect_limit, 10)))
            except Exception as exc:  # noqa: BLE001 - recovery should record and continue per source.
                failure_stage, failure_reason = classify_collection_failure(exc)
                message = f"{source.name} {prefix} 실패: {type(exc).__name__}: {exc}"
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
                f"{source.name} {prefix} 통과: "
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

    def _discover_fallback_url_candidates_once(self) -> list[str]:
        limit = env_int(AUTO_URL_DISCOVERY_LIMIT_ENV, 3, minimum=0)
        if limit <= 0:
            return []
        candidates = _url_discovery_candidates(self.store, self.config_path, limit)
        if not candidates:
            return []
        discoveries: list[dict[str, object]] = []
        messages: list[str] = []
        for source in candidates:
            urls = _discover_candidate_urls(source)
            if not urls:
                continue
            discoveries.append(
                {
                    "source_id": source.id,
                    "source_name": source.name,
                    "urls": urls,
                }
            )
            messages.append(f"{source.name} 대체 URL 후보 {len(urls)}개 발견")
        if discoveries:
            self.store.set_app_metadata(
                AUTO_URL_DISCOVERY_STATUS_KEY,
                json.dumps({"updated_at": _now(), "discoveries": discoveries[-10:]}, ensure_ascii=False),
            )
        return messages

    def _verify_latest_backup_once(self) -> str | None:
        if not env_bool(AUTO_BACKUP_VERIFY_ENV, True):
            return None
        backup_dir = env_path("NEWS_SUMMARY_BACKUP_DIR", "data/backups")
        latest_backup = _latest_backup_file(backup_dir)
        result = verify_backup(latest_backup) if latest_backup else {
            "ok": False,
            "status_label": "백업 없음",
            "message": "검증할 백업 파일이 없습니다.",
            "checked_sqlite": False,
        }
        payload = {
            "updated_at": _now(),
            "backup_name": latest_backup.name if latest_backup else "",
            **result,
        }
        self.store.set_app_metadata(AUTO_BACKUP_VERIFY_STATUS_KEY, json.dumps(payload, ensure_ascii=False))
        if result.get("ok"):
            return None
        return f"백업 자동 검증 확인 필요: {result.get('message')}"

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

    def _persist_operations_summary_snapshot(self, messages: list[str], now: datetime) -> None:
        report = _operations_summary_snapshot(self.store, now=now, messages=messages)
        self.store.set_app_metadata(AUTO_OPERATIONS_SUMMARY_STATUS_KEY, json.dumps(report, ensure_ascii=False))


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


def _network_failure_recheck_cooldown_seconds() -> int:
    return env_int(AUTO_NETWORK_FAILURE_RECHECK_COOLDOWN_ENV, 21600, minimum=900)


def _source_recovery_candidates(store: Store, config_path: Path, limit: int) -> list[SourceRecoveryCandidate]:
    failed = _failed_source_candidates(store, config_path, limit)
    remaining = max(0, limit - len(failed))
    focused = _focused_source_candidates(store, config_path, remaining) if remaining else []
    remaining = max(0, remaining - len(focused))
    quiet = _quiet_source_candidates(store, config_path, remaining) if remaining else []
    seen: set[str] = set()
    candidates: list[SourceRecoveryCandidate] = []
    for candidate in [*failed, *focused, *quiet]:
        if candidate.source.id in seen:
            continue
        seen.add(candidate.source.id)
        candidates.append(candidate)
    return candidates[:limit]


def _failed_source_candidates(store: Store, config_path: Path, limit: int) -> list[SourceRecoveryCandidate]:
    source_map = {source.id: source for source in load_sources(config_path) if source.enabled}
    now = datetime.now(timezone.utc)
    network_cooldown = timedelta(seconds=_network_failure_recheck_cooldown_seconds())
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
            (max(limit * 4, limit),),
        ).fetchall()
    candidates: list[SourceRecoveryCandidate] = []
    for row in rows:
        source_id = str(row["source_id"])
        if source_id not in source_map:
            continue
        stage = str(row["failure_stage"] or "")
        reason = str(row["failure_reason"] or "")
        checked_at = _parse_datetime(str(row["checked_at"] or ""))
        if is_transient_site_failure(stage, reason) and checked_at:
            elapsed = now.astimezone(timezone.utc) - checked_at.astimezone(timezone.utc)
            if elapsed < network_cooldown:
                continue
        candidates.append(SourceRecoveryCandidate(source_map[source_id], "failed"))
        if len(candidates) >= limit:
            break
    return candidates


def _quiet_source_candidates(store: Store, config_path: Path, limit: int) -> list[SourceRecoveryCandidate]:
    if limit <= 0 or not env_bool(AUTO_QUIET_SOURCE_RECHECK_ENV, True):
        return []
    now = datetime.now(LOCAL_TZ)
    today = now.date()
    holidays = retention_holidays({today.year - 1, today.year, today.year + 1})
    if not is_collection_business_day(today, holidays):
        return []
    check_hour = env_int(AUTO_QUIET_SOURCE_RECHECK_HOUR_ENV, 9, minimum=0)
    if now.hour < min(check_hour, 23):
        return []

    source_map = {source.id: source for source in load_sources(config_path) if source.enabled}
    if not source_map:
        return []
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
    with store.connect() as conn:
        release_counts = {
            str(row["source_id"]): int(row["count"] or 0)
            for row in conn.execute(
                f"""
                SELECT source_id, COUNT(*) AS count
                FROM press_releases
                WHERE {date_expr} = ?
                GROUP BY source_id
                """,
                (today.isoformat(),),
            ).fetchall()
        }
        checked_today = {
            str(row["source_id"])
            for row in conn.execute(
                """
                SELECT DISTINCT source_id
                FROM source_collection_runs
                WHERE checked_at >= ?
                  AND message LIKE ?
                """,
                (local_start, "%업무일 무수집 보정%"),
            ).fetchall()
        }
        latest_statuses = {
            str(row["source_id"]): str(row["status"])
            for row in conn.execute(
                """
                SELECT scr.source_id, scr.status
                FROM source_collection_runs scr
                JOIN (
                    SELECT source_id, MAX(id) AS max_id
                    FROM source_collection_runs
                    GROUP BY source_id
                ) latest ON latest.max_id = scr.id
                """
            ).fetchall()
        }
    candidates = []
    for source in source_map.values():
        if release_counts.get(source.id, 0) > 0:
            continue
        if source.id in checked_today:
            continue
        if latest_statuses.get(source.id) == "failed":
            continue
        candidates.append(SourceRecoveryCandidate(source, "quiet"))
    return candidates[:limit]


def _focused_source_candidates(store: Store, config_path: Path, limit: int) -> list[SourceRecoveryCandidate]:
    if limit <= 0:
        return []
    focus_limit = env_int(AUTO_FOCUSED_RECRAWL_LIMIT_ENV, 3, minimum=0)
    if focus_limit <= 0:
        return []
    now = datetime.now(timezone.utc)
    anomaly_report = _collection_anomaly_snapshot(store, config_path, now=now)
    source_map = {source.id: source for source in load_sources(config_path) if source.enabled}
    local_today = now.astimezone(LOCAL_TZ).date()
    local_start = datetime.combine(local_today, datetime.min.time(), tzinfo=LOCAL_TZ).astimezone(timezone.utc).isoformat()
    with store.connect() as conn:
        checked_today = {
            str(row["source_id"])
            for row in conn.execute(
                """
                SELECT DISTINCT source_id
                FROM source_collection_runs
                WHERE checked_at >= ?
                  AND message LIKE ?
                """,
                (local_start, "%이상치 집중 재수집%"),
            ).fetchall()
        }
    candidates: list[SourceRecoveryCandidate] = []
    for issue in anomaly_report.get("issues", []):
        if not isinstance(issue, dict):
            continue
        if issue.get("type") not in {"today_zero", "drop"}:
            continue
        source_id = str(issue.get("source_id") or "")
        if source_id not in source_map or source_id in checked_today:
            continue
        candidates.append(SourceRecoveryCandidate(source_map[source_id], "focused"))
        if len(candidates) >= min(limit, focus_limit):
            break
    return candidates


def _url_discovery_candidates(store: Store, config_path: Path, limit: int) -> list[Source]:
    source_map = {source.id: source for source in load_sources(config_path) if source.enabled}
    if not source_map:
        return []
    with store.connect() as conn:
        latest_rows = conn.execute(
            """
            SELECT scr.*
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
            (max(limit * 3, limit),),
        ).fetchall()
        status_rows = conn.execute(
            """
            SELECT source_id, status
            FROM source_collection_runs
            ORDER BY source_id, id DESC
            """
        ).fetchall()
    consecutive_failures = _consecutive_failure_counts(status_rows)
    candidates = []
    structural_stages = {"사이트 구조 변경", "수집 처리", "자료 파싱", "HTTP 상태 오류", "사이트 접속", "DNS 조회"}
    for row in latest_rows:
        source_id = str(row["source_id"])
        if source_id not in source_map:
            continue
        if consecutive_failures.get(source_id, 0) < 3:
            continue
        if str(row["failure_stage"] or "") not in structural_stages:
            continue
        candidates.append(source_map[source_id])
        if len(candidates) >= limit:
            break
    return candidates


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


def _discover_candidate_urls(source: Source, limit: int = 5) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    keywords = ("보도자료", "시정뉴스", "군정뉴스", "군정소식", "시정소식", "새소식", "뉴스")
    with httpx.Client(timeout=8, follow_redirects=True, verify=source.verify_ssl) as client:
        for home_url in _source_home_urls(source):
            try:
                response = client.get(home_url)
                response.raise_for_status()
            except Exception as exc:  # noqa: BLE001 - discovery is best-effort diagnostics.
                logger.info("url discovery homepage failed source_id=%s url=%s error=%s", source.id, home_url, exc)
                continue
            soup = BeautifulSoup(response.text, "html.parser")
            for node in soup.select("a[href]"):
                href = str(node.get("href") or "").strip()
                text = " ".join(node.get_text(" ").split())
                haystack = f"{text} {href}"
                if not any(keyword in haystack for keyword in keywords):
                    continue
                candidate_url = urljoin(str(response.url), href)
                if not _same_host(candidate_url, home_url):
                    continue
                normalized = candidate_url.split("#", 1)[0]
                if normalized in seen:
                    continue
                seen.add(normalized)
                urls.append(normalized)
                if len(urls) >= limit:
                    return urls
    return urls


def _source_home_urls(source: Source) -> list[str]:
    raw_urls = [source.base_url, source.list_url, source.feed_url]
    homes: list[str] = []
    seen: set[str] = set()
    for raw_url in raw_urls:
        if not raw_url:
            continue
        parsed = urlparse(raw_url)
        if not parsed.scheme or not parsed.netloc:
            continue
        for candidate in (raw_url, f"{parsed.scheme}://{parsed.netloc}/"):
            if candidate not in seen:
                seen.add(candidate)
                homes.append(candidate)
    return homes


def _same_host(url: str, base_url: str) -> bool:
    parsed = urlparse(url)
    base = urlparse(base_url)
    return bool(parsed.scheme and parsed.netloc and parsed.netloc == base.netloc)


def _latest_backup_file(backup_dir: Path) -> Path | None:
    if not backup_dir.exists():
        return None
    files = [path for path in backup_dir.glob("*.zip") if path.is_file()]
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


def _collection_anomaly_snapshot(store: Store, config_path: Path, *, now: datetime) -> dict[str, object]:
    local_now = now.astimezone(LOCAL_TZ)
    today = local_now.date()
    holidays = retention_holidays({today.year - 1, today.year, today.year + 1})
    is_business_day = is_collection_business_day(today, holidays)
    check_hour = env_int(AUTO_ANOMALY_CHECK_HOUR_ENV, 10, minimum=0)
    enabled_sources = {source.id: source for source in load_sources(config_path) if source.enabled}
    if not enabled_sources:
        return {
            "updated_at": now.isoformat(),
            "status_level": "warning",
            "status_label": "소스 없음",
            "issue_count": 0,
            "issues": [],
        }

    cutoff = collection_retention_cutoff_date(today=today)
    date_expr = _press_release_date_expr()
    with store.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT source_id, {date_expr} AS release_date, COUNT(*) AS count
            FROM press_releases
            WHERE {date_expr} >= ?
            GROUP BY source_id, {date_expr}
            """,
            (cutoff.isoformat(),),
        ).fetchall()

    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        source_id = str(row["source_id"])
        release_date = str(row["release_date"] or "")
        if not source_id or not release_date:
            continue
        counts.setdefault(source_id, {})[release_date] = int(row["count"] or 0)

    issues: list[dict[str, object]] = []
    for source_id, source in enabled_sources.items():
        by_date = counts.get(source_id, {})
        today_count = by_date.get(today.isoformat(), 0)
        baseline_values = [
            count
            for release_date, count in by_date.items()
            if release_date != today.isoformat() and count > 0
        ]
        if not baseline_values:
            continue
        average = sum(baseline_values) / len(baseline_values)
        if is_business_day and local_now.hour >= min(check_hour, 23) and today_count == 0 and average >= 1:
            issues.append(
                {
                    "source_id": source_id,
                    "source_name": source.name,
                    "type": "today_zero",
                    "label": "오늘 0건",
                    "today_count": today_count,
                    "average": round(average, 1),
                }
            )
        elif is_business_day and local_now.hour >= min(check_hour + 3, 23) and average >= 2 and today_count < max(1, average * 0.3):
            issues.append(
                {
                    "source_id": source_id,
                    "source_name": source.name,
                    "type": "drop",
                    "label": "평소 대비 급감",
                    "today_count": today_count,
                    "average": round(average, 1),
                }
            )
        elif average >= 2 and today_count >= max(10, average * 4):
            issues.append(
                {
                    "source_id": source_id,
                    "source_name": source.name,
                    "type": "spike",
                    "label": "평소 대비 급증",
                    "today_count": today_count,
                    "average": round(average, 1),
                }
            )

    issues = sorted(
        issues,
        key=lambda item: (
            {"today_zero": 0, "drop": 1, "spike": 2}.get(str(item.get("type")), 9),
            str(item.get("source_name") or ""),
        ),
    )
    status_level = "warning" if issues else "ok"
    return {
        "updated_at": now.isoformat(),
        "status_level": status_level,
        "status_label": "확인 필요" if issues else "정상",
        "issue_count": len(issues),
        "issues": issues[:20],
    }


def _operations_summary_snapshot(store: Store, *, now: datetime, messages: list[str]) -> dict[str, object]:
    local_today = now.astimezone(LOCAL_TZ).date()
    local_start = datetime.combine(local_today, datetime.min.time(), tzinfo=LOCAL_TZ).astimezone(timezone.utc).isoformat()
    date_expr = _press_release_date_expr()
    with store.connect() as conn:
        source_runs = conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM source_collection_runs
            WHERE checked_at >= ?
            GROUP BY status
            """,
            (local_start,),
        ).fetchall()
        recovery_successes = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM source_collection_runs
            WHERE checked_at >= ?
              AND status = 'ok'
              AND (
                message LIKE '%자동 복구 재검증%'
                OR message LIKE '%업무일 무수집 보정%'
                OR message LIKE '%이상치 집중 재수집%'
              )
            """,
            (local_start,),
        ).fetchone()["count"]
        today_sources = conn.execute(
            f"""
            SELECT COUNT(DISTINCT source_id) AS count
            FROM press_releases
            WHERE {date_expr} = ?
            """,
            (local_today.isoformat(),),
        ).fetchone()["count"]
    run_counts = {str(row["status"]): int(row["count"] or 0) for row in source_runs}
    draft_failures = store.draft_generation_failure_summary(limit=1)
    anomaly_raw = store.get_app_metadata(AUTO_COLLECTION_ANOMALY_STATUS_KEY)
    dedupe_raw = store.get_app_metadata(AUTO_DEDUPLICATE_STATUS_KEY)
    server_raw = store.get_app_metadata(AUTO_SERVER_HEALTH_STATUS_KEY)
    anomaly_count = _json_int(anomaly_raw, "issue_count")
    dedupe_merged = _json_int(dedupe_raw, "merged")
    server_level = _json_str(server_raw, "status_level") or "unknown"
    return {
        "date": local_today.isoformat(),
        "updated_at": now.isoformat(),
        "source_successes": run_counts.get("ok", 0),
        "source_failures": run_counts.get("failed", 0),
        "recovery_successes": int(recovery_successes or 0),
        "today_active_sources": int(today_sources or 0),
        "draft_failures": int(draft_failures.get("total") or 0),
        "anomaly_count": anomaly_count,
        "dedupe_merged": dedupe_merged,
        "server_status_level": server_level,
        "messages": messages[-8:],
    }


def _press_release_date_expr() -> str:
    return (
        "REPLACE("
        "REPLACE("
        "SUBSTR(TRIM(COALESCE(NULLIF(published_at, ''), collected_at, '')), 1, 10), "
        "'.', '-'"
        "), "
        "'/', '-'"
        ")"
    )


def _json_int(raw_value: str | None, key: str) -> int:
    if not raw_value:
        return 0
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        return int(payload.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _json_str(raw_value: str | None, key: str) -> str:
    if not raw_value:
        return ""
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get(key) or "")


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
    draft_failures = store.draft_generation_failure_summary(limit=1)
    return {
        "date": today,
        "updated_at": now.astimezone(timezone.utc).isoformat(),
        "today_releases": int(releases or 0),
        "today_drafts": int(drafts or 0),
        "pending_releases": int(pending or 0),
        "failed_sources": int(failed_sources or 0),
        "draft_failures": int(draft_failures.get("total") or 0),
        "draft_retry_due": int(draft_failures.get("due") or 0),
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
