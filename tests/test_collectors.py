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
    public_press_release_url,
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


def test_collect_html_board_extracts_images_and_attachments(monkeypatch):
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
            if url.endswith("/list"):
                return FakeResponse(
                    """
                    <ul>
                      <li><a href="/view/1">테스트군 현장 사업 추진</a></li>
                    </ul>
                    """
                )
            return FakeResponse(
                """
                <main>
                  <div class="content">
                    <p>테스트군 현장 사업 추진</p>
                    <p>테스트군은 주민 편의를 높이기 위해 현장 사업을 추진한다고 밝혔다.</p>
                    <p>군은 관계 기관 협의를 거쳐 다음 달부터 사업을 본격화하고 현장 점검을 이어갈 계획이다.</p>
                    <img src="/uploads/field-photo.webp" alt="현장 사진">
                  </div>
                  <div class="file_list">
                    <a href="/download?fileId=3&fileName=%EB%B3%B4%EB%8F%84%EC%9E%90%EB%A3%8C.hwp">보도자료 원문 다운로드</a>
                  </div>
                </main>
                """
            )

    monkeypatch.setattr("news_summary.collectors.httpx.Client", FakeClient)
    source = Source(
        id="asset-test",
        name="첨부 테스트 군청",
        region="전남",
        type="html_board",
        list_url="https://example.com/list",
        base_url="https://example.com",
        include_url_contains=["/view/"],
        selectors={"link": "a[href]", "content": [".content"]},
    )

    items = collect_html_board(source, limit=1)

    assert len(items) == 1
    assert [asset.asset_type for asset in items[0].assets] == ["image", "file"]
    assert items[0].assets[0].url == "https://example.com/uploads/field-photo.webp"
    assert items[0].assets[0].content_type == "image/webp"
    assert items[0].assets[1].filename == "보도자료.hwp"
    assert items[0].assets[1].content_type == "application/x-hwp"


def test_collect_html_board_skips_homepage_images_outside_press_body(monkeypatch):
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
            if url.endswith("/list"):
                return FakeResponse(
                    """
                    <ul>
                      <li><a href="/view/1">광양시 농업촬영 심화반 교육생 모집</a></li>
                    </ul>
                    """
                )
            return FakeResponse(
                """
                <main>
                  <div class="main_visual">
                    <img src="/images/main-banner.jpg" alt="시정 홍보 배너">
                  </div>
                  <nav>
                    <img src="/images/menu-icon.png" alt="메뉴">
                  </nav>
                  <article class="board_view">
                    <p>광양시는 농산물 온라인 홍보를 돕기 위해 농업촬영 심화반 교육생을 모집한다고 밝혔다.</p>
                    <p>이번 교육은 농업인 크리에이터를 대상으로 촬영 실습과 콘텐츠 제작 역량 강화를 지원한다.</p>
                    <p>참여 희망자는 지정된 기간 안에 신청하면 되며, 시는 교육생을 선발해 운영할 계획이다.</p>
                    <img src="/upload/editor/press-photo.jpg" alt="농업촬영 교육 사진">
                  </article>
                  <div class="file_list">
                    <a href="/download?fileId=3&fileName=press.hwp">보도자료 다운로드</a>
                  </div>
                </main>
                """
            )

    monkeypatch.setattr("news_summary.collectors.httpx.Client", FakeClient)
    source = Source(
        id="asset-scope-test",
        name="첨부 범위 테스트 시청",
        region="전남",
        type="html_board",
        list_url="https://example.com/list",
        base_url="https://example.com",
        include_url_contains=["/view/"],
        selectors={"link": "a[href]", "content": ["main"]},
    )

    items = collect_html_board(source, limit=1)

    assert len(items) == 1
    assert [asset.url for asset in items[0].assets] == [
        "https://example.com/upload/editor/press-photo.jpg",
        "https://example.com/download?fileId=3&fileName=press.hwp",
    ]
    assert all("main-banner" not in asset.url for asset in items[0].assets)
    assert all("menu-icon" not in asset.url for asset in items[0].assets)


def test_collect_html_board_skips_related_story_images_when_content_selector_is_broad(monkeypatch):
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
            if url.endswith("/list"):
                return FakeResponse(
                    """
                    <ul>
                      <li><a href="/view/1">광양시 농업촬영 심화반 교육생 모집</a></li>
                    </ul>
                    """
                )
            return FakeResponse(
                """
                <main>
                  <article class="board_view">
                    <p>광양시는 농산물 온라인 홍보를 돕기 위해 농업촬영 심화반 교육생을 모집한다고 밝혔다.</p>
                    <p>이번 교육은 농업인 크리에이터를 대상으로 촬영 실습과 콘텐츠 제작 역량 강화를 지원한다.</p>
                    <p>참여 희망자는 지정된 기간 안에 신청하면 되며, 시는 교육생을 선발해 운영할 계획이다.</p>
                    <img src="/upload/editor/press-photo.jpg" alt="농업촬영 교육 사진">
                  </article>
                  <section class="related_story">
                    <h2>함께 보는 소식</h2>
                    <a href="/view/old">
                      <img src="/upload/editor/old-event-photo.jpg" alt="지난 행사 사진">
                      지난 행사 소식
                    </a>
                  </section>
                  <div class="file_list">
                    <a href="/download?fileId=3&fileName=press.hwp">보도자료 다운로드</a>
                  </div>
                </main>
                """
            )

    monkeypatch.setattr("news_summary.collectors.httpx.Client", FakeClient)
    source = Source(
        id="asset-related-scope-test",
        name="첨부 관련글 테스트 시청",
        region="전남",
        type="html_board",
        list_url="https://example.com/list",
        base_url="https://example.com",
        include_url_contains=["/view/"],
        selectors={"link": "a[href]", "content": ["main"]},
    )

    items = collect_html_board(source, limit=1)

    assert len(items) == 1
    assert [asset.url for asset in items[0].assets] == [
        "https://example.com/upload/editor/press-photo.jpg",
        "https://example.com/download?fileId=3&fileName=press.hwp",
    ]
    assert all("old-event-photo" not in asset.url for asset in items[0].assets)


def test_collect_html_board_extracts_lazy_loaded_press_images(monkeypatch):
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
            if url.endswith("/list"):
                return FakeResponse("<a href='/view/1'>나주시 현장 사진 보도자료</a>")
            return FakeResponse(
                """
                <main>
                  <article class="board_view">
                    <p>나주시는 지역 현장 점검 결과를 보도자료로 배포했다고 밝혔다.</p>
                    <p>시는 주민 의견을 반영해 후속 조치를 마련하고 관계 기관 협의를 이어갈 계획이다.</p>
                    <p>이번 점검은 지역 생활 여건 개선을 위해 추진됐다.</p>
                    <img src="/images/blank.gif" data-src="/upload/press/lazy-photo.jpg" alt="현장 점검 사진">
                    <img data-original="/upload/press/original-photo.png" alt="후속 조치 사진">
                  </article>
                </main>
                """
            )

    monkeypatch.setattr("news_summary.collectors.httpx.Client", FakeClient)
    source = Source(
        id="lazy-image-test",
        name="지연 이미지 테스트 시청",
        region="전남",
        type="html_board",
        list_url="https://example.com/list",
        base_url="https://example.com",
        include_url_contains=["/view/"],
        selectors={"link": "a[href]", "content": ["main"]},
    )

    items = collect_html_board(source, limit=1)

    assert len(items) == 1
    assert [asset.url for asset in items[0].assets] == [
        "https://example.com/upload/press/lazy-photo.jpg",
        "https://example.com/upload/press/original-photo.png",
    ]
    assert all("blank.gif" not in asset.url for asset in items[0].assets)


def test_collect_html_board_uses_one_srcset_press_image_candidate(monkeypatch):
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
            if url.endswith("/list"):
                return FakeResponse("<a href='/view/1'>화순군 행사 사진 보도자료</a>")
            return FakeResponse(
                """
                <main>
                  <article class="board_view">
                    <p>화순군은 지역 행사를 개최하고 참여 주민을 대상으로 현장 의견을 들었다고 밝혔다.</p>
                    <p>군은 행사 결과를 바탕으로 다음 사업 계획을 보완하고 지원 체계를 정비할 예정이다.</p>
                    <p>이번 행사는 지역 공동체 활성화를 위해 마련됐다.</p>
                    <img src="/images/blank.gif"
                         srcset="/upload/press/event-small.jpg 480w, /upload/press/event-large.jpg 1024w"
                         alt="행사 사진">
                  </article>
                </main>
                """
            )

    monkeypatch.setattr("news_summary.collectors.httpx.Client", FakeClient)
    source = Source(
        id="srcset-image-test",
        name="srcset 이미지 테스트 군청",
        region="전남",
        type="html_board",
        list_url="https://example.com/list",
        base_url="https://example.com",
        include_url_contains=["/view/"],
        selectors={"link": "a[href]", "content": ["main"]},
    )

    items = collect_html_board(source, limit=1)

    assert len(items) == 1
    assert [asset.url for asset in items[0].assets] == [
        "https://example.com/upload/press/event-large.jpg",
    ]


def test_collect_html_board_extracts_only_jeonnam_gwangju_attached_files(monkeypatch):
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
            if "boardList.do" in url:
                return FakeResponse(
                    """
                    <a href="/boardView.do?pageId=jngj22&amp;boardId=JG_0000000003&amp;seq=78">
                      무등산권 세계지질공원, 스페인 그라나다와 국제협력 다진다
                    </a>
                    """
                )
            return FakeResponse(
                """
                <main>
                  <img src="/home/jngj/images/main/top_slogan.png" alt="압도적 성장 함께 사는 특별시">
                  <section class="card-news">
                    <a href="/imageView/cardnews1">
                      <img src="/imageView/cardnews1" alt="장마철 안전수칙">
                    </a>
                    <img src="/upload/co/1783492266550.jpg" alt="공지 이미지">
                  </section>
                  <article class="board_view">
                    <p>전남광주통합특별시는 스페인 그라나다 세계지질공원 방문단이 무등산권 세계지질공원을 방문한다고 밝혔다.</p>
                    <p>방문단은 무등산권 세계지질공원 운영 현황을 살피고 국제협력 방안을 논의할 예정이다.</p>
                    <p>시는 이번 교류를 계기로 지질공원 보전과 관광 활성화 협력을 이어갈 계획이다.</p>
                    <img src="/imageView/article-preview" alt="본문 미리보기 사진">
                  </article>
                  <div class="file_list">
                    <a href="/fileDownload.do?fileSe=BB&amp;fileKey=JG_0000000003%7C78&amp;fileSn=1&amp;boardId=JG_0000000003&amp;seq=78">무등산권 세계지질공원.hwpx</a>
                    <a href="/filePreView.do?action=H&amp;fileSn=1">미리보기</a>
                    <a href="/fileDownload.do?fileSe=BB&amp;fileKey=JG_0000000003%7C78&amp;fileSn=2&amp;boardId=JG_0000000003&amp;seq=78">스페인 그라나다 세계지질공원 방문단 (1).jpeg</a>
                    <a href="/filePreView.do?action=I&amp;fileSn=2">미리보기</a>
                    <a href="/fileDownload.do?fileSe=BB&amp;fileKey=JG_0000000003%7C78&amp;fileSn=3&amp;boardId=JG_0000000003&amp;seq=78">스페인 그라나다 세계지질공원 방문단 (2).jpeg</a>
                  </div>
                </main>
                """
            )

    monkeypatch.setattr("news_summary.collectors.httpx.Client", FakeClient)
    source = Source(
        id="jeonnam-gwangju-asset-test",
        name="전남광주통합특별시청 보도자료",
        region="전남광주통합특별시",
        type="html_board",
        list_url="https://www.jeonnam-gwangju.go.kr/boardList.do?pageId=jngj22&boardId=JG_0000000003",
        base_url="https://www.jeonnam-gwangju.go.kr",
        include_url_contains=["boardView.do", "JG_0000000003"],
        selectors={
            "link": "a[href*='boardView.do'][href*='JG_0000000003']",
            "content": ["main"],
        },
    )

    items = collect_html_board(source, limit=1)

    assert len(items) == 1
    assert items[0].url == "https://www.jeonnam-gwangju.go.kr/boardView.do?pageId=jngj22&boardId=JG_0000000003&seq=78"
    assert [asset.filename for asset in items[0].assets] == [
        "무등산권 세계지질공원.hwpx",
        "스페인 그라나다 세계지질공원 방문단 (1).jpeg",
        "스페인 그라나다 세계지질공원 방문단 (2).jpeg",
    ]
    assert [asset.asset_type for asset in items[0].assets] == ["file", "image", "image"]
    assert all("imageView" not in asset.url for asset in items[0].assets)
    assert all("/home/jngj/images/main/" not in asset.url for asset in items[0].assets)
    assert all("/upload/co/" not in asset.url for asset in items[0].assets)


def test_public_press_release_url_rewrites_jeonnam_governor_detail_url():
    old_url = "https://governor.jeonnam.go.kr/boardView.do?pageId=jngj22&boardId=JG_0000000003&seq=78"

    assert public_press_release_url(old_url) == (
        "https://www.jeonnam-gwangju.go.kr/boardView.do?"
        "pageId=jngj22&boardId=JG_0000000003&seq=78"
    )
    assert _canonical_url(old_url) == (
        "https://www.jeonnam-gwangju.go.kr/boardView.do?"
        "pageId=jngj22&boardId=JG_0000000003&seq=78"
    )


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


def test_collect_html_board_keeps_body_photo_served_from_preview_endpoint(monkeypatch):
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
            if url.endswith("/list"):
                return FakeResponse('<ul><li><a href="/view/1">해남군 미소 기획전 개최</a></li></ul>')
            return FakeResponse(
                """
                <div class="data_cont">
                  <p>해남군은 지역 농특산물을 널리 알리기 위한 기획전을 다음 달부터 연다고 밝혔다.</p>
                  <p>군은 참여 업체를 모집하고 현장 홍보와 판촉 행사를 함께 이어갈 계획이다.</p>
                  <p>기획전은 지역 농가의 판로를 넓히고 소비자 접점을 늘리기 위해 마련됐다.</p>
                  <img src="https://portal.example.go.kr/jfile/preview.do?fileId=abc123&fileSeq=1" alt="기획전 사진">
                  <img src="/images/btn_preview.png" alt="미리보기">
                </div>
                """
            )

    monkeypatch.setattr("news_summary.collectors.httpx.Client", FakeClient)
    source = Source(
        id="preview-endpoint-test",
        name="미리보기 주소 테스트 군청",
        region="전남",
        type="html_board",
        list_url="https://example.com/list",
        base_url="https://example.com",
        include_url_contains=["/view/"],
        selectors={"link": "a[href]", "content": [".data_cont"]},
    )

    items = collect_html_board(source, limit=1)

    # preview.do라도 파일 식별자를 받는 주소는 실제 보도사진이므로 수집한다.
    image_urls = [asset.url for asset in items[0].assets if asset.is_image]
    assert image_urls == ["https://portal.example.go.kr/jfile/preview.do?fileId=abc123&fileSeq=1"]
    # 파일 식별자가 없는 미리보기 버튼 이미지는 그대로 걸러진다.
    assert not [url for url in image_urls if "btn_preview" in url]


def test_collect_json_board_collects_assets_from_detail_api(monkeypatch):
    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self):
            return None

    list_payload = {
        "RSLT_DATA": {
            "boardContentsList": [
                {
                    "dataSid": 819033,
                    "dataTitle": "담양군, 상품권 운영 개선",
                    "dataContent": (
                        "<p>담양군은 상품권 이용자의 편의를 높이기 위해 운영 방식을 개선한다고 밝혔다.</p>"
                        "<p>군은 가맹점 등록 절차를 간소화하고 부정유통 관리도 지속해 나갈 계획이다.</p>"
                        "<p>이번 개선으로 이용자가 구매 단계에서 받는 혜택이 한층 늘어날 것으로 기대된다.</p>"
                    ),
                    "registerDate": "2026-07-28",
                }
            ]
        }
    }
    detail_payload = {
        "RSLT_DATA": {
            "boardDetail": {
                "boardContentsFileList": [
                    {"fileNm": "보도자료.hwpx", "fileSid": 213912},
                    {"fileNm": "담양군청(2026.07).jpg", "fileSid": 213913},
                ]
            }
        }
    }

    requested = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, params=None):
            requested.append(url)
            if "getBoardDetail" in url:
                return FakeResponse(detail_payload)
            return FakeResponse(list_payload)

    monkeypatch.setattr("news_summary.collectors.httpx.Client", FakeClient)
    source = Source(
        id="json-detail-asset-test",
        name="상세 API 첨부 테스트 군청",
        region="전남",
        type="json_board",
        list_url="https://example.com/board/getContentsList",
        base_url="https://example.com",
        include_url_contains=["/board/detail"],
        selectors={
            "items_path": "RSLT_DATA.boardContentsList",
            "title_field": "dataTitle",
            "content_field": "dataContent",
            "published_at_field": "registerDate",
            "url_template": "/board/detail?dataSid={dataSid}",
            "detail_api_url_template": "/board/getBoardDetail?dataSid={dataSid}",
            "files_path": "RSLT_DATA.boardDetail.boardContentsFileList",
            "file_url_template": "/board/getFile?fileSid={fileSid}",
            "file_name_field": "fileNm",
        },
    )

    items = collect_json_board(source, limit=1)

    assert len(items) == 1
    assets = items[0].assets
    assert [asset.asset_type for asset in assets] == ["file", "image"]
    assert assets[1].url == "https://example.com/board/getFile?fileSid=213913"
    assert assets[1].content_type == "image/jpeg"
    assert assets[1].title == "담양군청(2026.07).jpg"
    assert any("getBoardDetail" in url for url in requested)


def test_collect_json_board_without_detail_config_skips_asset_lookup(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "RSLT_DATA": {
                    "boardContentsList": [
                        {
                            "dataSid": 1,
                            "dataTitle": "테스트군 사업 추진",
                            "dataContent": (
                                "<p>테스트군은 주민 편의를 높이기 위해 새로운 사업을 추진한다고 밝혔다.</p>"
                                "<p>군은 관계 기관 협의를 거쳐 다음 달부터 사업을 본격적으로 시작할 계획이다.</p>"
                                "<p>사업 대상과 세부 일정은 주민 의견을 수렴해 확정할 예정이다.</p>"
                            ),
                            "registerDate": "2026-07-28",
                        }
                    ]
                }
            }

        def raise_for_status(self):
            return None

    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, params=None):
            calls.append(url)
            return FakeResponse()

    monkeypatch.setattr("news_summary.collectors.httpx.Client", FakeClient)
    source = Source(
        id="json-no-detail-test",
        name="상세 설정 없는 테스트 군청",
        region="전남",
        type="json_board",
        list_url="https://example.com/board/getContentsList",
        base_url="https://example.com",
        include_url_contains=["/board/detail"],
        selectors={
            "items_path": "RSLT_DATA.boardContentsList",
            "title_field": "dataTitle",
            "content_field": "dataContent",
            "published_at_field": "registerDate",
            "url_template": "/board/detail?dataSid={dataSid}",
        },
    )

    items = collect_json_board(source, limit=1)

    # 상세 API 설정이 없으면 추가 요청 없이 기존처럼 동작한다.
    assert items[0].assets == []
    assert len(calls) == 1
