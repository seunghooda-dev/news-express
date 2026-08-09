from news_summary.card_picks import MAX_PER_SOURCE, MAX_PER_TOPIC, MIN_SHORTLIST, rank_candidates


class FakeRow(dict):
    def keys(self):  # sqlite3.Row 흉내 — 컬럼 유무를 확인하는 코드 경로를 그대로 태운다
        return super().keys()


def row(draft_id, title, source_id="damyang", source_name="담양군청", body="", assets=1):
    return FakeRow(
        id=draft_id,
        title=title,
        source_id=source_id,
        source_name=source_name,
        original_content=body,
        asset_count=assets,
    )


def test_action_news_outranks_administrative_news():
    """주민이 당장 할 일이 있는 소식이 먼저다."""
    rows = [
        row(1, "담양군수, 마을 방문 간담회 개최"),
        row(2, "담양군, 청년 월세 지원금 신청 접수 시작"),
    ]

    picks = rank_candidates(rows)

    assert picks[0].draft_id == 2
    assert any("주민 행동" in reason for reason in picks[0].reasons)


def test_safety_news_is_prioritised():
    rows = [
        row(1, "담양군, 도서관 신간 안내"),
        row(2, "담양군, 폭염 특보 발령에 따른 무더위쉼터 운영"),
    ]

    picks = rank_candidates(rows)

    assert picks[0].draft_id == 2
    assert any("안전" in reason for reason in picks[0].reasons)


def test_deadline_and_numbers_raise_score():
    with_detail = row(1, "담양군, 8월 20일까지 농기계 임대 신청 접수", body="총 120대를 지원합니다.")
    plain = row(2, "담양군, 농기계 임대사업 소개")

    picks = rank_candidates([plain, with_detail])

    assert picks[0].draft_id == 1
    assert "기한 있음" in picks[0].reasons
    assert "구체 수치" in picks[0].reasons


def test_one_source_cannot_dominate_the_shortlist():
    """지역 소식지가 한 기관 홍보물처럼 보이면 주민이 안 본다."""
    rows = [row(i, f"담양군, 지원금 신청 접수 {i}", source_id="damyang") for i in range(1, 6)]
    rows += [row(10, "화순군, 폭염 대비 지원금 신청 접수", source_id="hwasun", source_name="화순군청")]

    picks = rank_candidates(rows)

    damyang = [c for c in picks if c.source_id == "damyang"]
    assert len(damyang) <= MAX_PER_SOURCE
    assert any(c.source_id == "hwasun" for c in picks)


def test_shortlist_is_capped():
    rows = [row(i, f"담양군 소식 {i}", source_id=f"src{i}", source_name=f"기관{i}") for i in range(1, 30)]

    picks = rank_candidates(rows, limit=8)

    assert len(picks) == 8


def test_photoless_item_scores_lower_than_same_item_with_photo():
    with_photo = row(1, "담양군, 여름 캠프 참가자 모집", assets=2)
    without = row(2, "담양군, 여름 캠프 참가자 모집", source_id="hwasun", source_name="화순군청", assets=0)

    picks = rank_candidates([without, with_photo])

    assert picks[0].draft_id == 1
    assert "사진 있음" in picks[0].reasons


def test_marks_drafts_that_already_have_a_set():
    rows = [row(1, "담양군, 지원금 신청 접수"), row(2, "화순군, 무료 검진 신청", source_id="hwasun")]

    picks = rank_candidates(rows, existing_draft_ids={1})

    by_id = {c.draft_id: c for c in picks}
    assert by_id[1].has_set is True
    assert by_id[2].has_set is False


def test_one_topic_cannot_dominate_the_shortlist():
    """기관을 흩어도 주제가 같으면 소용없다.

    실측에서 폭염 주간에 8건 중 7건이 폭염 기사로 채워졌다 — 주민이 보기엔
    같은 소식 일곱 번이다.
    """
    heat = [
        row(i, f"{i}군, 폭염 대비 무더위쉼터 운영", source_id=f"src{i}", source_name=f"기관{i}")
        for i in range(1, 8)
    ]
    others = [
        row(20, "담양군, 청년 월세 지원금 신청 접수", source_id="damyang"),
        row(21, "화순군, 도서관 독서교실 모집", source_id="hwasun"),
    ]

    picks = rank_candidates(heat + others, limit=8)

    heat_picks = [c for c in picks if c.topic == "폭염"]
    # 다른 주제가 부족해 최소 인원까지는 채우지만, 폭염이 명단을 뒤덮지는 않는다.
    assert len(picks) == MIN_SHORTLIST
    assert len(heat_picks) < len(picks)
    assert {20, 21} <= {c.draft_id for c in picks}, "다른 주제는 전부 들어가야 한다"


def test_topic_cap_relaxes_when_nothing_else_is_available():
    """다른 주제가 아예 없으면 빈칸으로 두지 말고 채운다."""
    heat = [
        row(i, f"{i}군, 폭염 대비 급수 지원", source_id=f"src{i}", source_name=f"기관{i}")
        for i in range(1, 7)
    ]

    picks = rank_candidates(heat, limit=5)

    assert len(picks) == MIN_SHORTLIST


def test_empty_input_returns_empty_list():
    assert rank_candidates([]) == []
