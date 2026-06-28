from __future__ import annotations

from news_summary.settings import env_database
from news_summary.storage import Store


def test_env_database_prefers_database_url(monkeypatch):
    monkeypatch.setenv("NEWS_SUMMARY_DB", "data/news_summary.sqlite")
    monkeypatch.setenv("DATABASE_URL", "postgresql://kbcnews:newsexpress1@example.com:5432/kbcnews?sslmode=require")

    assert env_database() == "postgresql://kbcnews:newsexpress1@example.com:5432/kbcnews?sslmode=require"


def test_env_database_resolves_sqlite_path(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NEWS_SUMMARY_DATABASE_URL", raising=False)
    monkeypatch.setenv("NEWS_SUMMARY_DB", "data/news_summary.sqlite")

    assert env_database().endswith("data\\news_summary.sqlite") or env_database().endswith("data/news_summary.sqlite")


def test_store_redacts_postgres_password_in_display_location():
    store = Store("postgresql://kbcnews:newsexpress1@example.com:5432/kbcnews?sslmode=require")

    assert store.display_location == "postgresql://kbcnews:***@example.com:5432/kbcnews?sslmode=require"
