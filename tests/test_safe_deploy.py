# safe_deploy.py의 푸시 뒤 배포 훅 호출(Render 푸시 감지가 끊겼을 때의 보완 경로)을 점검한다
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

HOOK_URL = "https://api.render.com/deploy/srv-test?key=TOP-SECRET-KEY"


def _load_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "safe_deploy.py"
    spec = importlib.util.spec_from_file_location("safe_deploy_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code


@pytest.fixture
def deploy(monkeypatch, tmp_path):
    """수집 회차 점검·푸시·커밋 메시지를 가짜로 바꾼 safe_deploy 모듈과 호출 기록을 돌려준다."""
    module = _load_module()
    calls = {"push": [], "hook": []}
    state = {"push_code": 0, "message": "fix: 평범한 커밋", "hook": lambda url: _Response(200)}

    monkeypatch.setattr(sys, "argv", ["safe_deploy.py"])
    monkeypatch.setattr(module, "collector_state", lambda: (None, "정상"))
    monkeypatch.setattr(module, "minutes_to_next_hour", lambda: 40)
    monkeypatch.setattr(module, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(module, "head_message", lambda ref: state["message"])
    monkeypatch.delenv("RENDER_DEPLOY_HOOK_URL", raising=False)

    def fake_push(args):
        calls["push"].append(args)
        return state["push_code"]

    def fake_get(url, **kwargs):
        calls["hook"].append(url)
        return state["hook"](url)

    monkeypatch.setattr(module.subprocess, "call", fake_push)
    monkeypatch.setattr(module.httpx, "get", fake_get)
    return module, calls, state


def test_hook_is_called_after_a_successful_push(deploy, monkeypatch):
    module, calls, _state = deploy
    monkeypatch.setenv("RENDER_DEPLOY_HOOK_URL", HOOK_URL)

    assert module.main() == 0

    assert calls["push"] == [["git", "push", "origin", "HEAD"]]
    assert calls["hook"] == [HOOK_URL]


@pytest.mark.parametrize("marker", ["[skip render]", "[Render Skip]", "[skip deploy]", "[DEPLOY SKIP]"])
def test_hook_is_skipped_when_the_commit_asks_to_skip_render(deploy, monkeypatch, marker):
    module, calls, state = deploy
    monkeypatch.setenv("RENDER_DEPLOY_HOOK_URL", HOOK_URL)
    state["message"] = f"docs: 문서만 고쳤다 {marker}"

    assert module.main() == 0

    assert calls["push"] == [["git", "push", "origin", "HEAD"]]
    assert calls["hook"] == []


def test_no_hook_is_called_without_a_configured_url(deploy):
    module, calls, _state = deploy

    assert module.main() == 0

    assert len(calls["push"]) == 1
    assert calls["hook"] == []


def test_hook_url_can_come_from_the_dotenv_file(deploy, tmp_path):
    module, calls, _state = deploy
    (tmp_path / ".env").write_text(f"OTHER=1\nRENDER_DEPLOY_HOOK_URL={HOOK_URL}\n", encoding="utf-8")

    assert module.main() == 0

    assert calls["hook"] == [HOOK_URL]


def test_hook_is_not_called_when_the_push_fails(deploy, monkeypatch):
    module, calls, state = deploy
    monkeypatch.setenv("RENDER_DEPLOY_HOOK_URL", HOOK_URL)
    state["push_code"] = 1

    assert module.main() == 1

    assert calls["hook"] == []


def test_hook_http_error_returns_3_without_leaking_the_url(deploy, monkeypatch, capsys):
    module, calls, state = deploy
    monkeypatch.setenv("RENDER_DEPLOY_HOOK_URL", HOOK_URL)
    state["hook"] = lambda url: _Response(500)

    assert module.main() == 3

    captured = capsys.readouterr()
    assert "500" in captured.err
    assert "TOP-SECRET-KEY" not in captured.out + captured.err


def test_hook_network_error_returns_3_without_leaking_the_url(deploy, monkeypatch, capsys):
    module, calls, state = deploy
    monkeypatch.setenv("RENDER_DEPLOY_HOOK_URL", HOOK_URL)

    def boom(url):
        # httpx 예외 메시지에는 요청 URL이 그대로 들어갈 수 있다 — 키가 새면 안 된다.
        raise httpx.ConnectTimeout(f"timed out: {url}")

    state["hook"] = boom

    assert module.main() == 3

    captured = capsys.readouterr()
    assert "ConnectTimeout" in captured.err
    assert "TOP-SECRET-KEY" not in captured.out + captured.err


def test_round_guard_still_refuses_before_any_push_or_hook(deploy, monkeypatch):
    module, calls, _state = deploy
    monkeypatch.setenv("RENDER_DEPLOY_HOOK_URL", HOOK_URL)
    monkeypatch.setattr(module, "collector_state", lambda: (5, "정상"))
    monkeypatch.setattr(sys, "argv", ["safe_deploy.py", "--wait-minutes", "0"])

    assert module.main() == 1

    assert calls["push"] == []
    assert calls["hook"] == []
