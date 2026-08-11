"""수집한 기사를 남길지 버릴지 정하는 자리 — 여기서 조용히 버리면 아무도 모른다.

`filter_releases_by_retention`과 `press_release_published_date`는 테스트가
0건이었다(2026-08-12 실측). 이 둘이 잘못되면 **수집은 성공했다고 보고되는데
기사만 사라진다** — 소스 상태 화면도 초록이고 로그도 조용하다.

특히 중요한 것은 **날짜를 못 읽었을 때의 방향**이다. 지자체 27곳 중 19곳이
날짜 선택자 없이 본문 전체 스캔 폴백을 타므로(2026-08-12 실측) 파싱이 실패할
수 있는데, 지금은 실패하면 **남긴다**. 이 방향이 뒤집히면 읽지 못한 기사가
통째로 사라진다.
"""

from datetime import date

import pytest

from news_summary.models import PressRelease
from news_summary.service import filter_releases_by_retention, press_release_published_date

CUTOFF = date(2026, 8, 1)


def release(published_at: str | None) -> PressRelease:
    return PressRelease(
        source_id="damyang-county",
        source_name="담양군청 보도자료",
        region="전남 담양",
        title="담양군 소식",
        url=f"https://damyang.go.kr/{published_at}",
        content="본문입니다.",
        published_at=published_at,
        assets=[],
    )


# --- 남길지 버릴지 ------------------------------------------------------------


def test_old_release_is_dropped_and_counted():
    retained, skipped = filter_releases_by_retention([release("2026-07-31")], CUTOFF)

    assert retained == []
    assert skipped == 1, "버린 건수를 세지 않으면 운영 화면에 이유가 안 남는다"


def test_release_on_the_cutoff_day_is_kept():
    """경계일은 남긴다 — 하루 차이로 그날 기사가 통째로 사라지면 안 된다."""
    retained, skipped = filter_releases_by_retention([release("2026-08-01")], CUTOFF)

    assert [item.published_at for item in retained] == ["2026-08-01"]
    assert skipped == 0


@pytest.mark.parametrize(
    "value,why",
    [
        ("2026-13-45", "존재하지 않는 달·날"),
        ("2026-02-30", "2월 30일"),
        ("", "빈 값"),
        (None, "값 없음"),
        ("어제", "날짜가 아닌 글자"),
    ],
)
def test_unreadable_date_is_kept_not_silently_dropped(value, why):
    """읽지 못한 날짜는 **남긴다.** 이 방향이 이 파일의 핵심이다.

    버리는 쪽으로 뒤집히면 파싱이 어긋난 소스의 기사가 통째로 사라지는데,
    수집은 성공으로 보고되므로 아무도 모른다.
    """
    retained, skipped = filter_releases_by_retention([release(value)], CUTOFF)

    assert len(retained) == 1, f"읽지 못한 날짜({why})를 조용히 버렸다"
    assert skipped == 0


def test_mixed_batch_keeps_only_what_should_stay():
    releases = [release("2026-08-05"), release("2026-06-01"), release("2026-13-45")]

    retained, skipped = filter_releases_by_retention(releases, CUTOFF)

    assert [item.published_at for item in retained] == ["2026-08-05", "2026-13-45"]
    assert skipped == 1


# --- 날짜 읽기 ----------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-08-11", date(2026, 8, 11)),
        ("2026.08.11", date(2026, 8, 11)),  # 지자체 게시판이 흔히 쓰는 표기
        ("2026/8/9", date(2026, 8, 9)),
        ("등록일 2026-08-11 14:30", date(2026, 8, 11)),
        ("2026-08-11 14:30:59", date(2026, 8, 11)),
    ],
)
def test_reads_the_shapes_real_boards_send(value, expected):
    assert press_release_published_date(release(value)) == expected


@pytest.mark.parametrize("value", ["2026-13-45", "2026-02-30", "문의 062-613-2000", "조회수 2026", ""])
def test_returns_none_instead_of_a_made_up_date(value):
    """읽을 수 없으면 None이어야 한다 — 아무 날짜나 지어내면 보관 판정이 그걸 믿는다."""
    assert press_release_published_date(release(value)) is None
