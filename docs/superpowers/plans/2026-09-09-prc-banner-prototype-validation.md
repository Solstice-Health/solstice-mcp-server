# MCP PRC Contract v2 Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every MCP-composed email, banner, and social proof pass the frontend Contract v2 check, including annotation migration, while preserving creative content and banner hydration.

**Architecture:** Keep proof composition source-preserving. Add a focused `prc_annotation_normalizer` module that removes recognized legacy annotation chrome and validates the same MIGRATION residue the frontend checks; unknown mixed JavaScript fails closed. Call it from the existing composer before strict final validation, with a narrow pre-compose allowance for supplied proofs that are structurally baked but still need annotation migration.

**Tech Stack:** Python 3.12, standard-library `html.parser.HTMLParser`, pytest, Ruff, mypy, Docker, local MCP JSON-RPC, frontend Vitest.

## Global Constraints

- No new runtime dependency or lockfile change.
- Supported profiles are exactly `email`, `banner`, and `social`.
- Decoded creative slot/payload HTML remains equal to the input; creative links
  and `href` values remain unchanged.
- Preserve platform-owned Contract v2 runtime and `script#sol-prc-annotation-positions`.
- Unknown mixed scripts with residual legacy annotation tokens fail closed.
- Keep banner prototypes bare and creative payloads in `window.__BANNER_TEMPLATE_SRCDOC__`.
- Do not change Backend-Server or Solstice-Frontend production code.
- Sandbox writes are append-only and restricted to `sanofi_sandbox`.
- Work remains on `fix/mcp-prc-banner-prototype`, based on local MCP `main`; do not depend on Alex's PR.

---

### Task 1: Preserve the completed prototype and semantic-contract fixes

**Files:**
- Existing: `src/solstice_mcp/prc_proof_composer.py:1-627`
- Existing tests: `tests/test_prc_templates.py:100-230`
- Existing integration test: `tests/test_versions.py`

**Interfaces:**
- Produces: `_PrcStructureParser(HTMLParser)`
- Produces: `compose_prc_proof(base_html: str, creative_html: str, content_type: str) -> str`
- Produces: `validate_prc_proof(source: str, content_type: str) -> None`

- [x] Add semantic element-attribute inspection so comments and raw-text blocks cannot satisfy Contract v2.
- [x] Keep `template#frame-template` and `template#isi-region-template` banner iframes free of `src` and `srcdoc`.
- [x] Require banner creative payload data and reject empty live banner frames.
- [x] Run the focused tests, Ruff, Ruff format, and mypy.
- [x] Commit as `c33ae51 fix: preserve banner PRC prototypes`.

### Task 2: Add failing cross-profile annotation migration tests

**Files:**
- Create: `tests/test_prc_annotation_normalizer.py`
- Modify: `tests/test_prc_templates.py`

**Interfaces:**
- Consumes: `compose_prc_proof(base_html: str, creative_html: str, content_type: str) -> str`
- Produces coverage for `normalize_legacy_annotations(source: str, content_type: str) -> str`
- Produces coverage for `assert_no_legacy_annotations(source: str) -> None`

- [ ] **Step 1: Write profile-independent failing tests**

Use a minimal valid proof factory and parameterize `email`, `banner`, and
`social`. Add legacy DOM, CSS, a dedicated engine script, and legacy position
data outside creative slots:

```python
@pytest.mark.parametrize("content_type", ["email", "banner", "social"])
def test_compose_removes_legacy_annotation_format(content_type):
    base = contract_base(content_type).replace(
        "</body>",
        '<div class="callout-overlay"><div class="callout-box">old</div></div>'
        "<style>.callout-line{stroke:red}.layout-kept{display:block}</style>"
        "<script>function layoutStage(){};"
        'document.querySelector(".prc-callout-gutter");'
        'document.querySelector(".prc-connector-svg");</script>'
        "<script>window.__prc_annotation_positions = {old:{x:1}};</script>"
        "</body>",
    )
    proof = compose_prc_proof(base, CREATIVE_WITH_LINK, content_type)
    assert "https://example.test/landing" in proof
    assert ".layout-kept{display:block}" in proof
    assert_no_legacy_annotations(proof)
```

- [ ] **Step 2: Add preservation and fail-closed tests**

```python
def test_normalizer_preserves_canonical_v2_positions_and_platform_runtime():
    source = contract_base("email").replace(
        "</body>",
        '<script id="sol-prc-annotation-positions" type="application/json">{"keep":{"x":4}}</script>'
        '<script id="sol-prc-template-runtime">el.className="callout-box";</script></body>',
    )
    assert normalize_legacy_annotations(source, "email") == source


def test_normalizer_rejects_unrecognized_mixed_legacy_script():
    source = contract_base("social").replace(
        "</body>",
        '<script>hydrateSocial(); el.className="callout-box";</script></body>',
    )
    with pytest.raises(InvalidPrcProofError, match="legacy annotation"):
        normalize_legacy_annotations(source, "social")
```

- [ ] **Step 3: Run the new tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_prc_annotation_normalizer.py \
  tests/test_prc_templates.py -k "legacy_annotation or canonical_v2_positions" -x
```

Expected: import or assertion failure because the normalizer does not exist.

### Task 3: Implement source-preserving legacy annotation normalization

**Files:**
- Create: `src/solstice_mcp/prc_annotation_normalizer.py`
- Test: `tests/test_prc_annotation_normalizer.py`

**Interfaces:**
- Produces: `normalize_legacy_annotations(source: str, content_type: str) -> str`
- Produces: `assert_no_legacy_annotations(source: str) -> None`
- Internal: `_strip_legacy_css_rules(css: str) -> str`
- Internal: `_strip_sanofi_banner_engine(script: str) -> tuple[str, bool]`

- [ ] **Step 1: Implement a lossless source editor**

Use `HTMLParser` with absolute source offsets. Track open/close ranges, parent
classes, raw-text element IDs, and whether a node is under
`.sol-prc-annotation-overlay-v2`. Apply non-overlapping edits in reverse order:

```python
@dataclass(frozen=True)
class _Edit:
    start: int
    end: int
    replacement: str = ""


def _apply_edits(source: str, edits: list[_Edit]) -> str:
    updated = source
    for edit in sorted(edits, key=lambda item: item.start, reverse=True):
        updated = updated[: edit.start] + edit.replacement + updated[edit.end :]
    return updated
```

Remove pure chrome subtrees for legacy callouts/connectors/gutters. Unwrap
`.prc-render-stage` by deleting only its opening and closing tags so creative
children survive. Ignore platform-owned runtime IDs and v2 overlay descendants.

- [ ] **Step 2: Implement brace-aware CSS filtering**

Walk comments, strings, top-level rules, and nested at-rules. Remove a rule when
its selector contains one of the frontend legacy selectors:

```python
LEGACY_SELECTOR_RE = re.compile(
    r"(?:^|[.#\s])(?:prc-callout|prc-connector-line|prc-callout-gutter|"
    r"prc-alexion-gutter|prc-connector-svg|prc-alexion-svg|callout-overlay|"
    r"callout-box|callout-line|callout-dot|prc-render-stage)(?:\b|[-.#:\s])|"
    r"\[data-sol-prc-annotation(?:[=\]\s-])",
    re.IGNORECASE,
)
```

Preserve unrelated declarations and recursively retain non-empty `@media` /
`@supports` blocks. Remove comments that still contain a legacy source token.

- [ ] **Step 3: Remove dedicated engines and legacy position data**

Ignore `sol-prc-annotation-positions`, IDs in `PLATFORM_RUNTIME_STYLE_IDS`, and
`script[data-sol-prc-standalone-frame-fit]`. Remove a whole script only for the
existing generic/Alexion signatures:

```python
DEDICATED_ENGINE_SIGNATURES = (
    ("layoutStage", "prc-callout-gutter", "prc-connector-svg"),
    ("layoutAlexionStage", "prc-alexion-gutter", "prc-alexion-svg"),
)
```

Remove standalone legacy position scripts/assignments containing
`__prc_annotation_positions` or `generatedAnnotationPositions`. Do not alter
the canonical v2 positions script.

- [ ] **Step 4: Run isolated tests and verify GREEN**

Run:

```bash
.venv/bin/pytest tests/test_prc_annotation_normalizer.py -x
```

Expected: all tests pass.

- [ ] **Step 5: Commit the generic normalizer**

```bash
git add src/solstice_mcp/prc_annotation_normalizer.py \
  tests/test_prc_annotation_normalizer.py tests/test_prc_templates.py
git commit -m "feat: normalize legacy PRC annotation chrome"
```

### Task 4: Migrate the Sanofi mixed banner hydrator safely

**Files:**
- Modify: `src/solstice_mcp/prc_annotation_normalizer.py`
- Modify: `tests/test_prc_annotation_normalizer.py`
- Test fixture: compact inline fixture modeled on Sanofi sandbox V2 proof

**Interfaces:**
- Consumes: `_strip_sanofi_banner_engine(script: str) -> tuple[str, bool]`
- Preserves: `hydrateBannerSection`, `hydrate`, `hydrateIsi`, and bootstrap
- Removes: marker-delimited legacy callout engine plus external redraw hooks

- [ ] **Step 1: Add a failing mixed-script regression**

The fixture must include calls before the engine block and bootstrap after it:

```python
SANOFI_MIXED_SCRIPT = """
<script>
function hydrate(container) {
  iframe.srcdoc = bannerSrcdocForScene(container.rawSrcdoc, container.scene, "contain");
  renderPageCallouts(pageEl);
  setTimeout(function () { renderPageCallouts(pageEl); }, 220);
}
/* Hyperlink-destination annotation engine (self-contained). */
function renderPageCallouts(pageEl) { el.className = "callout-box"; }
function renderAllCallouts() { renderPageCallouts(document.body); }
/* ---- Bootstrap: iterate every banner section and hydrate ---- */
hydrateBannerSection(section, bannerIndex, isMulti);
</script>
"""
```

Assert the result retains `bannerSrcdocForScene`, `hydrateBannerSection`, and
the bootstrap marker, while removing `renderPageCallouts`, `renderAllCallouts`,
and every frontend MIGRATION token.

- [ ] **Step 2: Verify the regression fails**

Run:

```bash
.venv/bin/pytest tests/test_prc_annotation_normalizer.py \
  -k sanofi_mixed_banner -x
```

Expected: residual legacy script error.

- [ ] **Step 3: Implement marker-delimited mixed-script migration**

Require both exact semantic markers. Remove the region from the
`Hyperlink-destination annotation engine (self-contained)` comment through the
line before the bootstrap marker. Remove these external hooks with constrained
statement patterns:

```python
SANOFI_REDRAW_STATEMENTS = (
    r"^[^\S\n]*renderPageCallouts\([^;\n]*\);[^\S\n]*\n?",
    r"^[^\S\n]*setTimeout\(function\s*\(\)\s*\{\s*renderPageCallouts\([^;]*\);\s*\},\s*\d+\);[^\S\n]*\n?",
    r"^[^\S\n]*window\.addEventListener\([\"']resize[\"'],\s*renderAllCallouts\);[^\S\n]*\n?",
)
```

Also remove the narrow clone cleanup block that queries `.callout-overlay`.
After rewriting, rerun the same residual scanner; if any legacy token remains,
raise `InvalidPrcProofError` instead of returning partial output.

- [ ] **Step 4: Verify the Sanofi regression and generic suite**

Run:

```bash
.venv/bin/pytest tests/test_prc_annotation_normalizer.py -x
```

Expected: all tests pass.

- [ ] **Step 5: Commit the mixed-script migration**

```bash
git add src/solstice_mcp/prc_annotation_normalizer.py tests/test_prc_annotation_normalizer.py
git commit -m "fix: migrate mixed banner annotation engines"
```

### Task 5: Integrate normalization and strict frontend-parity validation

**Files:**
- Modify: `src/solstice_mcp/prc_proof_composer.py:554-627`
- Modify: `src/solstice_mcp/operations.py:762-779`
- Modify: `tests/test_prc_templates.py`
- Modify: `tests/test_versions.py`

**Interfaces:**
- Consumes: `normalize_legacy_annotations(source: str, content_type: str) -> str`
- Consumes: `assert_no_legacy_annotations(source: str) -> None`
- Changes: `validate_prc_proof(source: str, content_type: str, *, allow_legacy_annotations: bool = False) -> None`

- [ ] **Step 1: Add failing composer and supplied-proof integration tests**

Assert ordinary commits normalize a prior proof, and supplied baked proofs may
enter composition with legacy annotation residue but leave strictly clean:

```python
def test_compose_normalizes_annotations_before_final_validation():
    base = BANNER_TEMPLATE.replace(
        "</body>",
        '<div class="callout-overlay"></div><style>.callout-box{display:block}</style></body>',
    )
    proof = compose_prc_proof(base, CREATIVE, "banner")
    assert_no_legacy_annotations(proof)
```

In `tests/test_versions.py`, pass a structurally baked supplied proof with a
dedicated legacy engine and assert the stored proof has no migration residue.

- [ ] **Step 2: Verify integration tests fail**

Run:

```bash
.venv/bin/pytest tests/test_prc_templates.py tests/test_versions.py \
  -k "normalizes_annotations or supplied_proof" -x
```

Expected: residual legacy annotation assertion or pre-compose validation error.

- [ ] **Step 3: Wire normalization into composition**

Normalize after creative injection/payload replacement and before L4/final
validation:

```python
if content_type == "banner":
    proof = _set_banner_srcdoc_payload(proof, creative_html)
proof = normalize_legacy_annotations(proof, content_type)
proof = _canonicalize_l4_config(proof)
validate_prc_proof(proof, content_type)
```

At the end of `validate_prc_proof`, call
`assert_no_legacy_annotations(source)` unless
`allow_legacy_annotations=True`. In `_compose_supplied_prc_proof`, use that
flag only for the initial structural/baked check; `compose_prc_proof` performs
strict final validation.

- [ ] **Step 4: Verify focused integration tests pass**

Run:

```bash
.venv/bin/pytest tests/test_prc_templates.py tests/test_versions.py \
  -k "annotation or banner_prototype or comment_only_field or supplied_proof" -x
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit integration**

```bash
git add src/solstice_mcp/prc_proof_composer.py src/solstice_mcp/operations.py \
  tests/test_prc_templates.py tests/test_versions.py
git commit -m "fix: enforce PRC Contract v2 annotation parity"
```

### Task 6: Run repository verification

**Files:**
- Verify all changed Python and test files

- [ ] **Step 1: Run focused and full relevant tests**

```bash
.venv/bin/pytest tests/test_prc_annotation_normalizer.py tests/test_prc_templates.py tests/test_versions.py -x
```

Expected: exit 0 with zero failures.

- [ ] **Step 2: Run lint and format checks**

```bash
.venv/bin/ruff check src/solstice_mcp/prc_annotation_normalizer.py \
  src/solstice_mcp/prc_proof_composer.py src/solstice_mcp/operations.py \
  tests/test_prc_annotation_normalizer.py tests/test_prc_templates.py tests/test_versions.py
.venv/bin/ruff format --check src/solstice_mcp/prc_annotation_normalizer.py \
  src/solstice_mcp/prc_proof_composer.py src/solstice_mcp/operations.py \
  tests/test_prc_annotation_normalizer.py tests/test_prc_templates.py tests/test_versions.py
```

Expected: both commands exit 0.

- [ ] **Step 3: Run type checking**

```bash
.venv/bin/mypy src/solstice_mcp
```

Expected: exit 0.

- [ ] **Step 4: Review scope**

```bash
git diff --check
git status --short
git diff main...HEAD --stat
```

Expected: no whitespace errors; only MCP PRC composer/normalizer, focused tests,
and design/plan docs changed.

### Task 7: Verify through the Sanofi sandbox and frontend

**Files:**
- Temporary: `tmp/` verification artifacts only; delete after use
- Permanent external effect: one appended draft version on operation `4ff73599-e6c6-4767-a947-0e2a7a7f06b1`

**Interfaces:**
- Consumes: local MCP at `http://localhost:8001/mcp`
- Consumes: frontend `checkPrcContract`
- Produces: one verified Sanofi sandbox asset version

- [ ] **Step 1: Verify infrastructure**

```bash
aws sts get-caller-identity --profile solstice-dev
docker ps --format '{{.Names}}' | rg 'ssh-tunnel|solstice-mcp-local-verify'
```

Expected: valid `solstice-dev` identity and running dev tunnel.

- [ ] **Step 2: Build and run the local MCP**

Build the current branch image and run it on port 8001 with the development DB
template and writable AWS SSO cache. Confirm `/health` returns 200.

- [ ] **Step 3: Delegate the append and browser verification**

Dispatch a subagent with the exact asset URL and require it to:

1. Record current head/display version.
2. Commit a minimally changed sentinel creative through the local MCP using the
   current `base_message_id`.
3. Fetch the resulting proof and run the actual frontend
   `checkPrcContract`; expect `[]`.
4. Confirm prototype iframes are bare, creative payload is current, and no live
   frame is empty.
5. Open the asset in the local frontend and confirm the “Fix to v2” button is
   absent and storyboard frames render at the expected dimensions.
6. Return the new row ID, display version, exact commands/results, and a
   screenshot.

- [ ] **Step 4: Stop local verification services and delete temp artifacts**

Expected: no tracked verification files or local MCP container remain.

### Task 8: Push and open the PR

**Files:**
- Include all commits on `fix/mcp-prc-banner-prototype`

- [ ] **Step 1: Run the MCP pre-PR checklist**

Read `.cursor/rules/pr-review-checklist.mdc` and run every applicable check.

- [ ] **Step 2: Push the verified branch**

```bash
git push -u origin HEAD
```

Expected: branch `fix/mcp-prc-banner-prototype` exists on origin.

- [ ] **Step 3: Open a PR into `main`**

Use `.github/PULL_REQUEST_TEMPLATE.md`. Document:

- banner prototype stamping root cause,
- comment-only Contract v2 false positive,
- annotation MIGRATION mismatch,
- fail-closed behavior for unknown mixed scripts,
- exact local test output,
- Sanofi sandbox asset/version and screenshot.

Expected: a GitHub PR URL targeting `main`.
