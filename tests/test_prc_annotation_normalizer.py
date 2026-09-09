from __future__ import annotations

import pytest

from solstice_mcp.prc_annotation_normalizer import (
    LegacyAnnotationFormatError,
    assert_no_legacy_annotations,
    normalize_legacy_annotations,
)


def _contract_base(content_type: str, extra: str = "") -> str:
    slot = {"email": "desktop", "banner": "banner", "social": "social"}[content_type]
    return (
        '<!doctype html><html><head><meta name="sol-prc-contract" content="v2">'
        '<style id="sol-prc-export-style"></style></head>'
        f'<body class="sol-prc-export" data-sol-prc-proof="{content_type}">'
        '<main data-sol-prc-pages><section data-sol-prc-page="page">'
        '<span data-sol-prc-field="file_name"></span>'
        f'<iframe data-sol-prc-creative="{slot}" srcdoc="creative"></iframe>'
        f"</section></main>{extra}</body></html>"
    )


def test_normalizer_preserves_canonical_v2_positions_and_platform_runtime():
    source = _contract_base(
        "email",
        '<script id="sol-prc-annotation-positions" type="application/json">{"keep":{"x":4}}</script>'
        '<script id="sol-prc-template-runtime">el.className="callout-box";</script>',
    )

    assert normalize_legacy_annotations(source, "email") == source
    assert_no_legacy_annotations(source)


def test_normalizer_preserves_v2_overlay_that_reuses_legacy_classes():
    source = _contract_base(
        "email",
        '<div class="sol-prc-annotation-overlay-v2">'
        '<div class="prc-callout sol-prc-v2-callout">current</div>'
        '<svg><polyline class="prc-connector-line sol-prc-v2-connector"></polyline>'
        '<circle class="callout-dot sol-prc-v2-dot"></circle></svg></div>',
    )

    assert normalize_legacy_annotations(source, "email") == source
    assert_no_legacy_annotations(source)


def test_normalizer_rejects_unrecognized_mixed_legacy_script():
    source = _contract_base(
        "social",
        '<script>hydrateSocial(); el.className="callout-box";</script>',
    )

    with pytest.raises(LegacyAnnotationFormatError, match="legacy annotation source"):
        normalize_legacy_annotations(source, "social")


def test_normalizer_ignores_legacy_tokens_inside_creative_srcdoc():
    source = _contract_base("email").replace(
        'srcdoc="creative"',
        'srcdoc="&lt;style&gt;.callout-box{color:red}&lt;/style&gt;"',
    )

    assert normalize_legacy_annotations(source, "email") == source


def test_normalizer_unwraps_legacy_stage_without_removing_creative():
    source = _contract_base("email").replace(
        '<iframe data-sol-prc-creative="desktop" srcdoc="creative"></iframe>',
        '<div class="prc-render-stage"><iframe data-sol-prc-creative="desktop" srcdoc="creative"></iframe></div>',
    )

    normalized = normalize_legacy_annotations(source, "email")

    assert "prc-render-stage" not in normalized
    assert '<iframe data-sol-prc-creative="desktop" srcdoc="creative"></iframe>' in normalized


def test_normalizer_removes_legacy_rules_inside_media_query():
    source = _contract_base(
        "social",
        "<style>@media print{.callout-box{display:none}.proof{display:block}}</style>",
    )

    normalized = normalize_legacy_annotations(source, "social")

    assert "callout-box" not in normalized
    assert ".proof{display:block}" in normalized
    assert_no_legacy_annotations(normalized)


def test_normalizer_removes_sanofi_engine_without_breaking_banner_hydration():
    source = _contract_base(
        "banner",
        """
<div class="callout-overlay" data-callouts="all"></div>
<style>
/* === Functional callouts (hyperlink-destination annotations) === */
.callout-overlay{position:absolute}.callout-box{color:magenta}
.banner-layout{display:grid}
</style>
<script>
(function () {
  function storyboardPagesForSection(section) {
    var clone = section.cloneNode(true);
    var overlay = clone.querySelector(".callout-overlay");
    if (overlay) overlay.innerHTML = "";
    return clone;
  }
  function hydrate(container) {
    iframe.srcdoc = bannerSrcdocForScene(container.rawSrcdoc, container.scene, "contain");
    renderPageCallouts(pageEl);
    setTimeout(function () { renderPageCallouts(pageEl); }, 220);
  }
  function hydrateIsi(container) {
    iframe.srcdoc = bannerSrcdocForScene(container.rawSrcdoc, container.scene, "isi-only");
    renderPageCallouts(pageEl);
  }
  /* ============================================================
   * Hyperlink-destination annotation engine (self-contained).
   * ============================================================ */
  function renderPageCallouts(pageEl) {
    var box = document.createElement("div");
    box.className = "callout-box";
    box.setAttribute("data-sol-prc-annotation-key", "old");
  }
  function renderAllCallouts() { renderPageCallouts(document.body); }
  window.addEventListener("resize", renderAllCallouts);

  /* ---- Bootstrap: iterate every banner section and hydrate ---- */
  var sections = document.querySelectorAll("[data-banner-section]");
  hydrateBannerSection(sections[0], 0, false);
  allFrameContainers.forEach(hydrate);
  allIsiContainers.forEach(hydrateIsi);
})();
</script>
""",
    )

    normalized = normalize_legacy_annotations(source, "banner")

    assert ".banner-layout{display:grid}" in normalized
    assert "bannerSrcdocForScene" in normalized
    assert "hydrateBannerSection" in normalized
    assert "allFrameContainers.forEach(hydrate)" in normalized
    assert "allIsiContainers.forEach(hydrateIsi)" in normalized
    for leftover in (
        "callout-overlay",
        "callout-box",
        "renderPageCallouts",
        "renderAllCallouts",
        "data-sol-prc-annotation",
    ):
        assert leftover not in normalized
    assert_no_legacy_annotations(normalized)
