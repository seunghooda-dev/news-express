"""승인 기사 내보내기 — 운영자 일과의 마지막 단계인데 테스트가 0건이었다."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from news_summary import exporter
from news_summary.exporter import export_approved
from news_summary.models import ArticleDraft, PressRelease
from news_summary.storage import Store


def _store_with_approved_draft(tmp_path: Path, count: int = 2) -> Store:
    store = Store(tmp_path / f"export_{uuid4().hex}.sqlite")
    store.init_db()
    for index in range(count):
        release_id = store.add_press_release(
            PressRelease(
                source_id="damyang-county",
                source_name="담양군청 보도자료",
                region="전남 담양",
                title=f"담양군 소식 {index}",
                url=f"https://example.com/{uuid4().hex}",
                content="원문입니다.",
                published_at="2026-08-11",
                assets=[],
            )
        )
        draft_id = store.add_article_draft(
            ArticleDraft(
                press_release_id=release_id,
                title=f"담양군 소식 {index}",
                body=f"본문 {index}입니다. 담양군이 대책을 발표했습니다.",
                review_note=f"검수 메모 {index}",
                model="gemini-3.5-flash",
            )
        )
        store.set_draft_status(draft_id, "approved")
    return store


def test_export_writes_both_files_and_marks_rows_exported(tmp_path):
    store = _store_with_approved_draft(tmp_path)

    markdown_path, csv_path, count = export_approved(store, tmp_path / "out")

    assert count == 2
    assert markdown_path.exists() and csv_path.exists()
    body = markdown_path.read_text(encoding="utf-8")
    assert "담양군 소식 0" in body and "검수 메모 1" in body
    # 두 번째 호출은 이미 내보낸 것을 다시 담지 않아야 한다.
    _, _, again = export_approved(store, tmp_path / "out")
    assert again == 0, "이미 내보낸 기사를 다시 내보냈다"


def test_export_csv_opens_in_excel_without_mojibake(tmp_path):
    """한글 CSV는 BOM이 없으면 엑셀에서 깨진다 — 실제로 겪는 문제다."""
    store = _store_with_approved_draft(tmp_path, count=1)

    _, csv_path, _ = export_approved(store, tmp_path / "out")

    raw = csv_path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "UTF-8 BOM이 없다"
    assert "담양군 소식 0" in raw.decode("utf-8-sig")


def test_export_filename_uses_korean_time_not_container_time(tmp_path, monkeypatch):
    """컨테이너는 UTC다. naive `now()`를 쓰면 파일명이 9시간 어긋난다.

    한국 00~09시에 내보내면 **어제 날짜** 파일이 나온다. 개발 머신이 KST라
    그냥 비교하면 헛도므로, 모듈의 LOCAL_TZ를 바꿔치기해 스탬프가 그것을
    따라가는지 본다 — 어느 시간대에서 돌려도 판별된다.
    """
    store = _store_with_approved_draft(tmp_path, count=1)
    far_tz = timezone(timedelta(hours=-11))
    monkeypatch.setattr(exporter, "LOCAL_TZ", far_tz)

    markdown_path, _, _ = export_approved(store, tmp_path / "out")

    stamp = markdown_path.stem.removeprefix("승인기사_")[:13]  # YYYYMMDD_HHMM
    expected = datetime.now(far_tz).strftime("%Y%m%d_%H%M")
    assert stamp.startswith(expected), (
        f"파일명이 LOCAL_TZ를 안 따른다: {stamp!r} (기대 {expected!r})"
    )
