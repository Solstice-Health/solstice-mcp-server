# MCP PRC Banner Prototype and Contract Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve bare banner template prototypes during MCP commits and require real Contract v2 element attributes.

**Architecture:** Add a standard-library HTML parser that records element attributes and whether an iframe is inside a recognized banner prototype template. Keep existing source-preserving regex transforms, but use parsed structure to select valid rewrite/validation targets.

**Tech Stack:** Python 3.12, `html.parser.HTMLParser`, pytest, Ruff, mypy, local MCP server, Solstice MCP tools.

## Global Constraints

- No new runtime dependency or lockfile change.
- Email and social injection behavior remains unchanged.
- Banner creative remains in `window.__BANNER_TEMPLATE_SRCDOC__`.
- No backend or frontend production-code changes.
- Sandbox write is append-only and restricted to `sanofi_sandbox`.

---

### Task 1: Add failing regression tests

**Files:**
- Modify: `tests/test_prc_templates.py`

**Interfaces:**
- Consumes: `compose_prc_proof(base_html: str, creative_html: str, content_type: str) -> str`
- Consumes: `validate_prc_proof(source: str, content_type: str) -> None`
- Produces: regression coverage for banner prototype and comment-only field behavior

- [ ] Add `test_compose_prc_proof_leaves_banner_prototype_bare`, parsing the composed proof and asserting `template#frame-template iframe` has no `srcdoc`, while the banner payload contains `CREATIVE`.
- [ ] Add `test_validate_prc_proof_rejects_empty_live_banner_slot`, adding an empty banner iframe outside a template and expecting `InvalidPrcProofError("empty creative slot")`.
- [ ] Add `test_validate_prc_proof_rejects_comment_only_field`, replacing the real field with `<!-- data-sol-prc-field="file_name" -->` and expecting `InvalidPrcProofError("baked contract v2")`.
- [ ] Add `test_compose_prc_proof_stamps_field_when_only_comment_mentions_it`, composing a legacy seed with only the comment marker and asserting a real `[data-sol-prc-field]` exists.
- [ ] Run:
  `pytest tests/test_prc_templates.py -k 'banner_prototype or empty_live_banner_slot or comment_only_field or stamps_field_when_only_comment' -x`
  Expected: failures proving the current prototype is stamped and comment text is accepted.

### Task 2: Implement semantic structure inspection

**Files:**
- Modify: `src/solstice_mcp/prc_proof_composer.py`
- Test: `tests/test_prc_templates.py`

**Interfaces:**
- Produces: `_PrcStructureParser(HTMLParser)` with collected attributes and banner prototype iframe classification
- Produces: `_has_element_attr(source: str, name: str) -> bool`
- Produces: `_banner_frames(source: str) -> tuple[list[str], list[str]]`, separating prototype and live opening tags

- [ ] Import `HTMLParser` from `html.parser`.
- [ ] Implement a parser whose `handle_starttag`/`handle_startendtag` records normalized attribute names and tracks `template` ancestors with IDs `frame-template` and `isi-region-template`.
- [ ] Replace raw `_has` checks for `data-sol-prc-pages`, `data-sol-prc-page`, and `data-sol-prc-field` in base validation, stamping, and final validation with `_has_element_attr`.
- [ ] Change banner composition so recognized prototype iframes are never assigned `srcdoc`; legacy adaptation may add `data-sol-prc-creative="banner"` but must keep the prototype bare.
- [ ] Change banner validation to allow empty recognized prototypes, reject empty live banner slots, and require the generated banner payload assignment.
- [ ] Run the focused command from Task 1. Expected: all selected tests pass.

### Task 3: Verify the MCP repository

**Files:**
- Verify: `src/solstice_mcp/prc_proof_composer.py`
- Verify: `tests/test_prc_templates.py`

- [ ] Run `pytest tests/test_prc_templates.py -x`. Expected: zero failures.
- [ ] Run `ruff check src/solstice_mcp/prc_proof_composer.py tests/test_prc_templates.py`. Expected: exit 0.
- [ ] Run `ruff format --check src/solstice_mcp/prc_proof_composer.py tests/test_prc_templates.py`. Expected: exit 0.
- [ ] Run `mypy src/solstice_mcp/prc_proof_composer.py`. Expected: exit 0.
- [ ] Review `git diff --check` and the scoped diff for unrelated changes.

### Task 4: Verify through a Sanofi sandbox append

**Files:**
- Temporary only: local verification HTML outside tracked test directories

**Interfaces:**
- Consumes: local modified MCP at `http://localhost:8001/mcp`
- Produces: one permanent draft HTML version on an existing scratch banner operation in `sanofi_sandbox`

- [ ] Verify AWS SSO with `aws sts get-caller-identity --profile solstice-dev` and confirm the dev SSH tunnel is available on localhost port 5432.
- [ ] Start the modified local MCP on port 8001 with the localhost OAuth audience and development DB template.
- [ ] Delegate discovery and append verification to a subagent: select a staff-visible scratch banner operation whose head has a PRC proof; record its current head and displayed version.
- [ ] Through the local MCP, call prepare, upload a sentinel-bearing HTML creative, and commit with the recorded `base_message_id`.
- [ ] Re-read the new head and proof. Confirm version increment, sentinel payload, bare recognized prototypes, no empty live frame, and clean Contract v2 structure.
- [ ] Stop the temporary local MCP and restore any uncommitted local plugin URL override.

### Task 5: Deliver the pull request

**Files:**
- Commit: `src/solstice_mcp/prc_proof_composer.py`
- Commit: `tests/test_prc_templates.py`
- Include: design and implementation documents

- [ ] Run the repository's CI-equivalent checks and pre-PR checklist.
- [ ] Commit the implementation with a descriptive message; obtain a Linear ID before PR creation only if the repository template requires one.
- [ ] Push `fix/mcp-prc-banner-prototype`.
- [ ] Open a GitHub PR into `main` describing both root causes, tests, and sandbox asset/version evidence.
