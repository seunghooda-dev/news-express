from bs4 import BeautifulSoup
import httpx

from news_summary.collectors import (
    _canonical_url,
    _extract_labeled_date,
    _extract_detail_content,
    _normalize_published_at,
    _validated_release,
    collect_html_board,
    collect_json_board,
)
from news_summary.models import Source


def test_extract_detail_content_keeps_form_wrapped_board_body():
    soup = BeautifulSoup(
        """
        <div class="board_view">
          <form>
            <h1>광주시, 사업 추진</h1>
            <p>광주시는 지역 사업을 추진한다고 밝혔다.</p>
            <p>시는 다음 달부터 시민 신청을 받을 예정이다.</p>
          </form>
          <div class="btn_set">목록</div>
          <div class="siiruModal">개인정보 열람 안내</div>
        </div>
        """,
        "html.parser",
    )

    text = _extract_detail_content(soup, {"content": [".board_view"]})

    assert "지역 사업" in text
    assert "개인정보" not in text


def test_extract_detail_content_prefers_article_body_over_attachment_metadata():
    soup = BeautifulSoup(
        """
        <section>
          <div class="bbs_view">
            <h3>전남도, 2027년 국고 건의사업 대응전략 논의</h3>
            <span>작성일 2026-05-20</span>
            <span>담당부서 예산담당관</span>
            <span>전남도, 2027년 국고 건의사업 대응전략 논의.hwp 다운로드 미리보기 바로듣기</span>
          </div>
          <div class="bbs_view_contnet">
            <iframe src="about:blank"></iframe>
            전남도, 2027년 국고 건의사업 대응전략 논의<br>
            - 중간보고회 열어 중앙부처의 예산요구서 반영여부 점검 -<br>
            전라남도가 2027년 국고 확보를 위해 건의사업의 중앙부처 반영 상황을 점검하고,
            기획예산처 심의 단계 대응 전략 마련에 나섰다.<br><br>
            전남도는 20일 도청 서재필실에서 국고 건의사업 중앙부처 반영 중간 보고회를 열었다.
          </div>
        </section>
        """,
        "html.parser",
    )

    text = _extract_detail_content(soup, {"content": [".bbs_view", ".bbs_view_contnet"]})

    assert "전라남도가 2027년 국고 확보" in text
    assert "다운로드" not in text


def test_extract_labeled_date_keeps_written_at_time():
    text = "작성일2026.05.14 13:56 등록자 홍보팀 조회수 72"

    assert _extract_labeled_date(text) == "2026.05.14 13:56"
    assert _normalize_published_at("(이용우 / 2026-05-20 14:03)") == "2026-05-20 14:03"
    assert _normalize_published_at("등록일\t2026-05-11 17:28:00") == "2026-05-11 17:28:00"
    assert _normalize_published_at(text) == "2026-05-14 13:56"
    assert _normalize_published_at("2026/06/26 13:36") == "2026-06-26 13:36"


def test_extract_detail_content_trims_haenam_contact_header():
    soup = BeautifulSoup(
        """
        <div class="data_cont">
          <p>“이틀만에 마감 땅끝해남 반값여행”오는 26일 2차 접수</p>
          <p>여행기간 6월 29일까지, 비용 50~70% 모바일 지역사랑상품권으로 환급</p>
          <p>〔해남군문화관광재단 관광사업팀 ☎061-535-6266〕</p>
          <p>해남군은 여행 경비를 지원하는‘땅끝해남 반값여행’2차 접수를 오는 26일부터 시작한다.</p>
          <p>개인은 5만원 이상 소비할 경우 혜택을 받을 수 있다.</p>
        </div>
        """,
        "html.parser",
    )

    text = _extract_detail_content(soup, {"content": [".data_cont"]})

    assert text.startswith("해남군은 여행 경비를 지원하는")
    assert "관광사업팀" not in text


def test_extract_detail_content_skips_leading_public_notice():
    soup = BeautifulSoup(
        """
        <div id="contents">
          내용 : 보도자료 게시판의 제목, 작성자, 작성일 안내입니다.
          공공누리 적용 저작권법 안내 문구입니다.
          공공저작권 관련 상담센터 1670-0052
          고유가 대응 운수종사자 생활안정자금 신청하세요!
          전라남도에서는 유류비 상승으로 어려움을 겪는 운수업계 지원 대책으로 생활안정자금을 지원한다.
          대상자는 5월 15일까지 신청하면 된다.
        </div>
        """,
        "html.parser",
    )

    text = _extract_detail_content(soup, {"content": ["#contents"]})

    assert text.startswith("고유가 대응 운수종사자")
    assert "공공저작권" not in text


def test_collect_json_board_maps_nested_items(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "RSLT_DATA": {
                    "boardContentsList": [
                        {
                            "dataSid": 123,
                            "dataTitle": "13:38 광주시 지역 사업 추진 NEW",
                            "dataContent": (
                                "<p>지역 사업을 추진한다고 밝혔다. 광주시는 시민 편의를 "
                                "높이기 위해 다음 달부터 신청 접수를 시작하고, 관련 기관과 "
                                "현장 점검을 이어갈 계획이라고 설명했다.</p>"
                            ),
                            "registerDate": "2026-05-20",
                        }
                    ]
                }
            }

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, params=None):
            assert url == "https://example.go.kr/api"
            assert params == {"boardId": "BBS_SAMPLE"}
            return FakeResponse()

    monkeypatch.setattr("news_summary.collectors.httpx.Client", FakeClient)
    source = Source(
        id="json-sample",
        name="JSON 게시판",
        region="전남",
        type="json_board",
        list_url="https://example.go.kr/api",
        base_url="https://example.go.kr",
        include_url_contains=["/board/detail"],
        selectors={
            "params": {"boardId": "BBS_SAMPLE"},
            "items_path": "RSLT_DATA.boardContentsList",
            "title_field": "dataTitle",
            "content_field": "dataContent",
            "published_at_field": "registerDate",
            "url_template": "/board/detail?dataSid={dataSid}",
        },
    )

    items = collect_json_board(source)

    assert len(items) == 1
    assert items[0].title == "광주시 지역 사업 추진"
    assert items[0].url == "https://example.go.kr/board/detail?dataSid=123"
    assert items[0].content == "지역 사업을 추진한다고 밝혔다. 광주시는 시민 편의를 높이기 위해 다음 달부터 신청 접수를 시작하고, 관련 기관과 현장 점검을 이어갈 계획이라고 설명했다."
    assert items[0].published_at == "2026-05-20"


def test_collect_html_board_retries_list_request(monkeypatch):
    monkeypatch.setattr("news_summary.collectors.time.sleep", lambda seconds: None)

    class FakeResponse:
        def __init__(self, text, status_code=200):
            self.text = text
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("failed", request=None, response=None)

    class FakeClient:
        def __init__(self, **kwargs):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url):
            self.calls.append(url)
            if len(self.calls) == 1:
                raise httpx.ReadTimeout("temporary timeout")
            if "list" in url:
                return FakeResponse("<a href='https://example.com/view/1'>구례군 새 사업 추진</a>")
            return FakeResponse(
                "<main>구례군은 새 사업을 추진한다고 밝혔다. "
                "구례군은 주민 편의를 높이기 위해 현장 점검과 관계 기관 협의를 이어갈 계획이라고 설명했다. "
                "이번 사업은 지역 주민의 생활 여건을 개선하고 행정 서비스를 안정적으로 제공하기 위해 마련됐다.</main>"
            )

    monkeypatch.setattr("news_summary.collectors.httpx.Client", FakeClient)
    source = Source(
        id="gurye-test",
        name="구례군청 보도자료",
        region="전남 구례",
        type="html_board",
        list_url="https://example.com/list",
        include_url_contains=["/view/"],
        selectors={"link": "a[href]", "content": ["main"]},
    )

    items = collect_html_board(source, limit=1)

    assert len(items) == 1
    assert items[0].title == "구례군 새 사업 추진"


def test_collect_html_board_limits_connection_setup_retries(monkeypatch):
    monkeypatch.setattr("news_summary.collectors.time.sleep", lambda seconds: None)

    class FakeClient:
        calls = 0

        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url):
            type(self).calls += 1
            raise httpx.ConnectTimeout("TLS 연결 시간 초과")

    monkeypatch.setattr("news_summary.collectors.httpx.Client", FakeClient)
    source = Source(
        id="timeout-test",
        name="응답 지연 기관",
        region="전남",
        type="html_board",
        list_url="https://example.com/list",
    )

    try:
        collect_html_board(source, limit=1)
    except httpx.ConnectTimeout:
        pass

    assert FakeClient.calls == 2


def test_collect_html_board_skips_failed_detail_and_continues(monkeypatch):
    monkeypatch.setattr("news_summary.collectors.time.sleep", lambda seconds: None)

    class FakeResponse:
        status_code = 200

        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url):
            if "list" in url:
                return FakeResponse(
                    "<a href='https://example.com/view/1'>무안군 첫 사업 추진</a>"
                    "<a href='https://example.com/view/2'>무안군 둘째 사업 추진</a>"
                )
            if url.endswith("/1"):
                raise httpx.RemoteProtocolError("server disconnected")
            return FakeResponse(
                "<main>무안군은 둘째 사업을 추진한다고 밝혔다. "
                "무안군은 주민 편의를 높이기 위해 현장 점검과 관계 기관 협의를 이어갈 계획이라고 설명했다. "
                "이번 사업은 지역 주민의 생활 여건을 개선하고 행정 서비스를 안정적으로 제공하기 위해 마련됐다.</main>"
            )

    monkeypatch.setattr("news_summary.collectors.httpx.Client", FakeClient)
    source = Source(
        id="muan-test",
        name="무안군청 보도자료",
        region="전남 무안",
        type="html_board",
        list_url="https://example.com/list",
        include_url_contains=["/view/"],
        selectors={"link": "a[href]", "content": ["main"]},
    )

    items = collect_html_board(source, limit=2)

    assert len(items) == 1
    assert items[0].title == "무안군 둘째 사업 추진"


def test_collect_html_board_builds_detail_url_from_onclick(monkeypatch):
    class FakeResponse:
        status_code = 200

        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url):
            if "list" in url:
                return FakeResponse(
                    """
                    <table>
                      <tr>
                        <td><a href="#" onclick="searchDetail('10996')">북구, 음식물 감량 지원</a></td>
                        <td>청소행정과</td>
                        <td>2026-06-08</td>
                      </tr>
                    </table>
                    """
                )
            assert "news_epct_no=10996" in url
            return FakeResponse(
                """
                <table summary="보도자료 상세조회">
                  <tr>
                    <td colspan="4" style="word-break:break-all;">
                      북구는 음식물 쓰레기 배출량을 줄이기 위한 지원금을 지급한다고 밝혔다.
                      북구는 대상 사업장을 선정해 설치비 일부를 지원하고 현장 점검을 이어갈 계획이다.
                      이번 사업은 폐기물 감량과 쾌적한 도시 환경 조성을 위해 마련됐다.
                    </td>
                  </tr>
                </table>
                """
            )

    monkeypatch.setattr("news_summary.collectors.httpx.Client", FakeClient)
    source = Source(
        id="bukgu-test",
        name="광주 북구청 보도자료",
        region="광주 북구",
        type="html_board",
        list_url="https://example.com/list",
        include_url_contains=["news_epct_no="],
        selectors={
            "item": "tr",
            "link": "a[onclick*='searchDetail']",
            "title": "a[onclick*='searchDetail']",
            "published_at": "td:nth-child(3)",
            "detail_url_template": "/detail?news_epct_no={detail_id}",
            "content": ["table[summary='보도자료 상세조회'] td[colspan='4']"],
        },
    )

    items = collect_html_board(source, limit=1)

    assert len(items) == 1
    assert items[0].url == "https://example.com/detail?news_epct_no=10996"
    assert items[0].published_at == "2026-06-08"


def test_canonical_url_strips_board_session_path_segments():
    url = (
        "https://www.gurye.go.kr/board/view.do;gurye.go.kr=ABC123"
        "?bbsId=BBS_0000000000000300&nttId=78378"
    )

    assert _canonical_url(url) == (
        "https://www.gurye.go.kr/board/view.do"
        "?bbsId=BBS_0000000000000300&nttId=78378"
    )


def test_canonical_url_normalizes_suncheon_press_release_aliases():
    assert (
        _canonical_url("https://www.suncheon.go.kr/kr/news/0006/0001/?mode=view&seq=71413")
        == "https://sc.go.kr/kr/news/0006/0001/?mode=view&seq=71413"
    )
    assert (
        _canonical_url("https://m.suncheon.go.kr/kr/news/0006/0001/?mode=view&seq=71413")
        == "https://sc.go.kr/kr/news/0006/0001/?mode=view&seq=71413"
    )


def test_validated_release_records_original_text_check():
    source = Source(
        id="sample",
        name="광주시청",
        region="광주",
        type="html_board",
    )

    item = _validated_release(
        source=source,
        title="광주시, 지역 돌봄 사업 확대",
        url="https://example.go.kr/board/view.do;jsessionid=ABC?nttId=1",
        content=(
            "광주시는 지역 돌봄 사업을 확대한다고 밝혔다. "
            "시는 동 행정복지센터와 함께 신청 가구를 확인하고, "
            "현장 의견을 반영해 지원 대상을 넓힐 계획이라고 설명했다."
        ),
        published_at="2026-05-20",
    )

    assert item is not None
    assert item.validation_status == "검증 완료"
    assert "제목 핵심어" in item.validation_note
    assert item.url == "https://example.go.kr/board/view.do?nttId=1"


def test_validated_release_rejects_menu_noise():
    source = Source(
        id="sample",
        name="군청",
        region="전남",
        type="html_board",
    )

    item = _validated_release(
        source=source,
        title="장성군, 청년 지원사업 추진",
        url="https://example.go.kr",
        content=(
            "본문 바로가기 주메뉴 통합검색 로그인 회원가입 사이트맵 "
            "전자민원 분야별정보 열린군정 조직도 개인정보처리방침 "
            "대표누리집 바로가기 관광누리집 바로가기 문화관광 바로가기"
        ),
        published_at=None,
    )

    assert item is None


def test_validated_release_rejects_attachment_metadata_only():
    source = Source(
        id="jeonnam",
        name="전라남도청 보도자료",
        region="전남",
        type="html_board",
    )

    item = _validated_release(
        source=source,
        title="전남도, 2027년 국고 건의사업 대응전략 논의",
        url="https://www.jeonnam.go.kr/M7116/boardView.do?seq=1961617",
        content=(
            "전남도, 2027년 국고 건의사업 대응전략 논의 작성일 2026-05-20 "
            "담당부서 예산담당관 전남도, 2027년 국고 건의사업 대응전략 논의.hwp "
            "다운로드 236KB 다운로드 미리보기 바로듣기 국고 건의사업 중앙부처 반영 "
            "중간 보고회1.jpg 다운로드 1.03MB 다운로드 미리보기"
        ),
        published_at="2026-05-20",
    )

    assert item is None
