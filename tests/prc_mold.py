"""SOL-3398: a banner/social mold (`#frame-template` iframe) must stay empty."""

from __future__ import annotations

import html
import re
from typing import NamedTuple

_FRAME_TEMPLATE = re.compile(
    r'<template\b[^>]*\bid=["\']frame-template["\'][^>]*>([\s\S]*?)</template>',
    re.IGNORECASE,
)
_IFRAME_OPEN = re.compile(r"<iframe\b([^>]*)>", re.IGNORECASE)
_SRCDOC = re.compile(r"""\ssrcdoc\s*=\s*(["'])([\s\S]*?)\1""", re.IGNORECASE)
_PROOF_TYPE = re.compile(r"""\bdata-sol-prc-proof=["']([^"']+)["']""", re.IGNORECASE)
_BANNER_PAYLOAD = re.compile(r"window\.__BANNER_TEMPLATE_SRCDOCS?__\s*=")
_CREATIVE_DOC = re.compile(r"<!doctype|<html[\s>]|__BANNER_TEMPLATE_", re.IGNORECASE)


class PrcMoldInspection(NamedTuple):
    content_type: str | None
    has_frame_template: bool
    mold_srcdoc: str
    mold_stamped: bool
    has_banner_payload: bool


def inspect_prc_mold(source: str) -> PrcMoldInspection:
    template = _FRAME_TEMPLATE.search(source)
    template_body = template.group(1) if template else ""
    iframe = _IFRAME_OPEN.search(template_body)
    srcdoc = _SRCDOC.search(iframe.group(1)) if iframe else None
    mold_srcdoc = html.unescape(srcdoc.group(2) if srcdoc else "").strip()
    proof = _PROOF_TYPE.search(source)
    return PrcMoldInspection(
        content_type=proof.group(1) if proof else None,
        has_frame_template=bool(template_body),
        mold_srcdoc=mold_srcdoc,
        mold_stamped=bool(_CREATIVE_DOC.search(mold_srcdoc) or len(mold_srcdoc) > 2_000),
        has_banner_payload=bool(_BANNER_PAYLOAD.search(source)),
    )


def assert_healthy_banner_mold(source: str) -> PrcMoldInspection:
    inspection = inspect_prc_mold(source)
    if inspection.has_frame_template and inspection.mold_stamped:
        raise AssertionError("SOL-3398: #frame-template mold is stamped with the stacked creative")
    if inspection.content_type == "banner" and not inspection.has_banner_payload:
        raise AssertionError("Banner proof is missing window.__BANNER_TEMPLATE_SRCDOC(S)__")
    return inspection
