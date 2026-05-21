from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .ops_logging import get_logger
from .service import collect_and_draft_cycle
from .settings import load_sources
from .storage import Store


DEFAULT_AUTO_INTERVAL_SECONDS = 3600
LAST_AUTO_COLLECT_FINISHED_AT_KEY = "last_auto_collect_finished_at"
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
    collect_limit: int = 10
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
        collect_limit: int = 10,
        draft_limit: int = 250,
        require_gemini: bool = True,
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
        source_count = self._enabled_source_count()
        self._status = AutoCollectorStatus(
            enabled=True,
            interval_seconds=interval_seconds,
            collect_limit=collect_limit,
            draft_limit=draft_limit,
            require_gemini=require_gemini,
            progress_total=source_count,
            last_auto_finished_at=self.store.get_app_metadata(LAST_AUTO_COLLECT_FINISHED_AT_KEY),
        )

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="news-summary-auto-collector", daemon=True)
        self._thread.start()
        logger.info("auto collector thread started interval=%s collect_limit=%s", self.interval_seconds, self.collect_limit)

    def stop(self) -> None:
        self._stop_event.set()
        logger.info("auto collector stop requested")

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
        while not self._stop_event.is_set():
            next_run_at = _next_hourly_run_at()
            wait_seconds = _wait_seconds_until(next_run_at)
            self._set_next_run_at(next_run_at, message="다음 정각 자동 수집 대기 중")
            logger.info("auto collector waiting for hourly run wait_seconds=%s next_run_at=%s", wait_seconds, next_run_at.isoformat())
            if self._stop_event.wait(wait_seconds):
                break

            self.run_once()

    def _set_next_run_at(self, next_run_at: datetime, message: str | None = None) -> None:
        with self._state_lock:
            self._status.next_run_at = next_run_at.astimezone(timezone.utc).isoformat()
            if message and not self._status.running:
                self._status.progress_message = message

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

    def _enabled_source_count(self) -> int:
        try:
            return len([source for source in load_sources(self.config_path) if source.enabled])
        except Exception:  # noqa: BLE001 - progress should still render even if config is temporarily invalid.
            logger.exception("enabled source count failed config=%s", self.config_path)
            return 0


def build_auto_collector_from_env(store: Store, config_path: Path) -> AutoCollector | None:
    if not env_bool("NEWS_SUMMARY_AUTO_COLLECT", True):
        return None

    collect_limit = env_int("NEWS_SUMMARY_AUTO_COLLECT_LIMIT", 10)
    source_count = max(1, len([source for source in load_sources(config_path) if source.enabled]))
    default_draft_limit = max(collect_limit * source_count, 250)
    return AutoCollector(
        store=store,
        config_path=config_path,
        interval_seconds=DEFAULT_AUTO_INTERVAL_SECONDS,
        collect_limit=collect_limit,
        draft_limit=env_int("NEWS_SUMMARY_AUTO_DRAFT_LIMIT", default_draft_limit),
        require_gemini=env_bool("NEWS_SUMMARY_AUTO_REQUIRE_GEMINI", True),
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
