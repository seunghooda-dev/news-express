from news_summary.asset_filters import image_asset_looks_decorative


def test_image_asset_filter_detects_common_decorative_assets():
    assert image_asset_looks_decorative("https://example.com/images/main-banner.jpg")
    assert image_asset_looks_decorative("https://example.com/images/menu-icon.png")
    assert image_asset_looks_decorative("https://example.com/images/blank.gif")
    assert image_asset_looks_decorative("quick sns logo")


def test_image_asset_filter_avoids_short_token_false_positives():
    assert not image_asset_looks_decorative("https://example.com/upload/silicon-valley-event.jpg")
    assert not image_asset_looks_decorative("https://example.com/upload/navigation-safety-photo.jpg")
    assert not image_asset_looks_decorative("신규 아이콘택트 사업 현장 사진")
