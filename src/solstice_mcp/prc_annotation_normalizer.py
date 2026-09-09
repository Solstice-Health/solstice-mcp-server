from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser


class LegacyAnnotationFormatError(ValueError):
    pass


_PLATFORM_RUNTIME_IDS = {
    "sol-prc-proof-engine",
    "sol-prc-proof-engine-config",
    "sol-prc-field-engine",
    "sol-prc-field-engine-config",
    "sol-prc-annotation-engine-v2",
    "sol-prc-annotation-engine-v2-config",
    "sol-prc-annotation-engine-v2-style",
    "sol-prc-template-runtime",
    "sol-prc-template-runtime-style",
    "sol-prc-export-style",
}
_CANONICAL_POSITIONS_ID = "sol-prc-annotation-positions"
_V2_OVERLAY_CLASS = "sol-prc-annotation-overlay-v2"
_LEGACY_REMOVE_CLASSES = {
    "prc-callout",
    "prc-connector-line",
    "prc-callout-gutter",
    "prc-alexion-gutter",
    "prc-connector-svg",
    "prc-alexion-svg",
    "callout-overlay",
    "callout-box",
    "callout-line",
    "callout-dot",
}
_LEGACY_UNWRAP_CLASSES = {"prc-render-stage"}
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_RAW_TEXT_BLOCK = re.compile(
    r"(<(?P<tag>script|style)\b(?P<attrs>[^>]*)>)(?P<body>[\s\S]*?)(</(?P=tag)\s*>)",
    re.IGNORECASE,
)
_LEGACY_SOURCE_RE = re.compile(
    r"__prc_annotation_positions|generatedAnnotationPositions|"
    r"layout(?:Alexion)?Stage|prc-(?:callout-gutter|connector-svg|render-stage)|"
    r"callout-(?:overlay|box|line|dot)|data-sol-prc-annotation",
    re.IGNORECASE,
)
_LEGACY_SELECTOR_RE = re.compile(
    r"(?:^|[.#\s])(?:prc-callout|prc-connector-line|prc-callout-gutter|"
    r"prc-alexion-gutter|prc-connector-svg|prc-alexion-svg|callout-overlay|"
    r"callout-box|callout-line|callout-dot|prc-render-stage)(?:\b|[-.#:\s])|"
    r"\[data-sol-prc-annotation(?:[=\]\s-])",
    re.IGNORECASE,
)
_DEDICATED_ENGINE_SIGNATURES = (
    ("layoutStage", "prc-callout-gutter", "prc-connector-svg"),
    ("layoutAlexionStage", "prc-alexion-gutter", "prc-alexion-svg"),
)
_LEGACY_POSITION_SCRIPT = re.compile(
    r"^\s*(?:(?:var|let|const)\s+|window\.)?"
    r"(?:__prc_annotation_positions|generatedAnnotationPositions)\s*=\s*[\s\S]*?;\s*$",
    re.IGNORECASE,
)
_SANOFI_ENGINE_MARKER = "Hyperlink-destination annotation engine (self-contained)."
_SANOFI_BOOTSTRAP_MARKER = "/* ---- Bootstrap: iterate every banner section and hydrate ----"
_SANOFI_REDRAW_TIMER = re.compile(
    r"^[^\S\n]*setTimeout\(function\s*\(\)\s*\{\s*renderPageCallouts\([^;]*\);\s*\},\s*\d+\);[^\S\n]*\n?",
    re.MULTILINE,
)
_SANOFI_REDRAW_CALL = re.compile(
    r"^[^\S\n]*renderPageCallouts\([^;\n]*\);[^\S\n]*\n?",
    re.MULTILINE,
)
_SANOFI_CLONE_OVERLAY = re.compile(
    r"^[^\S\n]*var\s+overlay\s*=\s*clone\.querySelector\([\"']\.callout-overlay[\"']\);[^\S\n]*\n?"
    r"^[^\S\n]*if\s*\(overlay\)\s*overlay\.innerHTML\s*=\s*[\"'][\"'];[^\S\n]*\n?",
    re.MULTILINE,
)


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str | None]
    start: int
    open_end: int
    parent: int | None
    close_start: int | None = None
    end: int | None = None

    @property
    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())


@dataclass(frozen=True)
class _Edit:
    start: int
    end: int
    replacement: str = ""


class _SourceParser(HTMLParser):
    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source = source
        self.line_starts = [0]
        self.line_starts.extend(match.end() for match in re.finditer(r"\n", source))
        self.nodes: list[_Node] = []
        self.stack: list[int] = []

    def _offset(self) -> int:
        line, column = self.getpos()
        return self.line_starts[line - 1] + column

    def _record(self, tag: str, attrs: list[tuple[str, str | None]], *, push: bool) -> None:
        opening = self.get_starttag_text()
        if opening is None:
            return
        normalized_tag = tag.lower()
        start = self._offset()
        node = _Node(
            tag=normalized_tag,
            attrs={name.lower(): value for name, value in attrs},
            start=start,
            open_end=start + len(opening),
            parent=self.stack[-1] if self.stack else None,
        )
        self.nodes.append(node)
        if push and normalized_tag not in _VOID_TAGS:
            self.stack.append(len(self.nodes) - 1)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._record(tag, attrs, push=True)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._record(tag, attrs, push=False)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        for stack_offset in range(len(self.stack) - 1, -1, -1):
            node_index = self.stack[stack_offset]
            node = self.nodes[node_index]
            if node.tag != normalized_tag:
                continue
            start = self._offset()
            close_end = self.source.find(">", start)
            if close_end >= 0:
                node.close_start = start
                node.end = close_end + 1
            del self.stack[stack_offset:]
            return


def _parse(source: str) -> list[_Node]:
    parser = _SourceParser(source)
    parser.feed(source)
    parser.close()
    return parser.nodes


def _apply_edits(source: str, edits: list[_Edit]) -> str:
    updated = source
    for edit in sorted(edits, key=lambda item: item.start, reverse=True):
        updated = f"{updated[: edit.start]}{edit.replacement}{updated[edit.end :]}"
    return updated


def _is_platform_owned(nodes: list[_Node], index: int) -> bool:
    current: int | None = index
    while current is not None:
        node = nodes[current]
        # Frontend MIGRATION detection removes this overlay before checking
        # legacy DOM. Runtime IDs are ignored only for style/script source,
        # not as a blanket exemption for arbitrary descendant elements.
        if _V2_OVERLAY_CLASS in node.classes:
            return True
        current = node.parent
    return False


def _legacy_dom_kind(node: _Node) -> str | None:
    if node.classes & _LEGACY_REMOVE_CLASSES or "data-sol-prc-annotation" in node.attrs:
        return "remove"
    if node.classes & _LEGACY_UNWRAP_CLASSES:
        return "unwrap"
    return None


def _normalize_dom(source: str) -> str:
    nodes = _parse(source)
    selected_subtrees: set[int] = set()
    edits: list[_Edit] = []
    for index, node in enumerate(nodes):
        if _is_platform_owned(nodes, index):
            continue
        ancestor = node.parent
        if any(ancestor == selected or _is_descendant(nodes, ancestor, selected) for selected in selected_subtrees):
            continue
        kind = _legacy_dom_kind(node)
        if kind == "remove":
            selected_subtrees.add(index)
            edits.append(_Edit(node.start, node.end or node.open_end))
        elif kind == "unwrap":
            edits.append(_Edit(node.start, node.open_end))
            if node.close_start is not None and node.end is not None:
                edits.append(_Edit(node.close_start, node.end))
    return _apply_edits(source, edits)


def _is_descendant(nodes: list[_Node], index: int | None, ancestor: int) -> bool:
    current = index
    while current is not None:
        if current == ancestor:
            return True
        current = nodes[current].parent
    return False


def _find_open_brace(source: str, start: int) -> int | None:
    quote: str | None = None
    escaped = False
    in_comment = False
    index = start
    while index < len(source):
        if in_comment:
            if source.startswith("*/", index):
                in_comment = False
                index += 2
                continue
        elif quote is not None:
            if escaped:
                escaped = False
            elif source[index] == "\\":
                escaped = True
            elif source[index] == quote:
                quote = None
        elif source.startswith("/*", index):
            in_comment = True
            index += 2
            continue
        elif source[index] in {'"', "'"}:
            quote = source[index]
        elif source[index] == "{":
            return index
        index += 1
    return None


def _matching_brace(source: str, opening: int) -> int | None:
    depth = 0
    quote: str | None = None
    escaped = False
    in_comment = False
    index = opening
    while index < len(source):
        if in_comment:
            if source.startswith("*/", index):
                in_comment = False
                index += 2
                continue
        elif quote is not None:
            if escaped:
                escaped = False
            elif source[index] == "\\":
                escaped = True
            elif source[index] == quote:
                quote = None
        elif source.startswith("/*", index):
            in_comment = True
            index += 2
            continue
        elif source[index] in {'"', "'"}:
            quote = source[index]
        elif source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _strip_legacy_css_rules(css: str) -> str:
    chunks: list[str] = []
    cursor = 0
    while cursor < len(css):
        opening = _find_open_brace(css, cursor)
        if opening is None:
            chunks.append(css[cursor:])
            break
        closing = _matching_brace(css, opening)
        if closing is None:
            chunks.append(css[cursor:])
            break
        prelude = css[cursor:opening]
        selector = re.sub(r"/\*[\s\S]*?\*/", "", prelude).strip()
        if selector.startswith("@"):
            nested = _strip_legacy_css_rules(css[opening + 1 : closing])
            chunks.append(f"{prelude}{{{nested}}}")
        elif not _LEGACY_SELECTOR_RE.search(selector):
            chunks.append(css[cursor : closing + 1])
        cursor = closing + 1
    cleaned = "".join(chunks)
    return re.sub(
        r"/\*[\s\S]*?\*/",
        lambda match: "" if _LEGACY_SOURCE_RE.search(match.group(0)) else match.group(0),
        cleaned,
    )


def _attr_value(attrs: str, name: str) -> str | None:
    match = re.search(
        rf"\b{re.escape(name)}(?![\w:-])(?:\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+)))?",
        attrs,
        re.IGNORECASE,
    )
    if match is None:
        return None
    return next((value for value in match.groups() if value is not None), "")


def _raw_block_is_ignored(tag: str, attrs: str) -> bool:
    element_id = _attr_value(attrs, "id")
    return bool(
        element_id in _PLATFORM_RUNTIME_IDS
        or element_id == _CANONICAL_POSITIONS_ID
        or (tag.lower() == "script" and _attr_value(attrs, "data-sol-prc-standalone-frame-fit") is not None)
    )


def _strip_sanofi_banner_engine(script: str) -> tuple[str, bool]:
    marker = script.find(_SANOFI_ENGINE_MARKER)
    if marker < 0:
        return script, False
    bootstrap = script.find(_SANOFI_BOOTSTRAP_MARKER, marker)
    if bootstrap < 0:
        return script, False
    section_start = script.rfind("/*", 0, marker)
    if section_start < 0:
        return script, False
    updated = f"{script[:section_start]}{script[bootstrap:]}"
    updated = _SANOFI_REDRAW_TIMER.sub("", updated)
    updated = _SANOFI_REDRAW_CALL.sub("", updated)
    updated = _SANOFI_CLONE_OVERLAY.sub("", updated)
    return updated, True


def _normalize_raw_text(source: str) -> str:
    edits: list[_Edit] = []
    for match in _RAW_TEXT_BLOCK.finditer(source):
        tag = match.group("tag").lower()
        attrs = match.group("attrs")
        body = match.group("body")
        if _raw_block_is_ignored(tag, attrs):
            continue
        if tag == "style":
            cleaned = _strip_legacy_css_rules(body)
            if cleaned != body:
                edits.append(_Edit(match.start("body"), match.end("body"), cleaned))
            continue
        cleaned, stripped_sanofi_engine = _strip_sanofi_banner_engine(body)
        if stripped_sanofi_engine:
            edits.append(_Edit(match.start("body"), match.end("body"), cleaned))
            continue
        if any(all(token in body for token in signature) for signature in _DEDICATED_ENGINE_SIGNATURES) or (
            _LEGACY_POSITION_SCRIPT.fullmatch(body)
        ):
            edits.append(_Edit(match.start(), match.end()))
    return _apply_edits(source, edits)


def assert_no_legacy_annotations(source: str) -> None:
    nodes = _parse(source)
    for index, node in enumerate(nodes):
        if not _is_platform_owned(nodes, index) and _legacy_dom_kind(node) is not None:
            raise LegacyAnnotationFormatError("legacy annotation DOM remains")
    for match in _RAW_TEXT_BLOCK.finditer(source):
        if _raw_block_is_ignored(match.group("tag"), match.group("attrs")):
            continue
        if _LEGACY_SOURCE_RE.search(match.group("body")):
            raise LegacyAnnotationFormatError("legacy annotation source remains")


def normalize_legacy_annotations(source: str, content_type: str) -> str:
    if content_type not in {"email", "banner", "social"}:
        raise LegacyAnnotationFormatError(f"unsupported PRC content type: {content_type}")
    normalized = _normalize_raw_text(_normalize_dom(source))
    assert_no_legacy_annotations(normalized)
    return normalized
