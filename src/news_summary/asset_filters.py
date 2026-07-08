from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote


IMAGE_DECORATIVE_TOKENS = (
    "logo",
    "icon",
    "ico_",
    "banner",
    "main_visual",
    "visual_wrap",
    "visual-wrap",
    "visual_area",
    "visual-area",
    "visualbanner",
    "visual-banner",
    "popup",
    "quick",
    "gnb",
    "lnb",
    "snb",
    "nav",
    "menu",
    "breadcrumb",
    "header",
    "footer",
    "search",
    "share",
    "print",
    "satisfaction",
    "symbol",
    "emblem",
    "mascot",
    "sns",
    "facebook",
    "instagram",
    "youtube",
    "blog",
    "favicon",
    "spacer",
)


def image_asset_looks_decorative(*values: object) -> bool:
    text = unquote(" ".join(str(value or "") for value in values)).lower()
    return any(_decorative_token_matches(text, token) for token in IMAGE_DECORATIVE_TOKENS)


def is_display_noise_image_asset(asset: Any) -> bool:
    if not _row_value(asset, "is_image"):
        return False
    return image_asset_looks_decorative(
        _row_value(asset, "url"),
        _row_value(asset, "title"),
        _row_value(asset, "filename"),
        _row_value(asset, "content_type"),
        _row_value(asset, "asset_type"),
    )


def is_cleanup_noise_image_asset(asset: Any) -> bool:
    if not _row_value(asset, "is_image"):
        return False
    return image_asset_looks_decorative(
        _row_value(asset, "url"),
        _row_value(asset, "filename"),
    )


def _row_value(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, None)


def _decorative_token_matches(text: str, token: str) -> bool:
    token = token.lower()
    if token.endswith("_"):
        return token in text
    normalized_text = re.sub(r"[^0-9a-z가-힣]+", " ", text)
    normalized_token = re.sub(r"[^0-9a-z가-힣]+", " ", token).strip()
    if not normalized_token:
        return False
    if " " in normalized_token:
        return normalized_token in normalized_text
    return bool(re.search(rf"(?<![0-9a-z가-힣]){re.escape(normalized_token)}(?![0-9a-z가-힣])", normalized_text))
