# 수집 회차 중에는 배포를 거절하는 푸시 도구 — 회차를 끊는 사고를 명령 자체가 막는다
from __future__ import annotations

import argparse
import subprocess
import sys
import time

import httpx

HEALTH_URL = "https://news-express-1.onrender.com/healthz"
POLL_SECONDS = 30


def collector_state() -> tuple[object, str]:
    payload = httpx.get(HEALTH_URL, timeout=20).json()
    return payload.get("auto_collector_run_minutes"), str(payload.get("service_status_label") or "")


def main() -> int:
    parser = argparse.ArgumentParser(description="수집 회차가 끝난 뒤에만 푸시한다.")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument(
        "--wait-minutes",
        type=int,
        default=25,
        help="회차 중이면 이만큼 기다린다. 0이면 기다리지 않고 거절한다.",
    )
    args = parser.parse_args()

    deadline = time.monotonic() + args.wait_minutes * 60
    while True:
        try:
            run_minutes, status = collector_state()
        except Exception as exc:  # noqa: BLE001 - 상태를 못 읽으면 배포를 미룬다.
            print(f"상태 확인 실패({type(exc).__name__}) — 배포하지 않습니다.", file=sys.stderr)
            return 2

        if run_minutes is None:
            print(f"수집 회차 없음 (서비스 {status}) — 푸시합니다.")
            break

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(
                f"수집 회차 진행 중(run={run_minutes}분) — 배포를 거절합니다. "
                "회차를 끊으면 그 회차 수집이 유실됩니다.",
                file=sys.stderr,
            )
            return 1
        print(f"수집 회차 진행 중(run={run_minutes}분) — {int(remaining // 60)}분 더 기다립니다.")
        time.sleep(POLL_SECONDS)

    return subprocess.call(["git", "push", args.remote, args.ref])


if __name__ == "__main__":
    raise SystemExit(main())
