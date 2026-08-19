from __future__ import annotations

from solstice_mcp.public_fonts import font_label, is_font_key, matches_query, public_font_url


def test_font_label_strips_md5_prefix_and_extension() -> None:
    key = "permanent_assets/513f7223784344d099366f01e688ffd2_Filson-Soft-Medium.ttf"
    assert font_label(key) == "Filson Soft Medium"


def test_font_label_keeps_fonts_subdir_name() -> None:
    assert font_label("permanent_assets/fonts/Inter-Regular.woff2") == "Inter Regular"


def test_is_font_key_skips_images() -> None:
    assert is_font_key("permanent_assets/logo.png") is False
    assert is_font_key("permanent_assets/Filson-Soft-Medium.ttf") is True


def test_matches_query_all_tokens_against_label_not_hash() -> None:
    key = "permanent_assets/513f7223784344d099366f01e688ffd2_Filson-Soft-Medium.ttf"
    assert matches_query(key, "filson medium") is True
    assert matches_query(key, "513f") is False
    assert matches_query(key, "") is True


def test_public_font_url_is_regional_https() -> None:
    key = "permanent_assets/fonts/Inter-Regular.woff2"
    assert public_font_url(key) == (
        "https://solstice-public-forever.s3.us-east-1.amazonaws.com/"
        "permanent_assets/fonts/Inter-Regular.woff2"
    )
