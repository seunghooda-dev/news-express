from __future__ import annotations

import hmac
import os
import re
from dataclasses import dataclass

from werkzeug.security import check_password_hash, generate_password_hash

from .storage import Store


ADMIN_PASSWORD_HASH_KEY = "admin_password_hash"
OPERATIONS_PASSWORD_HASH_KEY = "operations_password_hash"
OPERATIONS_PASSWORD_MIN_LENGTH = 4


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
    configured_hash = os.getenv("NEWS_SUMMARY_ADMIN_PASSWORD_HASH")
    if configured_hash:
        return check_password_hash(configured_hash, password)

    configured_password = os.getenv("NEWS_SUMMARY_ADMIN_PASSWORD")
    if configured_password:
        return hmac.compare_digest(configured_password, password)

    stored_hash = store.get_app_metadata(ADMIN_PASSWORD_HASH_KEY)
    if stored_hash:
        return check_password_hash(stored_hash, password)

    return False


def operations_password_configured(store: Store) -> bool:
    return bool(
        os.getenv("NEWS_SUMMARY_OPERATIONS_PASSWORD_HASH")
        or os.getenv("NEWS_SUMMARY_OPERATIONS_PASSWORD")
        or store.get_app_metadata(OPERATIONS_PASSWORD_HASH_KEY)
    )


def operations_password_source(store: Store) -> str:
    if os.getenv("NEWS_SUMMARY_OPERATIONS_PASSWORD_HASH") or os.getenv("NEWS_SUMMARY_OPERATIONS_PASSWORD"):
        return "environment"
    if store.get_app_metadata(OPERATIONS_PASSWORD_HASH_KEY):
        return "database"
    return ""


def verify_operations_password(store: Store, password: str) -> bool:
    configured_hash = os.getenv("NEWS_SUMMARY_OPERATIONS_PASSWORD_HASH")
    if configured_hash:
        return check_password_hash(configured_hash, password)

    configured_password = os.getenv("NEWS_SUMMARY_OPERATIONS_PASSWORD")
    if configured_password:
        return hmac.compare_digest(configured_password, password)

    stored_hash = store.get_app_metadata(OPERATIONS_PASSWORD_HASH_KEY)
    if stored_hash:
        return check_password_hash(stored_hash, password)

    # 운영 전용 비밀번호가 없으면 기존 동작대로 관리자 비밀번호로 확인한다.
    return verify_admin_password(store, password)


def set_operations_password(store: Store, password: str) -> None:
    store.set_app_metadata(OPERATIONS_PASSWORD_HASH_KEY, generate_password_hash(password))


def operations_password_issues(password: str) -> list[str]:
    # 운영 관리 비밀번호는 내부 도구용이라 강도 규칙을 완화한다(빈 값/너무 짧은 값만 막는다).
    value = str(password or "")
    issues: list[str] = []
    if not value:
        issues.append("비어 있음")
    elif len(value) < OPERATIONS_PASSWORD_MIN_LENGTH:
        issues.append(f"{OPERATIONS_PASSWORD_MIN_LENGTH}자 미만")
    return issues


def set_admin_password(store: Store, password: str) -> None:
    store.set_app_metadata(ADMIN_PASSWORD_HASH_KEY, generate_password_hash(password))


def admin_password_hash(password: str) -> str:
    return generate_password_hash(password)


def admin_plain_password_issues(password: str) -> list[str]:
    value = str(password or "")
    lowered = value.lower()
    issues: list[str] = []
    if len(value) < 12:
        issues.append("12자 미만")
    if not re.search(r"[A-Za-z]", value) or not re.search(r"\d", value):
        issues.append("문자/숫자 조합 부족")
    if not re.search(r"[^A-Za-z0-9]", value):
        issues.append("기호 없음")
    common_fragments = ("password", "admin", "news", "express", "kbc", "1234", "0000", "qwer")
    if any(fragment in lowered for fragment in common_fragments):
        issues.append("예측 쉬운 단어")
    return issues


def admin_password_strength_message(label: str, issues: list[str]) -> str:
    return (
        f"{label}가 상용 운영 기준에 약합니다: "
        + ", ".join(issues)
        + ". 12자 이상, 영문/숫자/기호를 섞은 예측 어려운 값으로 설정하세요."
    )
