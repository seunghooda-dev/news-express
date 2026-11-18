# 수집 회차 중에는 배포를 거절하는 푸시 도구 — 회차를 끊는 사고를 명령 자체가 막는다
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

HEALTH_URL = "https://news-express-1.onrender.com/healthz"
POLL_SECONDS = 30

# Render가 GitHub 푸시를 못 알아챌 때(2026-11 실측: 푸시 두 건에 배포 기록이 아예 없었다)를 위한
# 보완 경로. 이 값이 있으면 푸시 직후 Render 배포 훅을 직접 부른다. URL에 비밀 키가 들어 있으니
# 저장소에 넣지 말고 환경변수나 .gitignore된 .env에만 둔다. 자동 배포가 복구되면 값을 지운다
# (둘 다 켜 두면 같은 커밋이 두 번 배포된다).
HOOK_ENV = "RENDER_DEPLOY_HOOK_URL"
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
# 훅은 Render의 [skip render] 판정을 거치지 않으므로 같은 규약을 여기서 지킨다.
SKIP_MARKERS = ("[skip render]", "[render skip]", "[skip deploy]", "[deploy skip]")


def deploy_hook_url() -> str | None:
    value = os.getenv(HOOK_ENV)
    if not value:
        try:
            from dotenv import dotenv_values

            value = dotenv_values(ENV_PATH).get(HOOK_ENV)
        except ModuleNotFoundError:
            value = None
    return (value or "").strip() or None


def head_message(ref: str) -> str:
    return subprocess.check_output(["git", "log", "-1", "--format=%B", ref], text=True, encoding="utf-8")


def trigger_deploy_hook(url: str, message: str) -> int:
    if any(marker in message.lower() for marker in SKIP_MARKERS):
        print("커밋 메시지에 [skip render]가 있어 배포 훅을 부르지 않습니다.")
        return 0
    try:
        response = httpx.get(url, timeout=20)
    except httpx.HTTPError as exc:
        # 예외 메시지에는 키가 든 URL이 그대로 실릴 수 있어 종류만 남긴다.
        print(f"배포 훅 호출 실패({type(exc).__name__}) — 푸시는 끝났으니 Render 대시보드에서 수동 배포하세요.", file=sys.stderr)
        return 3
    if response.status_code >= 400:
        print(f"배포 훅이 HTTP {response.status_code}를 돌려줬습니다 — Render 대시보드에서 수동 배포하세요.", file=sys.stderr)
        return 3
    print(f"배포 훅 호출 완료(HTTP {response.status_code}) — Render가 배포를 시작합니다.")
    return 0


def collector_state() -> tuple[object, str]:
    payload = httpx.get(HEALTH_URL, timeout=20).json()
    return payload.get("auto_collector_run_minutes"), str(payload.get("service_status_label") or "")


def minutes_to_next_hour() -> int:
    now = datetime.now(timezone.utc)
    return 60 - now.minute - (1 if now.second else 0)


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
    parser.add_argument(
        "--build-minutes",
        type=int,
        default=12,
        help=(
            "빌드가 끝나 새 프로세스가 뜨기까지 걸리는 시간. 정각까지 이보다 적게 남았으면 "
            "곧 시작할 회차를 배포가 덮치므로 기다린다."
        ),
    )
    args = parser.parse_args()

    deadline = time.monotonic() + args.wait_minutes * 60
    while True:
        try:
            run_minutes, status = collector_state()
        except Exception as exc:  # noqa: BLE001 - 상태를 못 읽으면 배포를 미룬다.
            print(f"상태 확인 실패({type(exc).__name__}) — 배포하지 않습니다.", file=sys.stderr)
            return 2

        # 회차 중이 아니어도, 빌드가 끝날 무렵 정각 회차가 시작되면 그 회차가 죽는다.
        # 2026-08-09에 8~12분 간격으로 여섯 번 배포해 두 시간치 수집을 통째로 날렸다.
        if run_minutes is None and minutes_to_next_hour() <= args.build_minutes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print(
                    f"정각까지 {minutes_to_next_hour()}분 — 빌드가 회차를 덮칩니다. 배포를 거절합니다.",
                    file=sys.stderr,
                )
                return 1
            print(f"정각까지 {minutes_to_next_hour()}분 — 회차를 덮치지 않도록 기다립니다.")
            time.sleep(POLL_SECONDS)
            continue

        if run_minutes is None:
            print(f"수집 회차 없음 · 정각까지 {minutes_to_next_hour()}분 (서비스 {status}) — 푸시합니다.")
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

    code = subprocess.call(["git", "push", args.remote, args.ref])
    if code != 0:
        return code
    hook_url = deploy_hook_url()
    if not hook_url:
        return 0
    return trigger_deploy_hook(hook_url, head_message(args.ref))


if __name__ == "__main__":
    raise SystemExit(main())
