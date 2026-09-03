from __future__ import annotations

import html
import json
import re


class InvalidPrcProofError(ValueError):
    pass


_SLOTS = {
    "email": ("desktop", "mobile"),
    "banner": ("banner",),
    "social": ("social",),
}
_OPEN_TAG = r"<{tag}\b(?:[^>\"']|\"[^\"]*\"|'[^']*')*>"


def _has(source: str, pattern: str) -> bool:
    return re.search(pattern, source, re.IGNORECASE | re.DOTALL) is not None


def _attr_value(tag: str, name: str) -> str | None:
    match = re.search(
        rf"\s{re.escape(name)}(?![\w:-])(?:\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+)))?",
        tag,
        re.IGNORECASE,
    )
    if match is None:
        return None
    return next((value for value in match.groups() if value is not None), "")


def _tag_with_attrs(source: str, tag: str, **attrs: str | None) -> str | None:
    for match in re.finditer(_OPEN_TAG.format(tag=tag), source, re.IGNORECASE):
        opening = match.group(0)
        if all(
            _attr_value(opening, name) is not None and (expected is None or _attr_value(opening, name) == expected)
            for name, expected in attrs.items()
        ):
            return opening
    return None


def _slot_present(source: str, slot: str) -> bool:
    return _tag_with_attrs(source, "iframe", **{"data-sol-prc-creative": slot}) is not None


def _required_slots(source: str, content_type: str) -> bool:
    slots = _SLOTS.get(content_type)
    if slots is None:
        raise InvalidPrcProofError(f"Unsupported PRC content type: {content_type}")
    present = [_slot_present(source, slot) for slot in slots]
    return any(present) if content_type == "email" else all(present)


def _is_legacy_seed(source: str, content_type: str) -> bool:
    if content_type == "banner":
        return all(
            _has(source, pattern)
            for pattern in (
                r'<body\b[^>]*class=["\'][^"\']*\bbanner-proof-doc\b',
                r'<main\b[^>]*class=["\'][^"\']*\bpages\b[\s\S]*data-banner-section',
                r'<script\b[^>]*id=["\']banner-template-data["\'][^>]*type=["\']application/json["\']',
                r'<template\b[^>]*id=["\']frame-template["\'][\s\S]*<iframe\b[^>]*class=["\'][^"\']*\bbanner-frame\b',
                r'data-slot=["\']frames["\']',
            )
        )
    return all(
        (
            _tag_with_attrs(source, "body", **{"data-sol-prc-proof": content_type}),
            _has(source, r"data-sol-prc-pages"),
            _tag_with_attrs(
                source,
                "script",
                type="application/json",
                **{"data-sol-prc-config": None},
            ),
        )
    )


def _validate_base(source: str, content_type: str) -> None:
    if content_type not in _SLOTS:
        raise InvalidPrcProofError(f"Unsupported PRC content type: {content_type}")
    strict = all(
        (
            _tag_with_attrs(source, "meta", name="sol-prc-contract", content="v2"),
            _tag_with_attrs(source, "body", **{"data-sol-prc-proof": content_type}),
            _has(source, r"data-sol-prc-pages[\s\S]*data-sol-prc-page"),
            _tag_with_attrs(source, "script", type="application/json", **{"data-sol-prc-config": None})
            or _tag_with_attrs(source, "script", type="application/json", id="sol-prc-config"),
            _has(source, r"data-sol-prc-field"),
        )
    )
    if not strict and not _is_legacy_seed(source, content_type):
        raise InvalidPrcProofError("PRC template does not satisfy contract v2 or fleet seed anatomy")
    if content_type != "banner" and not _required_slots(source, content_type):
        raise InvalidPrcProofError("PRC template is missing creative slots")


def _set_attr(tag: str, name: str, value: str = "") -> str:
    attr = re.compile(
        rf"\s+{re.escape(name)}(?![\w:-])(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+))?",
        re.IGNORECASE,
    )
    rendered = f' {name}="{html.escape(value, quote=True)}"' if value else f" {name}"
    return f"{attr.sub('', tag)[:-1]}{rendered}>"


def _remove_attr(tag: str, name: str) -> str:
    return re.sub(
        rf"\s+{re.escape(name)}(?![\w:-])(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+))?",
        "",
        tag,
        flags=re.IGNORECASE,
    )


def _set_class(tag: str, class_name: str) -> str:
    match = re.search(r'\sclass\s*=\s*(["\'])(.*?)\1', tag, re.IGNORECASE)
    classes = match.group(2).split() if match else []
    if class_name not in classes:
        classes.append(class_name)
    return _set_attr(tag, "class", " ".join(classes))


def _replace_first_tag(source: str, tag: str, predicate: str, transform) -> str:
    for match in re.finditer(_OPEN_TAG.format(tag=tag), source, re.IGNORECASE):
        opening = match.group(0)
        if re.search(predicate, opening, re.IGNORECASE):
            return f"{source[: match.start()]}{transform(opening)}{source[match.end() :]}"
    return source


def _inject_slot(source: str, slot: str, creative_html: str) -> str:
    found = False

    def transform(tag: str) -> str:
        nonlocal found
        found = True
        return _set_attr(_remove_attr(tag, "src"), "srcdoc", creative_html)

    updated = _replace_first_tag(
        source,
        "iframe",
        rf'data-sol-prc-creative\s*=\s*(["\']){re.escape(slot)}\1',
        transform,
    )
    if not found or html.escape(creative_html, quote=True) not in updated:
        raise InvalidPrcProofError("PRC creative injection failed")
    return updated


def _stamp_contract(source: str, content_type: str) -> str:
    head_bits = []
    if _tag_with_attrs(source, "meta", name="sol-prc-contract", content="v2") is None:
        head_bits.append('<meta name="sol-prc-contract" content="v2">')
    if _tag_with_attrs(source, "meta", name="sol-prc-contract-baked", content="v2") is None:
        head_bits.append('<meta name="sol-prc-contract-baked" content="v2">')
    if _tag_with_attrs(source, "style", id="sol-prc-export-style") is None:
        head_bits.append('<style id="sol-prc-export-style"></style>')
    if head_bits:
        source = re.sub(r"</head\s*>", f"{''.join(head_bits)}</head>", source, count=1, flags=re.IGNORECASE)
    source = _replace_first_tag(
        source,
        "body",
        r".*",
        lambda tag: _set_attr(_set_class(tag, "sol-prc-export"), "data-sol-prc-proof", content_type),
    )
    source = _replace_first_tag(
        source,
        "main",
        r"(?:data-sol-prc-pages|\bclass\s*=\s*[\"'][^\"']*\bpages\b)",
        lambda tag: _set_attr(tag, "data-sol-prc-pages"),
    )
    if not _has(source, r"\bdata-sol-prc-page(?:\s|=)"):
        source = _replace_first_tag(
            source,
            "article",
            r'\bclass\s*=\s*["\'][^"\']*\bpage\b',
            lambda tag: _set_attr(tag, "data-sol-prc-page", "legacy_page"),
        )
        source = _replace_first_tag(
            source,
            "section",
            r'\bclass\s*=\s*["\'][^"\']*\bprc-page\b',
            lambda tag: _set_attr(tag, "data-sol-prc-page", "legacy_page"),
        )
    if not _has(source, r"\bdata-sol-prc-field(?=[\s=>])"):
        source = _replace_first_tag(
            source,
            "div",
            r'\b(?:data-slot\s*=\s*["\']title["\']|class\s*=\s*["\'][^"\']*\bheader-title\b)',
            lambda tag: _set_attr(tag, "data-sol-prc-field", "file_name"),
        )
    if not _has(source, r"\bdata-sol-prc-field(?=[\s=>])"):
        source = re.sub(
            r"</body\s*>",
            '<span hidden data-sol-prc-field="file_name"></span></body>',
            source,
            count=1,
            flags=re.IGNORECASE,
        )
    return source


def _adapt_legacy_banner(source: str, creative_html: str) -> str:
    source = _replace_first_tag(
        source,
        "script",
        r'\bid\s*=\s*["\']banner-template-data["\']',
        lambda tag: _set_attr(tag, "data-sol-prc-config"),
    )

    def frame(tag: str) -> str:
        return _set_attr(_set_attr(tag, "data-sol-prc-creative", "banner"), "srcdoc", creative_html)

    source = _replace_first_tag(source, "iframe", r'\bclass\s*=\s*["\'][^"\']*\bbanner-frame\b', frame)
    payload = json.dumps(creative_html).replace("</", "<\\/")
    script = f'<script id="sol-prc-banner-template-data">window.__BANNER_TEMPLATE_SRCDOC__ = {payload};</script>'
    existing = re.compile(
        r'<script\b[^>]*id=["\']sol-prc-banner-template-data["\'][^>]*>.*?</script\s*>',
        re.IGNORECASE | re.DOTALL,
    )
    if existing.search(source):
        return existing.sub(script, source, count=1)
    return re.sub(r"</head\s*>", f"{script}</head>", source, count=1, flags=re.IGNORECASE)


def validate_prc_proof(source: str, content_type: str) -> None:
    slots = _SLOTS.get(content_type)
    if slots is None:
        raise InvalidPrcProofError(f"Unsupported PRC content type: {content_type}")
    body = _tag_with_attrs(source, "body", **{"data-sol-prc-proof": content_type})
    config = _tag_with_attrs(
        source, "script", type="application/json", **{"data-sol-prc-config": None}
    ) or _tag_with_attrs(source, "script", type="application/json", id="sol-prc-config")
    required = (
        _tag_with_attrs(source, "meta", name="sol-prc-contract", content="v2"),
        body,
        _has(source, r"\bdata-sol-prc-pages(?=[\s=>])"),
        _has(source, r"\bdata-sol-prc-page(?=[\s=>])"),
        config,
        _has(source, r"\bdata-sol-prc-field(?=[\s=>])"),
        _tag_with_attrs(source, "meta", name="sol-prc-contract-baked", content="v2"),
        _tag_with_attrs(source, "style", id="sol-prc-export-style"),
        body is not None and "sol-prc-export" in (_attr_value(body, "class") or "").split(),
    )
    if not all(required):
        raise InvalidPrcProofError("PRC proof does not satisfy baked contract v2")
    present = [slot for slot in slots if _slot_present(source, slot)]
    if not present:
        raise InvalidPrcProofError("PRC template is missing creative slots")
    for slot in present:
        frame = _tag_with_attrs(source, "iframe", **{"data-sol-prc-creative": slot})
        if frame is None or not html.unescape(_attr_value(frame, "srcdoc") or "").strip():
            raise InvalidPrcProofError("PRC proof has an empty creative slot")


def compose_prc_proof(base_html: str, creative_html: str, content_type: str) -> str:
    if not isinstance(base_html, str) or not base_html.strip():
        raise InvalidPrcProofError("PRC proof HTML is required")
    if not isinstance(creative_html, str) or not creative_html.strip():
        raise InvalidPrcProofError("PRC creative HTML is required")
    _validate_base(base_html, content_type)
    proof = _stamp_contract(base_html, content_type)
    if content_type == "banner" and not _required_slots(base_html, content_type):
        proof = _adapt_legacy_banner(proof, creative_html)
    else:
        for slot in _SLOTS[content_type]:
            if _slot_present(base_html, slot):
                proof = _inject_slot(proof, slot, creative_html)
    validate_prc_proof(proof, content_type)
    return proof
