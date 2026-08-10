# 초안 하나로 카드뉴스 세트를 만드는 통합 계층 — 사진 출처를 보장하는 책임이 여기 있다
from __future__ import annotations

import json
import re
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .asset_filters import is_display_noise_image_asset
from .cardcopy import CardCopyRequest, build_card_copy
from .cardnews import CardCopy, CardSlide, build_card_images
from .ops_logging import get_logger
from .storage import Store

logger = get_logger("cardnews_service")

# 쓸 만한 사진 하나를 찾을 때까지 **시도**할 첨부 수. 카드는 한 장이라 사진도
# 한 장만 쓰므로, 첫 성공에서 멈춘다 — 이 값은 전부 실패할 때의 상한이다.
MAX_PHOTO_ATTEMPTS = 4
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
        cards=[{"heading": s.heading, "body": s.body} for s in copy.cards],
        tags=copy.tags,
        source_label=source_label,
        image_count=len(images),
        copy_model=copy.model,
    )
    paths = _write_images(output_root, publish_date, set_id, images)
    logger.info("card news set built set_id=%s draft_id=%s cards=%s", set_id, draft_id, len(images))
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
    paths = _write_images(output_root, str(row["publish_date"]), set_id, images)
    store.set_card_news_image_count(set_id, len(images))
    return paths


def _own_photos(store: Store, release_id: int, downloader) -> Iterator[bytes]:
    """**그 원문에 붙은 첨부만** 내려받는다. 다른 기사 사진이 섞일 여지를 두지 않는다.

    downloader는 자산 행을 통째로 받는다 — 요청 헤더(리퍼러 등)를 만들려면 URL만으로
    부족한 기관이 있다.

    **지연 생성이다.** 쓸 사진을 찾으면 소비하는 쪽이 멈추고, 그러면 뒤 첨부는
    내려받지도 않는다. 전에는 4장을 다 받아 놓고 1장만 썼다.
    """
    if downloader is None:
        return
    attempts = 0
    for asset in store.press_release_assets(release_id):
        if attempts >= MAX_PHOTO_ATTEMPTS:
            break
        # 로고·배너 같은 장식 이미지는 목록 화면에서도 걸러 낸다. 카드에 넣으면
        # 사진 자리를 통째로 버린다.
        if not asset["is_image"] or is_display_noise_image_asset(asset):
            continue
        attempts += 1
        try:
            content = downloader(asset)
        except Exception as exc:  # noqa: BLE001 - 첨부 하나가 실패해도 세트는 나와야 한다.
            logger.info("card news photo download failed asset=%s error=%s", asset["id"], exc)
            continue
        if content:
            yield content


def set_directory(output_root: Path, publish_date: str, set_id: int) -> Path:
    """세트 폴더 경로. **날짜를 먼저 검증한다** — 이 값이 곧 경로 조각이 된다.

    `_write_images`가 이 경로에 `shutil.rmtree`를 부르므로 밖으로 새면 남의 폴더를
    지운다(2026-08-11 재현: `../backups`가 카드뉴스 루트를 벗어났다). 로그인과
    CSRF 뒤라 공개 취약점은 아니지만, 더 잦을 형태는 오타다 — `2026/08/09`라고
    치면 3단 중첩 폴더가 생기고 `prune_old_dates`는 그것을 하루로 세지 못해
    **영영 안 지운다.** 형식이 곧 안전이라 정규식 하나로 닫는다.
    """
    return Path(output_root) / _safe_date_segment(publish_date) / str(set_id)


_DATE_SEGMENT = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _safe_date_segment(publish_date: str) -> str:
    value = (publish_date or "").strip()
    if not _DATE_SEGMENT.match(value):
        raise CardNewsServiceError(f"카드뉴스 날짜 형식이 잘못됐습니다: {publish_date!r}")
    return value


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
    # **읽기는 관대하다.** 검증이 생기기 전에 저장된 행에 이상한 날짜가 들어 있으면
    # 관리 화면이 통째로 500이 된다 — 그림이 없는 것으로 보고 넘어간다(쓰기 쪽은
    # 그대로 거절하므로 새로 이상한 폴더가 생기지는 않는다).
    try:
        directory = set_directory(output_root, publish_date, set_id)
    except CardNewsServiceError:
        logger.warning("card news set has an unusable publish_date set_id=%s value=%r", set_id, publish_date)
        return []
    if not directory.is_dir():
        return []
    # 숫자가 아닌 png가 섞이면 int()가 터져 화면이 500이 된다 — 그런 파일은 건너뛴다.
    numbered = [path for path in directory.glob(f"*{CARD_IMAGE_SUFFIX}") if path.stem.isdigit()]
    return sorted(numbered, key=lambda path: int(path.stem))


def delete_set_images(output_root: Path, publish_date: str, set_id: int) -> None:
    try:
        directory = set_directory(output_root, publish_date, set_id)
    except CardNewsServiceError:
        # 지울 폴더를 특정할 수 없다 — 엉뚱한 곳을 지우느니 아무것도 안 한다.
        logger.warning("card news delete skipped for unusable publish_date value=%r", publish_date)
        return
    shutil.rmtree(directory, ignore_errors=True)


def prune_old_dates(output_root: Path, keep_days: int, store: Store | None = None) -> int:
    """오래된 날짜 폴더를 지운다. 한 장 카드 실측으로 하루 약 0.6MB다.

    **`store`를 주면 그 날짜의 세트 행도 함께 지운다.** 그림만 지우고 행을 남기면
    주민 화면이 제목만 있고 카드가 없는 상태로 렌더된다 — `card_news_published_dates`
    가 그 날짜를 계속 돌려주고, `load_set_images`는 조용히 빈 목록을 주며,
    템플릿의 빈 상태 안내는 세트가 0개일 때만 뜨기 때문이다(2026-08-11 재현).
    """
    root = Path(output_root)
    if keep_days <= 0 or not root.is_dir():
        return 0
    dates = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
    removed = 0
    for stale in dates[keep_days:]:
        shutil.rmtree(stale, ignore_errors=True)
        if store is not None:
            store.delete_card_news_sets_for_date(stale.name)
        removed += 1
    return removed


def decode_cards(row) -> CardCopy:
    """DB 행을 다시 CardCopy로 만든다 — 재생성·수정 화면에서 쓴다."""
    return CardCopy(
        cover=str(row["cover"] or ""),
        cards=_json_slides(row["cards"]),
        source_label=str(row["source_label"] or ""),
        date_label=_date_label(str(row["publish_date"] or "")),
        tags=_json_list(row["tags"]),
    )


def _json_slides(value) -> list[CardSlide]:
    """옛 형태(문자열 리스트)로 저장된 세트도 그대로 읽는다."""
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        # 조용히 0장이 되면 공개 화면은 멀쩡해 보이고 관리 화면만 비어 복구가 막힌다.
        logger.warning("card news cards column unreadable value=%r", str(value)[:120])
        return []
    if not isinstance(parsed, list):
        logger.warning("card news cards column is not a list type=%s", type(parsed).__name__)
        return []
    slides: list[CardSlide] = []
    for item in parsed:
        if isinstance(item, dict):
            slides.append(CardSlide(heading=str(item.get("heading") or ""), body=str(item.get("body") or "")))
        else:
            slides.append(CardSlide(heading="", body=str(item)))
    return slides


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
