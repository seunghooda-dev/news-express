from __future__ import annotations

import os
from pathlib import Path

from .models import Source


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_environment() -> None:
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        return
    load_dotenv(PROJECT_ROOT / ".env")


def env_path(name: str, default: str) -> Path:
    value = os.getenv(name, default)
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_sources(config_path: Path) -> list[Source]:
    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise RuntimeError("수집 설정을 읽으려면 PyYAML이 필요합니다. python -m pip install -e . 명령을 실행하세요.") from exc

    if not config_path.exists():
        raise FileNotFoundError(
            f"수집 설정 파일을 찾을 수 없습니다: {config_path}. "
            "config/municipalities.sample.yaml을 config/municipalities.yaml로 먼저 복사하세요."
        )

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    sources = []
    for item in raw.get("sources", []):
        sources.append(
            Source(
                id=str(item["id"]),
                name=str(item["name"]),
                region=str(item.get("region", "")),
                type=str(item["type"]),
                enabled=bool(item.get("enabled", True)),
                list_url=item.get("list_url"),
                feed_url=item.get("feed_url"),
                base_url=item.get("base_url"),
                selectors=item.get("selectors") or {},
                include_url_contains=list(item.get("include_url_contains") or []),
                exclude_title_contains=list(item.get("exclude_title_contains") or []),
                verify_ssl=bool(item.get("verify_ssl", True)),
            )
        )
    return sources
