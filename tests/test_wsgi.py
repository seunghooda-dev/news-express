from __future__ import annotations

import importlib
import sys


def test_wsgi_app_exports_health_checked_flask_app(monkeypatch, tmp_path):
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(tmp_path / "wsgi.sqlite"))
    monkeypatch.setenv("NEWS_SUMMARY_AUTO_COLLECT", "0")
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_REQUIRED", "0")
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_DISABLED", "1")

    sys.modules.pop("news_summary.wsgi", None)
    module = importlib.import_module("news_summary.wsgi")

    response = module.app.test_client().get("/healthz")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["database"] == "ok"
    assert payload["ok"] is True
    assert payload["auto_collector"] == "disabled"
    assert payload["last_auto_finished_at"] is None
    assert module.app.config["AUTO_COLLECTOR"].snapshot().enabled is False
