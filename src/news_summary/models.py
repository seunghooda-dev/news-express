from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class PressReleaseAsset:
    url: str
    title: str = ""
    filename: str = ""
    content_type: str = ""
    asset_type: str = "file"
    is_image: bool = False
    sort_order: int = 0


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    region: str
    type: str
    enabled: bool = True
    list_url: str | None = None
    feed_url: str | None = None
    base_url: str | None = None
    selectors: dict[str, object] | None = None
    include_url_contains: list[str] = field(default_factory=list)
    exclude_title_contains: list[str] = field(default_factory=list)
    fallback_urls: list[str] = field(default_factory=list)
    verify_ssl: bool = True


@dataclass(frozen=True)
class PressRelease:
    source_id: str
    source_name: str
    region: str
    title: str
    url: str
    content: str
    published_at: str | None = None
    collected_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    validation_status: str = "검증 완료"
    validation_note: str = "원문 제목과 본문 구조를 확인했습니다."
    assets: list[PressReleaseAsset] = field(default_factory=list)


@dataclass(frozen=True)
class ArticleDraft:
    press_release_id: int
    title: str
    body: str
    review_note: str
    model: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
