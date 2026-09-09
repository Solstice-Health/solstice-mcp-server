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
_RAW_TEXT_BLOCK = re.compile(
    r"(<(?P<tag>script|style|textarea)\b[^>]*>)([\s\S]*?)(</(?P=tag)\s*>)",
    re.IGNORECASE,
)


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


def _mask_raw_text(source: str) -> str:
    masked: list[str] = []
    cursor = 0
    for match in _RAW_TEXT_BLOCK.finditer(source):
        masked.append(source[cursor: match.start(3)])
        masked.append(" " * len(match.group(3)))
        cursor = match.start(4)
    if cursor == 0:
        return source
    masked.append(source[cursor:])
    return "".join(masked)


def _iter_open_tags(source: str, tag: str):
    masked = _mask_raw_text(source)
    for match in re.finditer(_OPEN_TAG.format(tag=tag), masked, re.IGNORECASE):
        yield match, source[match.start(): match.end()]


def _tag_with_attrs(source: str, tag: str, **attrs: str | None) -> str | None:
    for _match, opening in _iter_open_tags(source, tag):
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
    for match, opening in _iter_open_tags(source, tag):
        if re.search(predicate, opening, re.IGNORECASE):
            return f"{source[: match.start()]}{transform(opening)}{source[match.end() :]}"
    return source


def _replace_all_tags(source: str, tag: str, predicate: str, transform) -> str:
    updated = source
    for match, opening in reversed(list(_iter_open_tags(source, tag))):
        if re.search(predicate, opening, re.IGNORECASE):
            updated = f"{updated[: match.start()]}{transform(opening)}{updated[match.end() :]}"
    return updated


_TEMPLATE_BLOCK = re.compile(
    r"<template\b(?:[^>\"']|\"[^\"]*\"|'[^']*')*>[\s\S]*?</template\s*>",
    re.IGNORECASE,
)


def _template_ranges(source: str) -> list[tuple[int, int]]:
    masked = _mask_raw_text(source)
    return [(match.start(), match.end()) for match in _TEMPLATE_BLOCK.finditer(masked)]


def _in_template(ranges: list[tuple[int, int]], position: int) -> bool:
    return any(start <= position < end for start, end in ranges)


def _live_slot_present(source: str, slot: str) -> bool:
    """Slot iframes inside <template> are inert prototypes, not render targets."""
    ranges = _template_ranges(source)
    for match, opening in _iter_open_tags(source, "iframe"):
        if _in_template(ranges, match.start()):
            continue
        if _attr_value(opening, "data-sol-prc-creative") == slot:
            return True
    return False


def _strip_template_creative_frames(source: str) -> str:
    """Clear creative payloads baked onto <template> prototype frames.

    The runtime clones the prototype and assigns the real srcdoc after
    insertion; Chrome commits a cloned iframe's srcdoc from the value held at
    insertion, so a baked-in stamp wins over the reassignment and every frame
    renders the same whole-creative blob.
    """
    ranges = _template_ranges(source)
    if not ranges:
        return source
    parts: list[str] = []
    cursor = 0
    for match, opening in _iter_open_tags(source, "iframe"):
        if not _in_template(ranges, match.start()):
            continue
        if _attr_value(opening, "data-sol-prc-creative") is None:
            continue
        parts.append(source[cursor : match.start()])
        parts.append(_remove_attr(_remove_attr(opening, "srcdoc"), "src"))
        cursor = match.end()
    if not parts:
        return source
    parts.append(source[cursor:])
    return "".join(parts)


def _inject_slot(source: str, slot: str, creative_html: str, *, replace_all: bool = True) -> str:
    found = False
    ranges = _template_ranges(source)

    def transform(tag: str) -> str:
        nonlocal found
        found = True
        return _set_attr(_remove_attr(tag, "src"), "srcdoc", creative_html)

    predicate = rf'data-sol-prc-creative\s*=\s*(["\']){re.escape(slot)}\1'
    matches = [
        (match, opening)
        for match, opening in _iter_open_tags(source, "iframe")
        if re.search(predicate, opening, re.IGNORECASE) and not _in_template(ranges, match.start())
    ]
    if not replace_all:
        matches = matches[:1]
    updated = source
    for match, opening in reversed(matches):
        updated = f"{updated[: match.start()]}{transform(opening)}{updated[match.end() :]}"
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

    return _replace_first_tag(source, "iframe", r'\bclass\s*=\s*["\'][^"\']*\bbanner-frame\b', frame)


def _canonicalize_l4_config(source: str) -> str:
    if _tag_with_attrs(source, "script", type="application/json", id="sol-prc-config") is not None:
        return source
    for legacy_id in ("prc-cover-data", "banner-template-data"):
        found = False

        def transform(tag: str) -> str:
            nonlocal found
            found = True
            return _set_attr(_set_attr(tag, "id", "sol-prc-config"), "type", "application/json")

        updated = _replace_first_tag(
            source,
            "script",
            rf'\bid\s*=\s*["\']{re.escape(legacy_id)}["\']',
            transform,
        )
        if found:
            return updated
    return source


# Creative-derived globals the banner hydrator reads. It prefers these over the
# iframe `srcdoc`, and the per-banner arrays outrank the single global.
_BANNER_CREATIVE_GLOBALS = (
    "__BANNER_TEMPLATE_SRCDOC__",
    "__BANNER_TEMPLATE_SRCDOCS__",
    "__BANNER_TEMPLATE_SRCDOC_ADCHOICES__",
    "__BANNER_TEMPLATE_SRCDOCS_ADCHOICES__",
    "__BANNER_TEMPLATE_EXPANDED_SRCDOC__",
    "__BANNER_TEMPLATE_EXPANDED_SRCDOCS__",
)

# Matched by literal shape, not by line: the payload script is often emitted on
# a single line together with its `<script>` tag.
_JS_STRING = r'"(?:[^"\\]|\\.)*"'
_JS_ARRAY = rf"\[\s*(?:(?:{_JS_STRING}|null)\s*,\s*)*(?:{_JS_STRING}|null)?\s*\]"

_STALE_BANNER_GLOBAL = re.compile(
    rf"[^\S\n]*window\.(?:{'|'.join(_BANNER_CREATIVE_GLOBALS)})\s*=\s*(?:{_JS_STRING}|{_JS_ARRAY}|null)\s*;\n?"
)

_BANNER_PAYLOAD_SCRIPT = re.compile(
    r'(<script\b[^>]*id=["\']sol-prc-banner-template-data["\'][^>]*>)(.*?)(</script\s*>)',
    re.IGNORECASE | re.DOTALL,
)
_BANNER_EXPANDED_GLOBALS = (
    "__BANNER_TEMPLATE_EXPANDED_SRCDOC__",
    "__BANNER_TEMPLATE_EXPANDED_SRCDOCS__",
)
_BANNER_SPLIT_RE = re.compile(r"(?=<!DOCTYPE\s+html>)", re.IGNORECASE)


def _json_inline(value: object) -> str:
    return json.dumps(value).replace("</", "<\\/")


def _decode_banner_payload_value(raw: str) -> object:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidPrcProofError("expanded banner ISI payload unreadable; clear and rebake") from exc


def _read_js_assignment_value(script_body: str, assignment_end: int) -> str:
    i = assignment_end
    length = len(script_body)
    while i < length and script_body[i].isspace():
        i += 1
    if i >= length:
        raise InvalidPrcProofError("expanded banner ISI payload unreadable; clear and rebake")

    if script_body.startswith("null", i):
        j = i + 4
    elif script_body[i] == '"':
        j = i + 1
        escaped = False
        while j < length:
            ch = script_body[j]
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                j += 1
                break
            j += 1
        else:
            raise InvalidPrcProofError("expanded banner ISI payload unreadable; clear and rebake")
    elif script_body[i] == "[":
        j = i
        depth = 0
        in_string = False
        escaped = False
        while j < length:
            ch = script_body[j]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
            j += 1
        if depth != 0:
            raise InvalidPrcProofError("expanded banner ISI payload unreadable; clear and rebake")
    else:
        raise InvalidPrcProofError("expanded banner ISI payload unreadable; clear and rebake")

    while j < length and script_body[j].isspace():
        j += 1
    if j >= length or script_body[j] != ";":
        raise InvalidPrcProofError("expanded banner ISI payload unreadable; clear and rebake")
    return script_body[i:j]


def _split_banner_docs(creative_html: str) -> list[str]:
    parts = [part.strip() for part in _BANNER_SPLIT_RE.split(creative_html) if part.strip()]
    return parts if len(parts) > 1 else [creative_html]


def _extract_payload_assignments(script_body: str) -> dict[str, object]:
    found: dict[str, object] = {}
    for name in _BANNER_CREATIVE_GLOBALS:
        match = re.search(rf"window\.{name}\s*=", script_body)
        if not match:
            continue
        found[name] = _decode_banner_payload_value(_read_js_assignment_value(script_body, match.end()))
    for name in ("__BANNER_TEMPLATE_SRCDOC__", "__BANNER_TEMPLATE_SRCDOCS__", *_BANNER_EXPANDED_GLOBALS):
        if re.search(rf"window\.{name}\s*=", script_body) and name not in found:
            raise InvalidPrcProofError("expanded banner ISI payload unreadable; clear and rebake")
    return found


def _expanded_assignments_to_preserve(script_body: str, creative_html: str) -> list[tuple[str, object]]:
    if not any(re.search(rf"window\.{name}\s*=", script_body) for name in _BANNER_EXPANDED_GLOBALS):
        return []
    assignments = _extract_payload_assignments(script_body)
    split_docs = _split_banner_docs(creative_html)
    keep: list[tuple[str, object]] = []

    if re.search(r"window\.__BANNER_TEMPLATE_EXPANDED_SRCDOC__\s*=", script_body):
        if assignments.get("__BANNER_TEMPLATE_SRCDOC__") != creative_html:
            raise InvalidPrcProofError("expanded banner ISI payload is stale; clear and rebake")
        keep.append(("__BANNER_TEMPLATE_EXPANDED_SRCDOC__", assignments["__BANNER_TEMPLATE_EXPANDED_SRCDOC__"]))

    if re.search(r"window\.__BANNER_TEMPLATE_EXPANDED_SRCDOCS__\s*=", script_body):
        if assignments.get("__BANNER_TEMPLATE_SRCDOCS__") != split_docs:
            raise InvalidPrcProofError("expanded banner ISI payload is stale; clear and rebake")
        keep.append(("__BANNER_TEMPLATE_EXPANDED_SRCDOCS__", assignments["__BANNER_TEMPLATE_EXPANDED_SRCDOCS__"]))

    return keep


def _collect_expanded_assignments(source: str, creative_html: str) -> list[tuple[str, object]]:
    preserved: list[tuple[str, object]] = []
    for match in _BANNER_PAYLOAD_SCRIPT.finditer(source):
        for name, value in _expanded_assignments_to_preserve(match.group(2), creative_html):
            if any(existing_name == name for existing_name, _ in preserved):
                raise InvalidPrcProofError("expanded banner ISI payload has conflicting sources; clear and rebake")
            preserved.append((name, value))
    return preserved


def _set_banner_srcdoc_payload(source: str, creative_html: str) -> str:
    """Republish the banner creative globals against `creative_html`.

    Every creative-derived assignment has to go, not just the single global:
    the AdChoices and per-banner variants outrank it, so leaving one behind
    renders the previous creative. AdChoices falls back to the raw creative;
    a dropped expanded-ISI payload renders blank rather than stale safety copy.
    Non-creative publish flags in the same script are preserved.
    """
    raw_assignments = [f"window.__BANNER_TEMPLATE_SRCDOC__ = {_json_inline(creative_html)};"]
    split_docs = _split_banner_docs(creative_html)
    if len(split_docs) > 1:
        raw_assignments.append(f"window.__BANNER_TEMPLATE_SRCDOCS__ = {_json_inline(split_docs)};")
    preserved_expanded = _collect_expanded_assignments(source, creative_html)
    # Document-wide: a proof can carry more than one payload script, and any
    # surviving assignment outranks the one published here.
    source = _STALE_BANNER_GLOBAL.sub("", source)
    match = _BANNER_PAYLOAD_SCRIPT.search(source)
    if match:
        kept = match.group(2).strip()
        assignments = list(raw_assignments)
        assignments.extend(f"window.{name} = {_json_inline(value)};" for name, value in preserved_expanded)
        body = f"{kept}\n{'\n'.join(assignments)}" if kept else "\n".join(assignments)
        return f"{source[: match.start()]}{match.group(1)}{body}{match.group(3)}{source[match.end() :]}"
    assignments = list(raw_assignments)
    assignments.extend(f"window.{name} = {_json_inline(value)};" for name, value in preserved_expanded)
    script = f'<script id="sol-prc-banner-template-data">{"\n".join(assignments)}</script>'
    # Callable repl: json.dumps emits \uXXXX; a string repl would parse those as regex escapes.
    return re.sub(r"</head\s*>", lambda match: f"{script}{match.group(0)}", source, count=1, flags=re.I)


def validate_prc_proof(source: str, content_type: str) -> None:
    slots = _SLOTS.get(content_type)
    if slots is None:
        raise InvalidPrcProofError(f"Unsupported PRC content type: {content_type}")
    body = _tag_with_attrs(source, "body", **{"data-sol-prc-proof": content_type})
    config = _tag_with_attrs(source, "script", type="application/json", id="sol-prc-config")
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
    ranges = _template_ranges(source)
    for slot in present:
        # Banner creative rides the payload script; the frame-template prototype stays bare.
        if content_type == "banner":
            if not _has(source, r'window\.__BANNER_TEMPLATE_SRCDOC__\s*=\s*"(?:[^"\\]|\\.)+"'):
                raise InvalidPrcProofError("PRC proof has an empty creative slot")
            continue
        frames = [
            opening
            for match, opening in _iter_open_tags(source, "iframe")
            if _attr_value(opening, "data-sol-prc-creative") == slot
            and not _in_template(ranges, match.start())
        ]
        if not frames:
            raise InvalidPrcProofError("PRC proof has an empty creative slot")
        for frame in frames:
            if not html.unescape(_attr_value(frame, "srcdoc") or "").strip():
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
            if _live_slot_present(base_html, slot):
                proof = _inject_slot(proof, slot, creative_html, replace_all=content_type != "banner")
        proof = _strip_template_creative_frames(proof)
    if content_type == "banner":
        proof = _set_banner_srcdoc_payload(proof, creative_html)
    proof = _canonicalize_l4_config(proof)
    validate_prc_proof(proof, content_type)
    return proof
