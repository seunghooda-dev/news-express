from __future__ import annotations

import hmac
import os
from dataclasses import dataclass

from werkzeug.security import check_password_hash, generate_password_hash

from .storage import Store


ADMIN_PASSWORD_HASH_KEY = "admin_password_hash"


@dataclass(frozen=True)
class AuthConfig:
    enabled: bool
    setup_required: bool
    source: str


def auth_config(store: Store) -> AuthConfig:
    if os.getenv("NEWS_SUMMARY_AUTH_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
        return AuthConfig(enabled=False, setup_required=False, source="disabled")
    if os.getenv("NEWS_SUMMARY_ADMIN_PASSWORD_HASH") or os.getenv("NEWS_SUMMARY_ADMIN_PASSWORD"):
        return AuthConfig(enabled=True, setup_required=False, source="environment")
    if store.get_app_metadata(ADMIN_PASSWORD_HASH_KEY):
        return AuthConfig(enabled=True, setup_required=False, source="database")
    if os.getenv("NEWS_SUMMARY_AUTH_REQUIRED", "").strip().lower() in {"1", "true", "yes", "on"}:
        return AuthConfig(enabled=True, setup_required=True, source="required")
    return AuthConfig(enabled=False, setup_required=False, source="not_configured")


def verify_admin_password(store: Store, password: str) -> bool:
    configured_hash = os.getenv("NEWS_SUMMARY_ADMIN_PASSWORD_HASH") or store.get_app_metadata(ADMIN_PASSWORD_HASH_KEY)
    if configured_hash:
        return check_password_hash(configured_hash, password)

    configured_password = os.getenv("NEWS_SUMMARY_ADMIN_PASSWORD")
    if configured_password:
        return hmac.compare_digest(configured_password, password)

    return False


def set_admin_password(store: Store, password: str) -> None:
    store.set_app_metadata(ADMIN_PASSWORD_HASH_KEY, generate_password_hash(password))
