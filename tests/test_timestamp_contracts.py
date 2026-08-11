"""같은 이름의 `_parse_datetime` 두 개가 naive 값을 정반대로 읽는다.

`scheduler`는 UTC로, `web`은 KST로 본다 — 9시간 차이다. 각자 자기 입력에
대해서는 맞다(scheduler는 내부 `_now()`가 쓴 aware UTC만 받고, web은 사이트가
준 naive 한국 시각도 받는다). **둘 다 테스트가 0건이었다**(2026-08-12 실측).

이 파일의 목적은 통합을 막는 것이 아니라, **이름이 같다는 이유로 무심코 합치면
반드시 깨지게** 만드는 것이다. 2026-08-12에 컨테이너 UTC로 인한 시간대 결함을
세 곳 고쳤는데, 셋 다 이런 암묵적 가정에서 나왔다.
"""

from datetime import datetime, timedelta, timezone

import pytest

from news_summary.scheduler import _parse_datetime as parse_internal
from news_summary.web import _parse_datetime as parse_external

KST = timezone(timedelta(hours=9))


def test_internal_parser_treats_naive_as_utc():
    """스케줄러가 읽는 값은 전부 내부가 `_now()`로 쓴 UTC다."""
    parsed = parse_internal("2026-08-11T17:14:13")

    assert parsed is not None
    assert parsed.utcoffset() == timedelta(0)
    assert parsed.hour == 17


def test_internal_parser_keeps_an_aware_value_as_is():
    parsed = parse_internal("2026-08-11T17:14:13+00:00")

    assert parsed == datetime(2026, 8, 11, 17, 14, 13, tzinfo=timezone.utc)


def test_external_parser_treats_naive_as_korean_time():
    """사이트가 준 게시일에는 시간대가 없다. 그것은 한국 시각이다."""
    parsed = parse_external("2026-08-11T14:30:00")

    assert parsed is not None
    assert parsed.utcoffset() == timedelta(hours=9)
    assert parsed.hour == 14


def test_external_parser_converts_an_aware_value_to_korean_time():
    parsed = parse_external("2026-08-11T17:14:13+00:00")

    assert parsed == datetime(2026, 8, 12, 2, 14, 13, tzinfo=KST)


@pytest.mark.parametrize(
    "value,expected_date",
    [
        ("Mon, 11 Aug 2026 14:30:00 +0900", (2026, 8, 11)),  # RSS가 주는 모양
        ("2026.08.11 14:30", (2026, 8, 11)),  # 지자체 게시판이 흔히 쓰는 모양
        ("2026-08-11", (2026, 8, 11)),
    ],
)
def test_external_parser_accepts_the_shapes_real_sites_send(value, expected_date):
    parsed = parse_external(value)

    assert parsed is not None
    assert (parsed.year, parsed.month, parsed.day) == expected_date


@pytest.mark.parametrize("value", ["", None, "어제", "not a date"])
def test_both_parsers_return_none_on_junk(value):
    assert parse_internal(value) is None
    assert parse_external(value) is None


def test_the_two_parsers_disagree_on_purpose():
    """합치면 한쪽이 9시간 틀린다 — 이 시험이 그 순간 깨지라고 있다.

    합치려면 두 모듈의 시간대 계약을 하나로 정하고, 그 결정을 문서에 남긴 뒤
    이 시험을 함께 고쳐야 한다. 조용히 통과하게 두면 안 된다.
    """
    naive = "2026-08-11T17:00:00"

    internal = parse_internal(naive)
    external = parse_external(naive)

    assert internal is not None and external is not None
    assert internal.utcoffset() != external.utcoffset()
    assert external.astimezone(timezone.utc) - internal.astimezone(timezone.utc) == timedelta(
        hours=-9
    )
