"""무인으로 도는 삭제 작업 — 여기가 틀리면 되돌릴 수 없다.

`_prune_old_card_news_once`는 유지보수 작업 13개 중 **테스트가 하나도 없는
유일한 항목**이었다(2026-08-12 실측). 그림과 세트 행을 함께 지우는 경로이고
사람이 보지 않는 시간에 돈다. `prune_old_dates` 자체는 덮여 있었으므로
여기서는 스케줄러가 그것을 **어떤 값으로 부르는지**만 고정한다.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from news_summary.scheduler import (
    CARD_NEWS_KEEP_DAYS_ENV,
    OPERATION_EVENT_RETENTION_DAYS_ENV,
    AutoCollector,
)
from news_summary.storage import Store


def make_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / f"prune_{uuid4().hex}.sqlite")
    store.init_db()
    return store


@pytest.fixture
def card_root(tmp_path, monkeypatch):
    root = tmp_path / "cardnews"
    for day in ("2020-01-01", "2020-01-02", "2020-01-03"):
        (root / day).mkdir(parents=True)
        (root / day / "1.png").write_bytes(b"fake")
    monkeypatch.setenv("NEWS_SUMMARY_CARDNEWS_DIR", str(root))
    return root


def test_keep_days_zero_deletes_nothing(tmp_path, monkeypatch, card_root):
    """0은 '전부 지운다'가 아니라 '끈다'여야 한다 — 뒤집히면 되돌릴 수 없다."""
    monkeypatch.setenv(CARD_NEWS_KEEP_DAYS_ENV, "0")
    collector = AutoCollector(make_store(tmp_path), Path("unused.yaml"), enabled=True)

    assert collector._prune_old_card_news_once() is None
    assert sorted(p.name for p in card_root.iterdir()) == [
        "2020-01-01",
        "2020-01-02",
        "2020-01-03",
    ]


def test_keep_days_means_newest_n_folders_not_age(tmp_path, monkeypatch, card_root):
    """`keep_days`는 나이가 아니라 **최신 날짜 폴더 N개**를 남긴다는 뜻이다.

    이름이 KEEP_DAYS라 나이 기준으로 읽기 쉬운데, 실제로는 폴더를 이름 역순으로
    정렬해 앞의 N개만 남긴다. 여기 세 폴더는 전부 2020년이라 나이 기준이면
    하나도 안 남아야 하지만, 실제로는 가장 최신 하나가 남는다.
    """
    monkeypatch.setenv(CARD_NEWS_KEEP_DAYS_ENV, "1")
    collector = AutoCollector(make_store(tmp_path), Path("unused.yaml"), enabled=True)

    message = collector._prune_old_card_news_once()

    assert message == "오래된 카드뉴스 2일분 정리"
    assert [p.name for p in card_root.iterdir()] == ["2020-01-03"]


def test_nothing_to_prune_stays_silent(tmp_path, monkeypatch):
    """지운 게 없으면 조용해야 한다 — 매시 '0건 정리' 줄이 쌓이면 신호가 죽는다."""
    monkeypatch.setenv(CARD_NEWS_KEEP_DAYS_ENV, "30")
    monkeypatch.setenv("NEWS_SUMMARY_CARDNEWS_DIR", str(tmp_path / "없는폴더"))
    collector = AutoCollector(make_store(tmp_path), Path("unused.yaml"), enabled=True)

    assert collector._prune_old_card_news_once() is None


def test_pruning_reaches_the_store_rows_too(tmp_path, monkeypatch, card_root):
    """그림만 지우면 주민 화면에 제목만 있고 카드가 없는 기사가 남는다(2026-08-11 재현).

    스케줄러가 `store`를 넘기지 않으면 그 상태가 다시 만들어진다.
    """
    store = make_store(tmp_path)
    set_id = store.save_card_news_set(
        draft_id=1,
        press_release_id=1,
        publish_date="2020-01-01",
        cover="표지",
        cards=[{"heading": "소제목", "body": "본문입니다."}],
        tags=[],
        source_label="담양군청 보도자료",
        image_count=1,
    )
    monkeypatch.setenv(CARD_NEWS_KEEP_DAYS_ENV, "1")
    collector = AutoCollector(store, Path("unused.yaml"), enabled=True)

    collector._prune_old_card_news_once()

    assert store.card_news_set(set_id) is None, "그림은 지웠는데 세트 행이 남았다"


def test_default_keeps_thirty_days_when_env_is_unset(tmp_path, monkeypatch):
    """기본값 30을 아무도 안 지키고 있었다(2026-08-12 변이 검사에서 적발).

    env를 명시하는 시험만 있으면 기본값을 1로 바꿔도 아무 시험이 안 깨진다 —
    그러면 프로덕션 카드뉴스가 조용히 하루치만 남는다.
    """
    monkeypatch.delenv(CARD_NEWS_KEEP_DAYS_ENV, raising=False)
    root = tmp_path / "cardnews"
    for index in range(31):
        (root / f"2020-01-{index + 1:02d}").mkdir(parents=True)
    monkeypatch.setenv("NEWS_SUMMARY_CARDNEWS_DIR", str(root))
    collector = AutoCollector(make_store(tmp_path), Path("unused.yaml"), enabled=True)

    message = collector._prune_old_card_news_once()

    assert message == "오래된 카드뉴스 1일분 정리", f"기본 보관값이 30이 아니다 — {message}"
    assert len(list(root.iterdir())) == 30


# --- 운영 변경 이력 정리 ------------------------------------------------------
#
# 저장소의 `prune_operation_events`는 이미 덮여 있다(test_storage.py). 여기서는
# 스케줄러가 **어떤 기준선으로 그것을 부르는지**만 고정한다 — 카드뉴스에서
# 기본 보관값을 아무도 안 지키고 있던 것과 같은 자리다.


def _plant_event(store: Store, days_ago: int, now: datetime) -> None:
    store.record_operation_event(
        "test_event",
        detail=f"{days_ago}일 전",
        created_at=(now - timedelta(days=days_ago)).isoformat(),
    )


def test_operation_event_default_retention_is_180_days(tmp_path, monkeypatch):
    """기본값 180을 아무 시험도 지키지 않았다. 여기가 짧아지면 운영 이력이 조용히 사라진다."""
    monkeypatch.delenv(OPERATION_EVENT_RETENTION_DAYS_ENV, raising=False)
    store = make_store(tmp_path)
    now = datetime.now(timezone.utc)
    _plant_event(store, 181, now)
    _plant_event(store, 179, now)
    collector = AutoCollector(store, Path("unused.yaml"), enabled=True)

    message = collector._prune_old_operation_events_once(now)

    assert message == "운영 변경 이력 1건 자동 정리", f"보관 경계가 180일이 아니다 — {message}"
    remaining = [str(row["detail"]) for row in store.operation_events(limit=10)]
    assert remaining == ["179일 전"], f"경계 밖 이력을 지웠거나 안 지웠다 — {remaining}"


def test_operation_event_retention_env_overrides_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv(OPERATION_EVENT_RETENTION_DAYS_ENV, "30")
    store = make_store(tmp_path)
    now = datetime.now(timezone.utc)
    _plant_event(store, 31, now)
    _plant_event(store, 29, now)
    collector = AutoCollector(store, Path("unused.yaml"), enabled=True)

    assert collector._prune_old_operation_events_once(now) == "운영 변경 이력 1건 자동 정리"


def test_operation_event_pruning_stays_silent_when_nothing_is_old(tmp_path, monkeypatch):
    monkeypatch.delenv(OPERATION_EVENT_RETENTION_DAYS_ENV, raising=False)
    store = make_store(tmp_path)
    now = datetime.now(timezone.utc)
    _plant_event(store, 1, now)
    collector = AutoCollector(store, Path("unused.yaml"), enabled=True)

    assert collector._prune_old_operation_events_once(now) is None
