from __future__ import annotations

import re
import time
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

import httpx
from defusedxml.ElementTree import fromstring as safe_xml_fromstring
from bs4 import BeautifulSoup, Tag

from .asset_filters import image_asset_looks_decorative
from .models import PressRelease, PressReleaseAsset, Source
from .ops_logging import get_logger


DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NewsSummaryBot/0.1; press-release-monitor)"
}
DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=8.0)
VOLATILE_DETAIL_QUERY_PARAMS = {
    "movePage",
    "nPage",
    "page",
    "pageIndex",
    "recordCnt",
    "searchCategory1",
    "searchCategory2",
    "searchCategory3",
    "searchCondition",
    "searchEndDt",
    "searchEnDate",
    "searchKeyword",
    "searchStartDt",
    "searchStDate",
    "searchText",
    "searchType",
    "vlist_no_npage",
}
SUNCHEON_NEWS_HOST_ALIASES = {"www.suncheon.go.kr", "m.suncheon.go.kr", "sc.go.kr"}
JEONNAM_GWANGJU_PUBLIC_HOST = "www.jeonnam-gwangju.go.kr"
JEONNAM_GWANGJU_LEGACY_HOSTS = {"governor.jeonnam.go.kr"}
JEONNAM_GWANGJU_BOARD_QUERY_KEYS = ("pageId", "boardId", "seq")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
ATTACHMENT_EXTENSIONS = IMAGE_EXTENSIONS | {
    ".pdf",
    ".hwp",
    ".hwpx",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".tif",
    ".tiff",
    ".zip",
}
ASSET_SKIP_TOKENS = (
    "logo",
    "icon",
    "ico_",
    "sns",
    "facebook",
    "twitter",
    "instagram",
    "youtube",
    "opencode",
    "gonggong",
    "kogl",
    "copyright",
    "filepreview",
    "preview",
    "미리보기",
    "print",
    "blank",
    "spacer",
    "captcha",
    "layout",
    "favicon",
)
# 미리보기 계열 토큰은 UI 썸네일을 걸러내려는 것이지만, 일부 지자체는 실제 보도사진을
# preview.do 같은 주소로 제공한다. 파일 식별자를 쿼리로 받는 주소에는 적용하지 않는다.
PREVIEW_SKIP_TOKENS = ("filepreview", "preview", "미리보기")
STORED_FILE_QUERY_PATTERN = re.compile(r"(?:^|&)[a-z_]*file[a-z_]*=", re.IGNORECASE)
logger = get_logger("collectors")


class CollectionError(RuntimeError):
    pass


DATE_RE = re.compile(r"(20\d{2}[./-]\d{1,2}[./-]\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)")
LABELED_DATE_RE = re.compile(
    r"(?:작성일|게시일|등록일|입력일|보도일|날짜)\s*[:：]?\s*"
    r"(20\d{2}[./-]\d{1,2}[./-]\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)"
)
HANGUL_RE = re.compile(r"[가-힣]")
WORD_RE = re.compile(r"[가-힣A-Za-z0-9]+")
TITLE_STOPWORDS = {
    "보도자료",
    "보도",
    "자료",
    "해명",
    "군청",
    "시청",
    "전남",
    "광주",
    "대한",
    "글내용",
    "글보기",
}
NAVIGATION_NOISE_TOKENS = (
    "본문 바로가기",
    "주메뉴",
    "통합검색",
    "로그인",
    "회원가입",
    "사이트맵",
    "개인정보처리방침",
    "이메일무단수집거부",
    "전자민원",
    "분야별정보",
    "열린군정",
    "조직도",
    "만족도",
    "저작권",
    "공공누리",
)
LANDING_PAGE_TOKENS = (
    "대표누리집 바로가기",
    "관광누리집 바로가기",
    "문화관광",
    "누리집 바로가기",
    "바로가기",
)
DEFAULT_CONTENT_SELECTORS = [
    ".board_view",
    ".board-view",
    ".boardView",
    ".view_cont",
    ".view-content",
    ".viewContent",
    ".board_cont",
    ".board-content",
    ".boardContent",
    ".board_view_contents",
    ".bbs_view_contnet",
    ".bbs_view_content",
    ".bbs_view",
    ".bbs-view",
    ".detail_cont",
    ".detail-content",
    "article",
    "main",
    "#contents",
    "#content",
    ".contents",
    ".content",
]
ASSET_CONTENT_REFINEMENT_SELECTORS = [
    selector
    for selector in DEFAULT_CONTENT_SELECTORS
    if selector not in {"main", "#contents", "#content", ".contents", ".content"}
]
NOISE_SELECTORS = [
    "script",
    "style",
    "noscript",
    "iframe",
    "nav",
    "header",
    "footer",
    "button",
    "input",
    "select",
    ".sns",
    ".btn_set",
    ".siiruModal",
    ".satisfaction",
    ".pagination",
    ".paging",
    ".attach",
    ".file",
    ".comment",
]


def collect_source(source: Source, limit: int = 10) -> list[PressRelease]:
    if source.type == "rss":
        return collect_rss(source, limit=limit)
    if source.type == "json_board":
        return collect_json_board(source, limit=limit)
    if source.type == "html_board":
        return collect_html_board(source, limit=limit)
    raise CollectionError(f"지원하지 않는 수집 방식입니다: {source.type}")


def collect_rss(source: Source, limit: int = 10) -> list[PressRelease]:
    if not source.feed_url:
        raise CollectionError(f"{source.id} 설정에 RSS 주소가 없습니다.")

    with httpx.Client(
        headers=DEFAULT_HEADERS,
        timeout=DEFAULT_TIMEOUT,
        follow_redirects=True,
        verify=source.verify_ssl,
    ) as client:
        response = client.get(source.feed_url)
        response.raise_for_status()

    # 외부 엔티티/DTD 확장 공격을 막기 위해 defusedxml로 파싱한다.
    root = safe_xml_fromstring(response.content)
    items = root.findall(".//item")[:limit]
    releases = []
    for item in items:
        title = _xml_text(item, "title")
        link = _xml_text(item, "link")
        content = _xml_text(item, "description")
        published_at = _xml_text(item, "pubDate") or None
        if title and link and content:
            release = _validated_release(
                source=source,
                title=title,
                url=link,
                content=_clean_text(BeautifulSoup(content, "html.parser").get_text(" ")),
                published_at=published_at,
            )
            if release:
                releases.append(release)
    return releases


def collect_json_board(source: Source, limit: int = 10) -> list[PressRelease]:
    selectors = source.selectors or {}
    if not source.list_url:
        raise CollectionError(f"{source.id} 설정에 목록 주소가 없습니다.")

    params = selectors.get("params") or {}
    if not isinstance(params, dict):
        raise CollectionError(f"{source.id} 설정의 params는 키-값 형태여야 합니다.")

    with httpx.Client(
        headers=DEFAULT_HEADERS,
        timeout=DEFAULT_TIMEOUT,
        follow_redirects=True,
        verify=source.verify_ssl,
    ) as client:
        response = client.get(source.list_url, params=params)
        response.raise_for_status()
        data = response.json()

    items = _get_path(data, selectors.get("items_path", "items"))
    if not isinstance(items, list):
        return []

    releases = []
    title_field = selectors.get("title_field", "title")
    content_field = selectors.get("content_field", "content")
    date_field = selectors.get("published_at_field", "published_at")
    url_template = selectors.get("url_template")

    for item in items:
        if len(releases) >= limit:
            break
        if not isinstance(item, dict):
            continue
        title = _clean_title(str(item.get(title_field) or ""))
        raw_content = str(item.get(content_field) or "")
        content = _clean_text(BeautifulSoup(raw_content, "html.parser").get_text(" "))
        if not title or not content:
            continue
        if url_template:
            try:
                detail_url = url_template.format(**item)
            except KeyError:
                continue
        else:
            detail_url = str(item.get("url") or "")
        detail_url = _canonical_url(urljoin(source.base_url or source.list_url, detail_url))
        if not _is_allowed_link(source, detail_url, title):
            continue
        release = _validated_release(
            source=source,
            title=title,
            url=detail_url,
            content=_trim_boilerplate(content),
            published_at=_clean_text(str(item.get(date_field) or "")) or _extract_date(content),
            assets=_json_board_assets(client, source, selectors, item),
        )
        if release:
            releases.append(release)
    return releases


def _json_board_assets(
    client: httpx.Client,
    source: Source,
    selectors: dict,
    item: dict,
    limit: int = 24,
) -> list[PressReleaseAsset]:
    # 목록 JSON에는 첨부가 없고 상세 API에만 있는 게시판을 위한 경로다.
    # 세 설정이 모두 있어야 동작하고, 없으면 기존처럼 첨부 없이 수집한다.
    detail_template = selectors.get("detail_api_url_template")
    files_path = selectors.get("files_path")
    file_url_template = selectors.get("file_url_template")
    if not (detail_template and files_path and file_url_template):
        return []

    root_url = source.base_url or source.list_url or ""
    try:
        detail_api_url = urljoin(root_url, str(detail_template).format(**item))
    except (KeyError, IndexError):
        return []

    try:
        response = client.get(detail_api_url)
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(
            "json board detail assets skipped source_id=%s url=%s error=%s",
            source.id,
            detail_api_url,
            exc,
        )
        return []

    entries = _get_path(data, files_path)
    if not isinstance(entries, list):
        return []

    name_field = str(selectors.get("file_name_field") or "fileNm")
    assets: list[PressReleaseAsset] = []
    seen_urls: set[str] = set()
    for entry in entries:
        if len(assets) >= limit:
            break
        if not isinstance(entry, dict):
            continue
        try:
            raw_url = str(file_url_template).format(**entry)
        except (KeyError, IndexError):
            continue
        asset_url = _normal_asset_url(urljoin(root_url, raw_url), root_url)
        if not asset_url or asset_url in seen_urls:
            continue
        label = _clean_text(str(entry.get(name_field) or ""))
        extension = _asset_extension(asset_url, label)
        is_image = extension in IMAGE_EXTENSIONS
        if not is_image and extension not in ATTACHMENT_EXTENSIONS:
            continue
        if _is_noise_asset(asset_url, label):
            continue
        seen_urls.add(asset_url)
        assets.append(
            PressReleaseAsset(
                url=asset_url,
                title=label or ("사진" if is_image else "첨부파일"),
                filename=_asset_filename(asset_url, label),
                content_type=_asset_content_type(extension, is_image),
                asset_type="image" if is_image else "file",
                is_image=is_image,
                sort_order=len(assets),
            )
        )
    return assets


def collect_html_board(source: Source, limit: int = 10) -> list[PressRelease]:
    selectors = source.selectors or {}
    if not source.list_url:
        raise CollectionError(f"{source.id} 설정에 목록 주소가 없습니다.")

    with httpx.Client(
        headers=DEFAULT_HEADERS,
        timeout=DEFAULT_TIMEOUT,
        follow_redirects=True,
        verify=source.verify_ssl,
    ) as client:
        list_response = _get_with_retries(client, source.list_url, source=source, request_label="list")
        list_response.raise_for_status()
        soup = BeautifulSoup(list_response.text, "html.parser")
        rows = _candidate_rows(soup, selectors)
        if not rows:
            raise CollectionError(f"{source.name} 목록에서 보도자료 후보를 찾지 못했습니다. 사이트 구조 변경 가능성")

        releases = []
        seen_urls = set()
        for row in rows:
            if len(releases) >= limit:
                break

            title_node = _select_one(row, selectors.get("title"))
            link_node = _select_one(row, selectors.get("link", "a[href]"))
            if isinstance(row, Tag) and row.name == "a" and row.get("href"):
                link_node = row
                title_node = title_node or row
            if not title_node or not link_node:
                continue

            detail_url = _detail_url_from_node(source, row, link_node, selectors)
            if not detail_url:
                continue

            title = _node_title(title_node, selectors)
            if not _is_allowed_link(source, detail_url, title):
                continue
            if detail_url in seen_urls:
                continue
            seen_urls.add(detail_url)

            try:
                detail_response = _get_with_retries(client, detail_url, source=source, request_label="detail")
                detail_response.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning(
                    "detail collection skipped source_id=%s source_name=%s url=%s error=%s",
                    source.id,
                    source.name,
                    detail_url,
                    exc,
                )
                continue
            detail_soup = BeautifulSoup(detail_response.text, "html.parser")
            detail_text = _clean_text(detail_soup.get_text(" "))
            content = _extract_detail_content(detail_soup, selectors)
            if not content:
                continue

            date_node = row.select_one(selectors.get("published_at", "")) if selectors.get("published_at") else None
            row_text = _clean_text(row.get_text(" ")) if isinstance(row, Tag) else ""
            published_at = _normalize_published_at(_clean_text(date_node.get_text(" ")) if date_node else "")
            if not published_at and selectors.get("detail_published_at"):
                detail_date_node = detail_soup.select_one(str(selectors["detail_published_at"]))
                published_at = _normalize_published_at(
                    _clean_text(detail_date_node.get_text(" ")) if detail_date_node else ""
                )
            if not published_at:
                published_at = (
                    _extract_labeled_date(row_text)
                    or _extract_labeled_date(detail_text)
                    or _extract_date(row_text)
                    or _extract_date(content)
                )
            release = _validated_release(
                source=source,
                title=title,
                url=detail_url,
                content=content,
                published_at=published_at,
                assets=_extract_detail_assets(detail_soup, selectors, detail_url),
            )
            if release:
                releases.append(release)
        if not releases:
            raise CollectionError(f"{source.name} 수집 결과 0건입니다. 게시판 구조 또는 본문 선택자 변경 가능성")
    return releases


def _get_with_retries(
    client: httpx.Client,
    url: str,
    source: Source,
    request_label: str,
    attempts: int = 3,
) -> httpx.Response:
    last_error: httpx.HTTPError | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = client.get(url)
            if response.status_code >= 500 and attempt < attempts:
                logger.warning(
                    "http retry source_id=%s source_name=%s label=%s attempt=%s status=%s url=%s",
                    source.id,
                    source.name,
                    request_label,
                    attempt,
                    response.status_code,
                    url,
                )
                time.sleep(0.6 * attempt)
                continue
            return response
        except httpx.HTTPError as exc:
            last_error = exc
            if _is_connection_setup_error(exc) and attempt >= 2:
                break
            if attempt >= attempts:
                break
            logger.warning(
                "http retry source_id=%s source_name=%s label=%s attempt=%s error=%s url=%s",
                source.id,
                source.name,
                request_label,
                attempt,
                exc,
                url,
            )
            time.sleep(0.6 * attempt)
    assert last_error is not None
    raise last_error


def _is_connection_setup_error(exc: httpx.HTTPError) -> bool:
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))


def _xml_text(item: ElementTree.Element, tag: str) -> str:
    node = item.find(tag)
    return _clean_text(node.text or "") if node is not None else ""


def _clean_text(value: str) -> str:
    text = " ".join(value.split())
    text = re.sub(r"\s+([.,!?。])", r"\1", text)
    return text


def _candidate_rows(soup: BeautifulSoup, selectors: dict) -> list[Tag]:
    item_selector = selectors.get("item")
    if item_selector:
        return list(soup.select(item_selector))
    link_selector = selectors.get("link", "a[href]")
    return list(soup.select(link_selector))


def _select_one(node: Tag, selector: str | None) -> Tag | None:
    if not selector:
        return None
    try:
        return node.select_one(selector)
    except Exception:
        return None


def _node_title(node: Tag, selectors: dict) -> str:
    attr_name = selectors.get("title_attr")
    if attr_name:
        attr_value = node.get(str(attr_name))
        if attr_value:
            return _clean_title(str(attr_value))
    return _clean_title(node.get_text(" "))


def _detail_url_from_node(source: Source, row: Tag, link_node: Tag, selectors: dict) -> str:
    href = str(link_node.get("href") or "").strip()
    if href and href != "#" and not href.lower().startswith("javascript:"):
        return _canonical_url(urljoin(source.base_url or source.list_url or "", href))

    template = selectors.get("detail_url_template")
    if not template:
        return ""

    detail_id = _detail_id_from_node(row, link_node, selectors)
    if not detail_id:
        return ""

    return _canonical_url(
        urljoin(source.base_url or source.list_url or "", str(template).format(detail_id=detail_id))
    )


def _detail_id_from_node(row: Tag, link_node: Tag, selectors: dict) -> str:
    attr_names = ["onclick", "href", "data-id", "data-seq", "data-list-no"]
    configured_attr = selectors.get("detail_id_attr")
    if configured_attr:
        attr_names.insert(0, str(configured_attr))

    values = []
    for node in (link_node, row):
        for attr_name in attr_names:
            attr_value = node.get(attr_name)
            if attr_value:
                values.append(str(attr_value))

    pattern = str(selectors.get("detail_id_pattern") or r"searchDetail\(['\"]?([^'\")]+)")
    for value in values:
        match = re.search(pattern, value)
        if match:
            return _clean_text(match.group(1))
    return ""


def _clean_title(value: str) -> str:
    title = _clean_text(value)
    title = re.sub(r"^\d{1,2}:\d{2}\s+", "", title)
    title = re.sub(r"^(?:새글|NEW)\s+", "", title)
    title = re.sub(r"\s+(?:새글|NEW)$", "", title)
    title = re.sub(r"\s+(?:NEW|새로운글)$", "", title)
    title = re.sub(r"\s+에 대한 (?:글내용 보기|글보기)\.?$", "", title)
    title = re.sub(r"\s+20\d{2}[./-]\d{1,2}[./-]\d{1,2}$", "", title)
    return _clean_text(title)


def _is_allowed_link(source: Source, url: str, title: str) -> bool:
    if len(title) < 4:
        return False
    if any(token in title for token in source.exclude_title_contains):
        return False
    if source.include_url_contains and not any(token in url for token in source.include_url_contains):
        return False
    if any(token in title for token in ("목록", "다음글", "이전글", "첨부파일", "미리보기", "로그인")):
        return False
    return url.startswith("http://") or url.startswith("https://")


def _canonical_url(url: str) -> str:
    url = public_press_release_url(url)
    parts = urlsplit(url)
    netloc = parts.netloc
    if netloc.lower() in SUNCHEON_NEWS_HOST_ALIASES and parts.path.startswith("/kr/news/0006/0001"):
        netloc = "sc.go.kr"
    path = re.sub(r";[^/?#]*", "", parts.path)
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key not in VOLATILE_DETAIL_QUERY_PARAMS
        ],
        doseq=True,
    )
    return urlunsplit((parts.scheme, netloc, path, query, parts.fragment))


def public_press_release_url(url: str) -> str:
    """Return a browser-openable public URL for migrated official press boards."""
    raw_url = str(url or "").strip()
    parts = urlsplit(raw_url)
    host = parts.netloc.lower()
    path = parts.path.lower()
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    if host in JEONNAM_GWANGJU_LEGACY_HOSTS and path.endswith("boardview.do"):
        return _jeonnam_gwangju_public_board_url(parts, query_pairs)
    if host == JEONNAM_GWANGJU_PUBLIC_HOST and path.startswith("/mayor/") and _has_jeonnam_gwangju_board_query(query_pairs):
        return _jeonnam_gwangju_public_board_url(parts, query_pairs)
    return raw_url


def _has_jeonnam_gwangju_board_query(query_pairs: list[tuple[str, str]]) -> bool:
    keys = {key for key, value in query_pairs if value}
    return {"boardId", "seq"}.issubset(keys)


def _jeonnam_gwangju_public_board_url(parts, query_pairs: list[tuple[str, str]]) -> str:
    if not _has_jeonnam_gwangju_board_query(query_pairs):
        return parts.geturl()
    selected_pairs = [
        (key, value)
        for key, value in query_pairs
        if key in JEONNAM_GWANGJU_BOARD_QUERY_KEYS and value
    ]
    return urlunsplit(
        (
            "https",
            JEONNAM_GWANGJU_PUBLIC_HOST,
            "/boardView.do",
            urlencode(selected_pairs, doseq=True),
            "",
        )
    )


def _validated_release(
    source: Source,
    title: str,
    url: str,
    content: str,
    published_at: str | None,
    assets: list[PressReleaseAsset] | None = None,
) -> PressRelease | None:
    title = _clean_title(title)
    content = _trim_boilerplate(_clean_text(content))
    passed, note = _validate_original_text(title, content)
    if not passed:
        return None
    return PressRelease(
        source_id=source.id,
        source_name=source.name,
        region=source.region,
        title=title,
        url=_canonical_url(url),
        content=content,
        published_at=_normalize_published_at(published_at),
        validation_status="검증 완료",
        validation_note=note,
        assets=assets or [],
    )


def _validate_original_text(title: str, content: str) -> tuple[bool, str]:
    if len(title) < 4:
        return False, "원문 검증 실패: 제목이 너무 짧습니다."
    if len(content) < 80:
        return False, "원문 검증 실패: 본문이 너무 짧아 보도자료 원문으로 보기 어렵습니다."
    if _looks_like_attachment_metadata(content):
        return False, "원문 검증 실패: 첨부파일 목록 또는 게시글 정보만 수집됐습니다."

    hangul_count = len(HANGUL_RE.findall(content))
    hangul_ratio = hangul_count / max(len(content), 1)
    if hangul_count < 30 or hangul_ratio < 0.12:
        return False, "원문 검증 실패: 한글 본문 비율이 낮아 실제 보도자료인지 확인할 수 없습니다."

    first_block = content[:1200]
    title_in_content = title in content[:2000]
    keywords = _title_keywords(title)
    matched_keywords = [keyword for keyword in keywords if keyword in content]
    required_matches = 1 if len(keywords) <= 2 else 2
    if keywords and not title_in_content and len(matched_keywords) < required_matches:
        return False, "원문 검증 실패: 제목 핵심어가 본문과 충분히 일치하지 않습니다."

    navigation_hits = sum(1 for token in NAVIGATION_NOISE_TOKENS if token in first_block)
    if navigation_hits >= 5 and not title_in_content and len(matched_keywords) < required_matches + 1:
        return False, "원문 검증 실패: 본문보다 홈페이지 메뉴 문구가 더 많이 감지됐습니다."

    landing_hits = sum(1 for token in LANDING_PAGE_TOKENS if token in first_block)
    if landing_hits >= 2 and len(content) < 700 and not title_in_content:
        return False, "원문 검증 실패: 상세 보도자료가 아닌 바로가기 화면으로 보입니다."

    if not _has_sentence_like_text(content):
        return False, "원문 검증 실패: 보도자료 문장 형태를 확인하지 못했습니다."

    return (
        True,
        (
            f"본문 {len(content)}자, 제목 핵심어 {len(matched_keywords)}개 일치, "
            f"한글 비율 {hangul_ratio:.0%}를 확인했습니다."
        ),
    )


def _title_keywords(title: str) -> list[str]:
    keywords = []
    for token in WORD_RE.findall(title):
        token = token.strip()
        if len(token) < 2:
            continue
        if token in TITLE_STOPWORDS:
            continue
        if token.isdigit():
            continue
        if DATE_RE.fullmatch(token):
            continue
        keywords.append(token)
    return keywords[:8]


def _has_sentence_like_text(content: str) -> bool:
    if re.search(r"(?:다|요|임|함|됨|음|며|고)\.", content):
        return True
    if re.search(r"(?:했습니다|밝혔습니다|전했습니다|됩니다|입니다|합니다)", content):
        return True
    return len(content) >= 350 and len(re.findall(r"[가-힣]{8,}", content)) >= 4


def _extract_detail_content(soup: BeautifulSoup, selectors: dict) -> str:
    selector_candidates = []
    for selector in _as_list(selectors.get("content")):
        for node in soup.select(selector):
            text = _node_text(node)
            if len(text) >= 40:
                selector_candidates.append(text)
    best_selected = _best_content(selector_candidates)
    if best_selected:
        return _trim_boilerplate(best_selected)

    best_text = ""
    for selector in DEFAULT_CONTENT_SELECTORS:
        for node in soup.select(selector):
            text = _node_text(node)
            if _is_better_content(text, best_text):
                best_text = text

    if not best_text and soup.body:
        best_text = _node_text(soup.body)

    return _trim_boilerplate(best_text)


def _extract_detail_assets(
    soup: BeautifulSoup,
    selectors: dict,
    detail_url: str,
    limit: int = 24,
) -> list[PressReleaseAsset]:
    nodes = _asset_scope_nodes(soup, selectors)
    assets: list[PressReleaseAsset] = []
    seen_urls: set[str] = set()

    def add_asset(raw_url: str, label: str = "", *, force_image: bool = False) -> None:
        if len(assets) >= limit:
            return
        asset_url = _normal_asset_url(raw_url, detail_url)
        if not asset_url or asset_url in seen_urls:
            return
        extension = _asset_extension(asset_url, label)
        is_image = force_image or extension in IMAGE_EXTENSIONS
        if not is_image and extension not in ATTACHMENT_EXTENSIONS and not _looks_like_attachment_url(asset_url, label):
            return
        if _is_noise_asset(asset_url, label, from_image_tag=force_image):
            return
        seen_urls.add(asset_url)
        filename = _asset_filename(asset_url, label)
        assets.append(
            PressReleaseAsset(
                url=asset_url,
                title=_clean_text(label) or filename or ("사진" if is_image else "첨부파일"),
                filename=filename,
                content_type=_asset_content_type(extension, is_image),
                asset_type="image" if is_image else "file",
                is_image=is_image,
                sort_order=len(assets),
            )
        )

    for node in nodes:
        for img in node.select("img"):
            label = str(img.get("alt") or img.get("title") or "")
            for image_url in _image_asset_urls(img):
                if _is_decorative_image(img, image_url):
                    continue
                add_asset(image_url, label, force_image=True)
                if len(assets) >= limit:
                    break
        for link in node.select("a[href]"):
            label = _clean_text(link.get_text(" ") or str(link.get("title") or ""))
            add_asset(str(link.get("href") or ""), label)

    return assets


def _asset_scope_nodes(soup: BeautifulSoup, selectors: dict) -> list[Tag]:
    nodes: list[Tag] = _best_asset_content_nodes(soup, selectors)
    for selector in [
        ".attach",
        ".attachments",
        ".file",
        ".files",
        ".file_list",
        ".file-list",
        ".board_file",
        ".board-file",
        ".bbs_file",
        ".bbs-file",
        ".download",
        ".view_file",
        ".view-file",
    ]:
        nodes.extend(node for node in soup.select(selector) if isinstance(node, Tag))
    if not nodes:
        for node in _best_content_nodes_for_selectors(soup, DEFAULT_CONTENT_SELECTORS):
            nodes.append(node)
            if nodes:
                break
    if not nodes and soup.body:
        nodes.append(soup.body)
    return _dedupe_tags(nodes)


def _best_asset_content_nodes(soup: BeautifulSoup, selectors: dict) -> list[Tag]:
    configured_selectors = _as_list(selectors.get("content"))
    if configured_selectors:
        nodes = _best_content_nodes_for_selectors(soup, configured_selectors)
        if nodes:
            return _refine_asset_content_nodes(nodes)
    return _refine_asset_content_nodes(_best_content_nodes_for_selectors(soup, DEFAULT_CONTENT_SELECTORS))


def _refine_asset_content_nodes(nodes: list[Tag]) -> list[Tag]:
    refined: list[Tag] = []
    for node in nodes:
        refined.extend(_best_nested_asset_content_nodes(node) or [node])
    return _dedupe_tags(refined)


def _best_nested_asset_content_nodes(node: Tag) -> list[Tag]:
    candidates: list[tuple[int, int, Tag]] = []
    for order, selector in enumerate(ASSET_CONTENT_REFINEMENT_SELECTORS):
        for child in node.select(selector):
            if not isinstance(child, Tag) or child is node:
                continue
            text = _node_text(child)
            if len(text) < 40 or len(text) > 20000:
                continue
            score = _content_score(text)
            if score <= 0:
                continue
            candidates.append((score, -order, child))
    if not candidates:
        return []
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [candidates[0][2]]


def _best_content_nodes_for_selectors(soup: BeautifulSoup, selectors: list[str]) -> list[Tag]:
    candidates: list[tuple[int, int, Tag]] = []
    for order, selector in enumerate(selectors):
        for node in soup.select(selector):
            if not isinstance(node, Tag):
                continue
            text = _node_text(node)
            if len(text) < 40 or len(text) > 20000:
                continue
            score = _content_score(text)
            if score <= 0:
                continue
            candidates.append((score, -order, node))
    if not candidates:
        return []
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best_node = candidates[0][2]
    return [best_node]


def _dedupe_tags(nodes: list[Tag]) -> list[Tag]:
    deduped: list[Tag] = []
    seen: set[int] = set()
    for node in nodes:
        identity = id(node)
        if identity in seen:
            continue
        seen.add(identity)
        deduped.append(node)
    return deduped


def _is_decorative_image(img: Tag, image_url: str = "") -> bool:
    parts = [
        image_url,
        " ".join(str(item) for item in img.get("class", [])),
        str(img.get("id") or ""),
    ]
    for ancestor in img.parents:
        if not isinstance(ancestor, Tag) or ancestor.name in {"html", "body"}:
            break
        parts.extend(
            [
                str(ancestor.get("id") or ""),
                " ".join(str(item) for item in ancestor.get("class", [])),
                str(ancestor.get("role") or ""),
            ]
        )
        if ancestor.name in {"article", "main"}:
            break
    return image_asset_looks_decorative(*parts)


def _image_asset_urls(img: Tag) -> list[str]:
    urls: list[str] = []
    for attr in (
        "src",
        "data-src",
        "data-original",
        "data-url",
        "data-file",
        "data-img",
        "data-image",
        "data-lazy-src",
        "data-echo",
        "lazy-src",
    ):
        _append_image_url_candidate(urls, str(img.get(attr) or ""))
    srcset_urls = _image_srcset_urls(str(img.get("srcset") or "")) + _image_srcset_urls(
        str(img.get("data-srcset") or "")
    )
    if srcset_urls and (not urls or all(image_asset_looks_decorative(url) for url in urls)):
        _append_image_url_candidate(urls, srcset_urls[-1])
    return urls


def _image_srcset_urls(raw_srcset: str) -> list[str]:
    urls: list[str] = []
    for candidate in raw_srcset.split(","):
        _append_image_url_candidate(urls, candidate.strip().split(" ", 1)[0])
    return urls


def _append_image_url_candidate(urls: list[str], raw_url: str) -> None:
    raw_url = raw_url.strip()
    if not raw_url or raw_url.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
        return
    if raw_url not in urls:
        urls.append(raw_url)


def _normal_asset_url(raw_url: str, base_url: str) -> str:
    raw_url = str(raw_url or "").strip()
    if not raw_url or raw_url.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
        return ""
    return _canonical_url(urljoin(base_url, raw_url))


def _asset_extension(url: str, label: str = "") -> str:
    parts = urlsplit(url)
    candidates = [unquote(parts.path), unquote(parts.query), unquote(url), label]
    for value in candidates:
        match = re.search(
            r"\.(jpg|jpeg|png|gif|webp|bmp|pdf|hwp|hwpx|docx?|xlsx?|pptx?|tiff?|zip)(?:$|\s|[?#&=])",
            value,
            re.I,
        )
        if match:
            return f".{match.group(1).lower()}"
    return ""


def _looks_like_attachment_url(url: str, label: str = "") -> bool:
    text = f"{url} {label}".lower()
    return any(token in text for token in ("download", "attach", "atch", "file", "fileno", "file_id", "첨부", "다운로드"))


def _is_noise_asset(url: str, label: str = "", *, from_image_tag: bool = False) -> bool:
    text = f"{url} {label}".lower()
    if _is_jeonnam_gwangju_image_view_url(url):
        return True
    skip_tokens = ASSET_SKIP_TOKENS
    if from_image_tag and _serves_stored_file(url):
        # 본문 <img>가 파일 식별자를 받는 주소를 가리키면 그것이 실제 보도사진이다.
        # 미리보기 "링크"(다운로드 링크와 나란히 놓인 UI)는 여전히 걸러진다.
        skip_tokens = tuple(token for token in ASSET_SKIP_TOKENS if token not in PREVIEW_SKIP_TOKENS)
    if any(token in text for token in skip_tokens):
        return True
    if (
        "mode=view" in text
        and _asset_extension(url, label) not in ATTACHMENT_EXTENSIONS
        and not _looks_like_attachment_url(url, label)
    ):
        return True
    return False


def _serves_stored_file(url: str) -> bool:
    # fileId·fileSeq처럼 파일 식별자를 쿼리로 받는 주소는 실제 첨부를 내려주는 경로다.
    return bool(STORED_FILE_QUERY_PATTERN.search(urlsplit(url).query))


def _is_jeonnam_gwangju_image_view_url(url: str) -> bool:
    parts = urlsplit(url)
    host = parts.netloc.lower()
    return (
        host in {JEONNAM_GWANGJU_PUBLIC_HOST, *JEONNAM_GWANGJU_LEGACY_HOSTS}
        and parts.path.lower().startswith("/imageview/")
    )


def _asset_filename(url: str, label: str = "") -> str:
    query_filename = _asset_query_filename(url)
    if query_filename:
        return query_filename[:160]
    label = _clean_text(label)
    if label and _asset_extension("", label) in ATTACHMENT_EXTENSIONS:
        return label[:160]
    path_name = unquote(urlsplit(url).path.rsplit("/", 1)[-1]).strip()
    if path_name and "." in path_name and not _looks_like_download_handler_path(path_name):
        return path_name[:160]
    if label and "." in label:
        return label[:160]
    return path_name[:160] if path_name else label[:160]


def _looks_like_download_handler_path(path_name: str) -> bool:
    return path_name.lower().endswith((".do", ".php", ".asp", ".aspx", ".jsp"))


def _asset_query_filename(url: str) -> str:
    filename_keys = {
        "filename",
        "file_name",
        "fileName",
        "fileNm",
        "filenm",
        "orignlFileNm",
        "orgFileNm",
        "originFileNm",
        "realFileNm",
        "atchFileNm",
        "downFileNm",
    }
    for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        if key in filename_keys and value:
            return unquote(value).strip()
    return ""


def _asset_content_type(extension: str, is_image: bool) -> str:
    if extension == ".jpg":
        extension = ".jpeg"
    if is_image and extension:
        return f"image/{extension.lstrip('.')}"
    return {
        ".pdf": "application/pdf",
        ".hwp": "application/x-hwp",
        ".hwpx": "application/hwp+zip",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xls": "application/vnd.ms-excel",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".ppt": "application/vnd.ms-powerpoint",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".zip": "application/zip",
    }.get(extension, "")


def _node_text(node: Tag | None) -> str:
    if not node:
        return ""
    clone = BeautifulSoup(str(node), "html.parser")
    for noise in clone.select(", ".join(NOISE_SELECTORS)):
        noise.decompose()
    return _clean_text(clone.get_text("\n"))


def _is_better_content(text: str, current: str) -> bool:
    if len(text) < 40:
        return False
    if len(text) > 20000:
        return False
    return _content_score(text) > _content_score(current)


def _best_content(candidates: list[str]) -> str:
    best = ""
    for text in candidates:
        if _is_better_content(text, best):
            best = text
    return best


def _content_score(text: str) -> int:
    if not text:
        return 0
    text = _clean_text(text)
    sentence_count = _sentence_count(text)
    navigation_hits = sum(1 for token in NAVIGATION_NOISE_TOKENS if token in text[:1500])
    attachment_hits = _attachment_noise_count(text)
    score = min(len(text), 5000)
    score += sentence_count * 250
    score -= navigation_hits * 300
    score -= attachment_hits * 180
    if _looks_like_attachment_metadata(text):
        score -= 2000
    return score


def _sentence_count(text: str) -> int:
    return len(re.findall(r"(?:다|요|임|함|됨|음|했다|한다|있다|이다|밝혔다|말했다|전했다)\.", text))


def _attachment_noise_count(text: str) -> int:
    return sum(text.count(token) for token in ("다운로드", "미리보기", "바로듣기", ".hwp", ".hwpx", ".pdf", ".jpg", ".JPG"))


def _looks_like_attachment_metadata(content: str) -> bool:
    content = _clean_text(content)
    attachment_hits = _attachment_noise_count(content)
    has_board_meta = "작성일" in content and "담당부서" in content
    has_file_actions = "다운로드" in content and ("미리보기" in content or "바로듣기" in content)
    return (has_board_meta or has_file_actions) and attachment_hits >= 2 and _sentence_count(content) < 2


def _trim_boilerplate(text: str) -> str:
    text = _trim_leading_contact_metadata(_clean_text(text))
    markers = [
        "첨부파일",
        "목록",
        "개인정보 열람",
        "자료관리 담당자",
        "만족도조사",
        "콘텐츠 관리부서",
        "Q. 현재 페이지",
        "공공누리",
        "본 저작물은",
        "삭제 수정",
        "다음글",
        "이전글",
    ]
    cut_at = len(text)
    for marker in markers:
        idx = text.find(marker)
        if idx > 80 and _sentence_count(text[:idx]) >= 2:
            cut_at = min(cut_at, idx)
    return _clean_text(text[:cut_at])


def _trim_leading_contact_metadata(text: str) -> str:
    public_notice = re.search(r"공공저작권\s+관련\s+상담센터\s*\d{3,4}-\d{4}", text[:1200])
    if public_notice:
        return text[public_notice.end() :].strip()
    match = re.search(r"〔[^〕]{0,220}(?:☎|\d{2,4}-\d{3,4})[^〕]{0,220}〕", text[:600])
    if match:
        return text[match.end() :].strip()
    return text


def _extract_date(text: str) -> str | None:
    match = DATE_RE.search(text)
    return match.group(1) if match else None


def _extract_labeled_date(text: str) -> str | None:
    match = LABELED_DATE_RE.search(text)
    return match.group(1) if match else None


def _normalize_published_at(value: str | None) -> str | None:
    if not value:
        return None
    text = _clean_text(str(value))
    return _standardize_published_at(_extract_labeled_date(text) or _extract_date(text))


def _standardize_published_at(value: str | None) -> str | None:
    if not value:
        return None
    match = DATE_RE.search(str(value).strip())
    if not match:
        return None
    matched = match.group(1)
    date_part, _, time_part = matched.partition(" ")
    year, month, day = re.split(r"[./-]", date_part)
    normalized = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    if time_part:
        time_parts = time_part.split(":")
        hour = int(time_parts[0])
        minute = int(time_parts[1])
        if len(time_parts) > 2:
            second = int(time_parts[2])
            return f"{normalized} {hour:02d}:{minute:02d}:{second:02d}"
        return f"{normalized} {hour:02d}:{minute:02d}"
    return normalized


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _get_path(data: object, path: object) -> object:
    current = data
    for part in str(path).split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current
