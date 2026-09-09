from __future__ import annotations

import pytest
from prc_mold import assert_healthy_banner_mold, inspect_prc_mold
from test_prc_templates import BANNER_TEMPLATE, CREATIVE

from solstice_mcp.prc_proof_composer import compose_prc_proof

EMPTY_MOLD = (
    '<!doctype html><html><body data-sol-prc-proof="banner">'
    '<template id="frame-template">'
    '<iframe class="banner-frame" data-sol-prc-creative="banner"></iframe>'
    "</template>"
    '<script id="sol-prc-banner-template-data">'
    'window.__BANNER_TEMPLATE_SRCDOC__ = "<html>slice</html>";'
    "</script></body></html>"
)
STAMPED_MOLD = (
    '<!doctype html><html><body data-sol-prc-proof="banner">'
    '<template id="frame-template">'
    '<iframe class="banner-frame" data-sol-prc-creative="banner" '
    'srcdoc="&lt;!doctype html&gt;&lt;html&gt;&lt;body&gt;300x250&lt;/body&gt;&lt;/html&gt;">'
    "</iframe></template>"
    '<script id="sol-prc-banner-template-data">window.__BANNER_TEMPLATE_SRCDOCS__ = [];</script>'
    "</body></html>"
)
V2_BANNER_WITH_MOLD = (
    '<!doctype html><html><head><meta name="sol-prc-contract" content="v2" data-profile="banner">'
    '<style id="sol-prc-export-style"></style></head>'
    '<body class="sol-prc-export" data-sol-prc-proof="banner"><main data-sol-prc-pages>'
    '<section data-sol-prc-page="storyboard" data-banner-section>'
    '<div data-sol-prc-field="file_name">KEEP</div>'
    '<template id="frame-template">'
    '<iframe class="banner-frame" data-sol-prc-creative="banner"></iframe>'
    "</template>"
    '<div data-slot="frames"></div></section>'
    '<script id="sol-prc-config" type="application/json">{}</script>'
    "</main></body></html>"
)


def test_inspect_accepts_empty_mold_with_payload():
    inspection = inspect_prc_mold(EMPTY_MOLD)
    assert inspection.content_type == "banner"
    assert inspection.has_frame_template
    assert not inspection.mold_stamped
    assert inspection.has_banner_payload
    assert_healthy_banner_mold(EMPTY_MOLD)


def test_inspect_flags_stamped_mold():
    assert inspect_prc_mold(STAMPED_MOLD).mold_stamped
    with pytest.raises(AssertionError, match="SOL-3398"):
        assert_healthy_banner_mold(STAMPED_MOLD)


def test_compose_leaves_v2_banner_mold_empty():
    proof = compose_prc_proof(V2_BANNER_WITH_MOLD, CREATIVE, "banner")
    assert_healthy_banner_mold(proof)


def test_legacy_banner_compose_has_payload():
    proof = compose_prc_proof(BANNER_TEMPLATE, CREATIVE, "banner")
    assert inspect_prc_mold(proof).has_banner_payload or "NEW CREATIVE" in proof
