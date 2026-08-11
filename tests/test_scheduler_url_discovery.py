"""지자체가 사이트를 개편하면 수집이 죽는다 — 그때 도는 자가 진단 경로.

`scheduler.py` 1135~1240은 커버리지 0이었다(2026-08-12 실측). 이 프로젝트에서
가장 자주 실제로 벌어지는 고장이 "사이트 구조 변경"인데, 그걸 복구하라고 만든
경로가 한 줄도 검사되지 않고 있었다.
"""

from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from news_summary import scheduler
from news_summary.models import Source
from news_summary.scheduler import (
    _consecutive_failure_counts,
    _discover_candidate_urls,
    _same_host,
    _source_home_urls,
    _url_discovery_candidates,
)
from news_summary.storage import Store


def make_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / f"discovery_{uuid4().hex}.sqlite")
    store.init_db()
    return store


def make_source(source_id: str = "damyang-county", **overrides) -> Source:
    fields = {
        "id": source_id,
        "name": "담양군청 보도자료",
        "region": "전남 담양",
        "type": "html",
        "base_url": "https://www.damyang.go.kr",
        "list_url": "https://www.damyang.go.kr/board/list",
    }
    fields.update(overrides)
    return Source(**fields)


def fail(store: Store, source_id: str, times: int, stage: str = "사이트 구조 변경") -> None:
    for index in range(times):
        store.record_source_collection_status(
            source_id, "담양군청", "failed", f"실패 {index}", failure_stage=stage
        )


# --- 후보 선정 -------------------------------------------------------------


def test_needs_three_consecutive_failures_before_discovery(tmp_path, monkeypatch):
    """한 번 삐끗한 사이트마다 홈페이지를 긁으면 유지보수가 그 자체로 부하다."""
    store = make_store(tmp_path)
    monkeypatch.setattr(
        scheduler, "_collectable_source_map", lambda path: {"damyang-county": make_source()}
    )
    fail(store, "damyang-county", 2)

    assert _url_discovery_candidates(store, Path("config/x.yaml"), limit=5) == []

    fail(store, "damyang-county", 1)
    picked = _url_discovery_candidates(store, Path("config/x.yaml"), limit=5)
    assert [source.id for source in picked] == ["damyang-county"]


def test_transient_failures_do_not_trigger_discovery(tmp_path, monkeypatch):
    """접속 지연 같은 일시 장애는 주소를 다시 찾을 일이 아니다."""
    store = make_store(tmp_path)
    monkeypatch.setattr(
        scheduler, "_collectable_source_map", lambda path: {"damyang-county": make_source()}
    )
    fail(store, "damyang-county", 5, stage="응답 시간 초과")

    assert _url_discovery_candidates(store, Path("config/x.yaml"), limit=5) == []


def test_recent_success_clears_the_streak(tmp_path, monkeypatch):
    """실패 뒤 한 번이라도 성공했으면 연속 실패가 아니다 — 끊긴 줄을 세면 안 된다."""
    store = make_store(tmp_path)
    monkeypatch.setattr(
        scheduler, "_collectable_source_map", lambda path: {"damyang-county": make_source()}
    )
    fail(store, "damyang-county", 4)
    store.record_source_collection_status("damyang-county", "담양군청", "ok", "복구")

    assert _url_discovery_candidates(store, Path("config/x.yaml"), limit=5) == []


def test_disabled_or_excluded_source_is_never_probed(tmp_path, monkeypatch):
    """제외된 소스를 유지보수가 몰래 다시 긁으면 제외의 의미가 없다(강진 건)."""
    store = make_store(tmp_path)
    monkeypatch.setattr(scheduler, "_collectable_source_map", lambda path: {})
    fail(store, "gangjin-county", 5)

    assert _url_discovery_candidates(store, Path("config/x.yaml"), limit=5) == []


def test_consecutive_failure_counts_stops_at_first_success():
    """`recent_source_run_statuses`의 정렬(source_id, id DESC)에 정확성이 걸려 있다.

    정렬이 바뀌면 이 함수는 **떨어져 있는 실패까지 연속으로 세면서** 조용히
    틀린 값을 낸다. 그 결합을 여기서 붙잡는다.
    """
    rows = [
        {"source_id": "a", "status": "failed"},
        {"source_id": "a", "status": "failed"},
        {"source_id": "a", "status": "ok"},
        {"source_id": "a", "status": "failed"},  # 성공 이전의 실패 — 세면 안 된다
        {"source_id": "b", "status": "ok"},
        {"source_id": "b", "status": "failed"},
    ]

    counts = _consecutive_failure_counts(rows)

    assert counts["a"] == 2, "성공으로 끊긴 뒤의 옛 실패까지 셌다"
    assert "b" not in counts, "가장 최근이 성공인데 연속 실패로 셌다"


# --- 주소 수집 -------------------------------------------------------------


def test_home_urls_include_host_root_and_dedupe():
    source = make_source(
        base_url="https://www.damyang.go.kr/board/list",
        list_url="https://www.damyang.go.kr/board/list",
        feed_url=None,
    )

    homes = _source_home_urls(source)

    assert homes == ["https://www.damyang.go.kr/board/list", "https://www.damyang.go.kr/"]


def test_home_urls_drop_unusable_values():
    source = make_source(base_url="", list_url="게시판", feed_url=None)

    assert _source_home_urls(source) == []


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.damyang.go.kr/a", True),
        ("https://evil.example.com/a", False),
        ("/board/list", False),
        ("javascript:alert(1)", False),
    ],
)
def test_same_host_rejects_offsite_and_non_http(url, expected):
    assert _same_host(url, "https://www.damyang.go.kr/") is expected


class FakeResponse:
    def __init__(self, url: str, text: str):
        self.url = url
        self.text = text

    def raise_for_status(self) -> None:
        return None


class FakeClient:
    """`_discover_candidate_urls`가 클라이언트를 함수 안에서 만들어 주입할 수 없다."""

    pages: dict[str, str] = {}
    requested: list[str] = []

    def __init__(self, *args, **kwargs):
        self.verify = kwargs.get("verify")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url: str):
        FakeClient.requested.append(url)
        if url not in FakeClient.pages:
            raise httpx.ConnectError("not found")
        return FakeResponse(url, FakeClient.pages[url])


@pytest.fixture
def fake_http(monkeypatch):
    FakeClient.pages = {}
    FakeClient.requested = []
    monkeypatch.setattr(httpx, "Client", FakeClient)
    return FakeClient


def test_discovery_keeps_only_same_host_press_release_links(fake_http):
    fake_http.pages["https://www.damyang.go.kr/"] = """
        <a href="/board/press">보도자료</a>
        <a href="/board/press">보도자료</a>
        <a href="https://blog.example.com/보도자료">외부 보도자료</a>
        <a href="/board/notice">일반 공지</a>
        <a href="/news/list">군정소식</a>
    """
    source = make_source(base_url="https://www.damyang.go.kr/", list_url=None, feed_url=None)

    urls = _discover_candidate_urls(source, limit=5)

    assert urls == [
        "https://www.damyang.go.kr/board/press",
        "https://www.damyang.go.kr/news/list",
    ], "같은 호스트의 보도자료 링크만 중복 없이 남아야 한다"


def test_discovery_survives_a_dead_homepage(fake_http):
    """홈페이지 하나가 죽어도 나머지 후보 주소는 계속 본다 — 진단이 멈추면 안 된다."""
    fake_http.pages["https://www.damyang.go.kr/"] = '<a href="/board/press">보도자료</a>'
    source = make_source(
        base_url="https://down.damyang.go.kr/gone",
        list_url="https://www.damyang.go.kr/",
        feed_url=None,
    )

    urls = _discover_candidate_urls(source, limit=5)

    assert urls == ["https://www.damyang.go.kr/board/press"]
    assert "https://down.damyang.go.kr/gone" in fake_http.requested, "죽은 주소를 시도조차 안 했다"


def test_discovery_respects_the_limit(fake_http):
    links = "".join(f'<a href="/board/press{i}">보도자료 {i}</a>' for i in range(10))
    fake_http.pages["https://www.damyang.go.kr/"] = links
    source = make_source(base_url="https://www.damyang.go.kr/", list_url=None, feed_url=None)

    assert len(_discover_candidate_urls(source, limit=3)) == 3


# --- 유지보수 루프와 운영 화면의 이음매 -------------------------------------


def test_discovery_can_be_turned_off_by_limit_zero(tmp_path, monkeypatch, fake_http):
    """끌 수 있어야 한다 — 진단이 부담이 되는 날 운영자가 막을 수단이다."""
    from news_summary.scheduler import AUTO_URL_DISCOVERY_LIMIT_ENV, AutoCollector

    store = make_store(tmp_path)
    monkeypatch.setenv(AUTO_URL_DISCOVERY_LIMIT_ENV, "0")
    monkeypatch.setattr(
        scheduler, "_collectable_source_map", lambda path: {"damyang-county": make_source()}
    )
    fail(store, "damyang-county", 5)

    collector = AutoCollector(store, Path("unused.yaml"), enabled=True)

    assert collector._discover_fallback_url_candidates_once() == []
    assert fake_http.requested == [], "꺼 뒀는데 홈페이지를 긁었다"


def test_discovery_snapshot_round_trips_to_the_operations_screen(tmp_path, monkeypatch, fake_http):
    """스케줄러가 쓰고 운영 화면이 읽는다 — 모양이 어긋나면 화면이 조용히 빈다."""
    from news_summary.scheduler import AUTO_URL_DISCOVERY_LIMIT_ENV, AutoCollector
    from news_summary.web import _url_discovery_report

    store = make_store(tmp_path)
    monkeypatch.setenv(AUTO_URL_DISCOVERY_LIMIT_ENV, "3")
    monkeypatch.setattr(
        scheduler, "_collectable_source_map", lambda path: {"damyang-county": make_source()}
    )
    fail(store, "damyang-county", 3)
    fake_http.pages["https://www.damyang.go.kr/"] = (
        '<a href="/board/press">보도자료</a><a href="/news/list">군정소식</a>'
    )

    collector = AutoCollector(store, Path("unused.yaml"), enabled=True)
    messages = collector._discover_fallback_url_candidates_once()

    assert messages == ["담양군청 보도자료 대체 URL 후보 2개 발견"]

    report = _url_discovery_report(store)
    assert report["updated_at"], "언제 찾았는지가 화면에 안 간다"
    assert len(report["discoveries"]) == 1
    found = report["discoveries"][0]
    assert found["source_id"] == "damyang-county"
    assert found["url_count"] == 2
    assert found["urls"] == [
        "https://www.damyang.go.kr/board/press",
        "https://www.damyang.go.kr/news/list",
    ]


def test_discovery_records_nothing_when_no_links_are_found(tmp_path, monkeypatch, fake_http):
    """못 찾았으면 빈 항목을 남기지 않는다 — 화면에 빈 줄이 쌓이면 신호가 죽는다."""
    from news_summary.scheduler import AUTO_URL_DISCOVERY_LIMIT_ENV, AutoCollector
    from news_summary.web import _url_discovery_report

    store = make_store(tmp_path)
    monkeypatch.setenv(AUTO_URL_DISCOVERY_LIMIT_ENV, "3")
    monkeypatch.setattr(
        scheduler, "_collectable_source_map", lambda path: {"damyang-county": make_source()}
    )
    fail(store, "damyang-county", 3)
    fake_http.pages["https://www.damyang.go.kr/"] = "<a href='/about'>군수 인사말</a>"

    collector = AutoCollector(store, Path("unused.yaml"), enabled=True)

    assert collector._discover_fallback_url_candidates_once() == []
    assert _url_discovery_report(store)["discoveries"] == []


def test_discovery_limit_env_caps_how_many_sources_are_probed(tmp_path, monkeypatch, fake_http):
    """상한이 실제로 후보 수를 자르는지 — 27곳이 한꺼번에 깨진 날 홈페이지를 27번 긁으면 안 된다.

    off 스위치 시험만으로는 이 경로에 이빨이 없다. `limit <= 0` 가드를 지워도
    `LIMIT 0` 쿼리가 우연히 같은 결과를 내기 때문이다(2026-08-12 변이 검사에서
    적발). 상한이 0이 아닌 값에서 동작하는지를 따로 건다.
    """
    from news_summary.scheduler import AUTO_URL_DISCOVERY_LIMIT_ENV, AutoCollector

    store = make_store(tmp_path)
    sources = {f"town-{i}": make_source(f"town-{i}") for i in range(3)}
    monkeypatch.setenv(AUTO_URL_DISCOVERY_LIMIT_ENV, "1")
    monkeypatch.setattr(scheduler, "_collectable_source_map", lambda path: sources)
    for source_id in sources:
        fail(store, source_id, 3)
    fake_http.pages["https://www.damyang.go.kr/"] = '<a href="/board/press">보도자료</a>'

    collector = AutoCollector(store, Path("unused.yaml"), enabled=True)
    messages = collector._discover_fallback_url_candidates_once()

    assert len(messages) == 1, f"상한 1인데 {len(messages)}곳을 긁었다"
