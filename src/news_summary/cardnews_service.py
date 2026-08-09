# 초안 하나로 카드뉴스 세트를 만드는 통합 계층 — 사진 출처를 보장하는 책임이 여기 있다
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .cardcopy import CardCopyRequest, build_card_copy
from .cardnews import CardCopy, build_card_images
from .ops_logging import get_logger
from .storage import Store

logger = get_logger("cardnews_service")

MAX_PHOTOS_PER_SET = 4
CARD_IMAGE_SUFFIX = ".png"


class CardNewsServiceError(RuntimeError):
    """세트를 만들 수 없을 때."""


@dataclass
class CardNewsSet:
    set_id: int
    draft_id: int
    publish_date: str
    copy: CardCopy
    image_paths: list[Path]


def build_set_for_draft(
    store: Store,
    draft_id: int,
    api_key: str,
    output_root: Path,
    publish_date: str | None = None,
    downloader=None,
    copy_builder=None,
) -> CardNewsSet:
    """초안 하나를 카드뉴스 세트로 만든다.

    **진입점이 draft_id만 받는 것이 핵심이다.** 사진 바이트를 밖에서 넘길 수 없으므로
    다른 기사 사진이 섞일 수 없다. 구현 중 대시보드에서 사진을 긁어와 화재 현장
    사진에 공공예식장 문구가 붙은 카드가 나온 적이 있다 — 방송사에는 신뢰 사고다.
    """
    draft = store.get_draft(draft_id)
    if not draft:
        raise CardNewsServiceError(f"초안 #{draft_id}을 찾을 수 없습니다.")

    release_id = int(draft["press_release_id"])
    release = store.get_press_release(release_id)
    if not release:
        raise CardNewsServiceError(f"초안 #{draft_id}의 원문을 찾을 수 없습니다.")

    source_label = str(release["source_name"] or "")
    publish_date = publish_date or datetime.now().date().isoformat()
    # 기본 인자로 두면 정의 시점에 묶여 교체가 안 된다 — 호출 때 고른다.
    copy_builder = copy_builder or build_card_copy

    copy = copy_builder(
        CardCopyRequest(
            title=str(draft["title"] or ""),
            body=str(draft["body"] or ""),
            source_label=source_label,
            region=str(release["region"] or ""),
            date_label=_date_label(publish_date),
        ),
        api_key,
    )

    photos = _own_photos(store, release_id, downloader)
    images = build_card_images(copy, photos)

    set_id = store.save_card_news_set(
        draft_id=draft_id,
        press_release_id=release_id,
        publish_date=publish_date,
        cover=copy.cover,
        cards=copy.cards,
        tags=copy.tags,
        source_label=source_label,
        image_count=len(images),
    )
    paths = _write_images(output_root, publish_date, set_id, images)
    logger.info(
        "card news set built set_id=%s draft_id=%s photos=%s cards=%s",
        set_id,
        draft_id,
        len(photos),
        len(images),
    )
    return CardNewsSet(set_id=set_id, draft_id=draft_id, publish_date=publish_date, copy=copy, image_paths=paths)


def rebuild_images_from_copy(
    store: Store,
    set_id: int,
    output_root: Path,
    downloader=None,
) -> list[Path]:
    """저장된 문안으로 이미지만 다시 그린다 — 사람이 글자를 고쳤을 때 쓴다.

    AI를 다시 부르지 않으므로 손질한 문안이 덮이지 않고, 비용도 들지 않는다.
    """
    row = store.card_news_set(set_id)
    if not row:
        raise CardNewsServiceError(f"카드뉴스 #{set_id}을 찾을 수 없습니다.")
    copy = decode_cards(row)
    photos = _own_photos(store, int(row["press_release_id"]), downloader)
    images = build_card_images(copy, photos)
    return _write_images(output_root, str(row["publish_date"]), set_id, images)


def _own_photos(store: Store, release_id: int, downloader) -> list[bytes]:
    """**그 원문에 붙은 첨부만** 내려받는다. 다른 기사 사진이 섞일 여지를 두지 않는다.

    downloader는 자산 행을 통째로 받는다 — 요청 헤더(리퍼러 등)를 만들려면 URL만으로
    부족한 기관이 있다.
    """
    if downloader is None:
        return []
    photos: list[bytes] = []
    for asset in store.press_release_assets(release_id):
        if len(photos) >= MAX_PHOTOS_PER_SET:
            break
        if not asset["is_image"]:
            continue
        try:
            content = downloader(asset)
        except Exception as exc:  # noqa: BLE001 - 첨부 하나가 실패해도 세트는 나와야 한다.
            logger.info("card news photo download failed asset=%s error=%s", asset["id"], exc)
            continue
        if content:
            photos.append(content)
    return photos


def set_directory(output_root: Path, publish_date: str, set_id: int) -> Path:
    return Path(output_root) / publish_date / str(set_id)


def _write_images(output_root: Path, publish_date: str, set_id: int, images: list[bytes]) -> list[Path]:
    directory = set_directory(output_root, publish_date, set_id)
    if directory.exists():
        shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index, raw in enumerate(images, start=1):
        path = directory / f"{index}{CARD_IMAGE_SUFFIX}"
        path.write_bytes(raw)
        paths.append(path)
    return paths


def load_set_images(output_root: Path, publish_date: str, set_id: int) -> list[Path]:
    directory = set_directory(output_root, publish_date, set_id)
    if not directory.is_dir():
        return []
    return sorted(directory.glob(f"*{CARD_IMAGE_SUFFIX}"), key=lambda p: int(p.stem))


def delete_set_images(output_root: Path, publish_date: str, set_id: int) -> None:
    shutil.rmtree(set_directory(output_root, publish_date, set_id), ignore_errors=True)


def prune_old_dates(output_root: Path, keep_days: int) -> int:
    """오래된 날짜 폴더를 지운다. 하루 5세트 × 5장 × 약 300KB = 약 7.5MB/일."""
    root = Path(output_root)
    if keep_days <= 0 or not root.is_dir():
        return 0
    dates = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
    removed = 0
    for stale in dates[keep_days:]:
        shutil.rmtree(stale, ignore_errors=True)
        removed += 1
    return removed


def decode_cards(row) -> CardCopy:
    """DB 행을 다시 CardCopy로 만든다 — 재생성·수정 화면에서 쓴다."""
    return CardCopy(
        cover=str(row["cover"] or ""),
        cards=_json_list(row["cards"]),
        source_label=str(row["source_label"] or ""),
        date_label=_date_label(str(row["publish_date"] or "")),
        tags=_json_list(row["tags"]),
    )


def _json_list(value) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _date_label(publish_date: str) -> str:
    try:
        return datetime.fromisoformat(publish_date).strftime("%Y.%m.%d")
    except ValueError:
        return publish_date
