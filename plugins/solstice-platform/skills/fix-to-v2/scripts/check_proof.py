#!/usr/bin/env python3
"""Classify a Solstice PRC proof body as Contract v2 or legacy.

Usage: python3 check_proof.py <proof.html>  (or pipe the body on stdin)

Prints one verdict line — V2 or LEGACY — followed by reason lines.
Exit code 0 for V2, 1 for LEGACY, 2 on usage error.

Mirrors the platform's own gate: the v2 + baked meta markers are the
authoritative positive signal; the legacy scan checks DOM elements and
script/style text but skips platform-owned blocks (any script/style carrying
a data-sol-prc-* attribute or a sol-prc-* id), whose viewer JavaScript
legitimately names legacy class hooks.
"""

import re
import sys
from html.parser import HTMLParser

LEGACY_CLASSES = {
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
    "prc-render-stage",
}
LEGACY_CODE = re.compile(
    r"__prc_annotation_positions|generatedAnnotationPositions"
    r"|layout(?:Alexion)?Stage"
    r"|prc-(?:callout-gutter|connector-svg|render-stage)"
    r"|callout-(?:overlay|box|line|dot)"
    r"|data-sol-prc-annotation"
)


class ProofScan(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta_v2 = False
        self.meta_baked = False
        self.legacy_dom: list[str] = []
        self.legacy_code: list[str] = []
        self._skip_depth = 0  # inside a platform-owned script/style
        self._raw_tag: str | None = None
        self._raw_chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        names = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            if names.get("name") == "sol-prc-contract" and names.get("content") == "v2":
                self.meta_v2 = True
            if names.get("name") == "sol-prc-contract-baked" and names.get("content") == "v2":
                self.meta_baked = True
        classes = set(names.get("class", "").split())
        for hit in sorted(classes & LEGACY_CLASSES):
            self.legacy_dom.append(f"<{tag}> class '{hit}'")
        if any(k.startswith("data-sol-prc-annotation") for k in names):
            self.legacy_dom.append(f"<{tag}> data-sol-prc-annotation*")
        if tag in ("script", "style"):
            if self._skip_depth or self._raw_tag is not None:
                self._skip_depth += 1  # nested raw-text block: treat as owned
            elif names.get("id", "").startswith("sol-prc-") or any(
                k.startswith("data-sol-prc-") for k in names
            ):
                self._skip_depth = 1  # platform-owned block: ignore its text
            else:
                self._raw_tag = tag
                self._raw_chunks = []

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag in ("script", "style"):
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag not in ("script", "style"):
            return
        if self._skip_depth:
            self._skip_depth -= 1
        elif self._raw_tag == tag:
            body = "".join(self._raw_chunks)
            seen = set()
            for match in LEGACY_CODE.finditer(body):
                if match.group(0) not in seen:
                    seen.add(match.group(0))
                    self.legacy_code.append(f"<{tag}> contains '{match.group(0)}'")
            self._raw_tag = None
            self._raw_chunks = []

    def handle_data(self, data):
        if self._raw_tag and not self._skip_depth:
            self._raw_chunks.append(data)


def classify(text: str) -> tuple[bool, list[str]]:
    scan = ProofScan()
    scan.feed(text)
    scan.close()
    reasons: list[str] = []
    if not scan.meta_v2:
        reasons.append('missing <meta name="sol-prc-contract" content="v2">')
    if not scan.meta_baked:
        reasons.append('missing <meta name="sol-prc-contract-baked" content="v2">')
    reasons.extend(f"legacy DOM: {hit}" for hit in scan.legacy_dom[:5])
    reasons.extend(f"legacy code: {hit}" for hit in scan.legacy_code[:5])
    ok = scan.meta_v2 and scan.meta_baked and not scan.legacy_dom and not scan.legacy_code
    return ok, reasons


def main() -> int:
    if len(sys.argv) > 2:
        sys.stderr.write("usage: check_proof.py <proof.html>\n")
        return 2
    if len(sys.argv) == 2:
        with open(sys.argv[1], encoding="utf-8") as handle:
            text = handle.read()
    else:
        text = sys.stdin.read()
    head = text[:2000].lower()
    if "<html" not in head and "<!doctype html" not in head:
        # A presigned-URL failure downloads an S3 XML error body, not HTML;
        # classifying it would report a bogus LEGACY and trigger a needless write.
        print("INVALID")
        print("  - input is not an HTML document (S3 error body? re-fetch a fresh URL)")
        return 2
    ok, reasons = classify(text)
    print("V2" if ok else "LEGACY")
    for reason in reasons:
        print(f"  - {reason}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
