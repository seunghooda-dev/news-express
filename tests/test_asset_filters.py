from news_summary.asset_filters import image_asset_looks_decorative


def test_image_asset_filter_detects_common_decorative_assets():
    assert image_asset_looks_decorative("https://example.com/images/main-banner.jpg")
    assert image_asset_looks_decorative("https://example.com/images/menu-icon.png")
    assert image_asset_looks_decorative("https://example.com/images/blank.gif")
    assert image_asset_looks_decorative("https://example.com/images/main/img_visual_deco1.png")
    assert image_asset_looks_decorative("https://example.com/images/main/top_slogan.png")
    assert image_asset_looks_decorative("https://example.com/images/main/img_simin.png")
    assert image_asset_looks_decorative("https://governor.jeonnam.go.kr/imageView/temporary-card-news")
    assert image_asset_looks_decorative("https://www.jeonnam-gwangju.go.kr/imageView/temporary-preview")
    assert image_asset_looks_decorative("https://governor.jeonnam.go.kr/upload/co/1783492266550.jpg")
    assert image_asset_looks_decorative("quick sns logo")
    # 게시판 조작 버튼이 사진으로 수집되던 사례(장성군 "목록" 버튼)
    assert image_asset_looks_decorative("https://www.jangseong.go.kr/images/board/board_list.gif")
    assert image_asset_looks_decorative("https://example.com/images/board/btn_prev.gif")


def test_image_asset_filter_avoids_short_token_false_positives():
    assert not image_asset_looks_decorative("https://example.com/upload/silicon-valley-event.jpg")
    assert not image_asset_looks_decorative("https://example.com/upload/navigation-safety-photo.jpg")
    assert not image_asset_looks_decorative("신규 아이콘택트 사업 현장 사진")
    # 올라온 사진은 경로가 비슷해도 걸러지지 않아야 한다
    assert not image_asset_looks_decorative("https://example.com/upload/board/press-photo.jpg")
    assert not image_asset_looks_decorative("https://example.com/upload/boardgame-festival.jpg")
