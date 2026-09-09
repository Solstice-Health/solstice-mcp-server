# MCP PRC Composition and Contract v2 Parity Design

## Goal

Prevent MCP HTML commits from corrupting banner proof prototypes, and prevent
MCP from publishing any supported proof that the frontend still reports as
needing “Fix to v2.”

## Scope

The change is limited to the MCP PRC composer and its focused tests. It covers
all profiles the MCP composer currently supports: email, banner, and social.

- Banner composition must preserve empty prototype iframes inside
  `template#frame-template` and `template#isi-region-template`.
- Banner creative HTML remains available through the existing
  `window.__BANNER_TEMPLATE_SRCDOC__` payload written by the composer.
- Banner validation accepts empty prototype iframes but continues to reject
  empty rendered/live banner iframes.
- Contract checks for `data-sol-prc-pages`, `data-sol-prc-page`, and
  `data-sol-prc-field` require attributes on parsed HTML elements. Mentions in
  comments, scripts, styles, textareas, or text do not satisfy the contract.
- Composition removes template-owned legacy annotation DOM, CSS, JavaScript,
  and legacy position data before validating the proof. Creative links and
  `href` values remain unchanged.
- Platform-owned Contract v2 annotation runtime and
  `script#sol-prc-annotation-positions` are not legacy and remain valid.
- The MCP validator mirrors the frontend MIGRATION check. If an unfamiliar
  mixed script cannot be normalized safely, the commit fails rather than
  publishing a proof that still shows “Fix to v2.”
- Email and social slot injection behavior remains unchanged.
- The equivalent backend composer is not changed in this PR.

## Implementation

Use Python's standard-library `HTMLParser` to collect element attributes and
template ancestry without adding runtime dependencies. Validation and contract
stamping use this parsed structure for semantic checks. Existing position-aware
regex helpers remain responsible for preserving source formatting while
rewriting tags.

Banner injection selects only non-prototype banner iframes. Legacy banner
adaptation marks the prototype with `data-sol-prc-creative="banner"` but leaves
its `srcdoc` empty. Final validation distinguishes prototype slots from live
slots: prototypes may be empty; any live banner slot present in the document
must have a non-empty `srcdoc`. The existing banner payload assignment remains
required to carry the creative used by the runtime hydrator.

Add a source-preserving annotation normalization pass before final validation:

1. Remove pure legacy callout DOM and unwrap legacy geometry wrappers while
   preserving creative slots and page content.
2. Remove legacy annotation CSS rules with a brace-aware scanner instead of a
   broad text replacement.
3. Remove dedicated legacy annotation scripts by signature.
4. For the Sanofi fleet banner shape, remove the marker-delimited
   “Hyperlink-destination annotation engine” section and its external redraw
   call sites while retaining the surrounding banner hydration/bootstrap code.
5. Remove legacy position assignments and data, but preserve the canonical v2
   positions script.

After normalization, apply a profile-independent MIGRATION validator equivalent
to the frontend `hasLegacyAnnotationFormat` rules. It ignores creative `srcdoc`
payloads, canonical v2 annotation positions, platform runtime nodes/styles, and
the standalone frame-fit script. Any remaining legacy DOM or source token raises
`InvalidPrcProofError`.

## Tests

Follow red-green TDD in `tests/test_prc_templates.py`:

1. Reproduce a banner catalog shell whose first matching iframe is the
   `#frame-template` prototype and assert composition leaves it bare while
   publishing the creative payload.
2. Assert an empty live banner iframe still fails validation.
3. Reproduce a proof whose only `data-sol-prc-field` mention is an HTML comment
   and assert validation rejects it.
4. Assert composition stamps a real fallback field when a legacy seed only
   mentions the field in a comment.
5. Parameterize email, banner, and social proofs with removable legacy
   annotation DOM, CSS, script, and position data; assert composition removes
   it without changing creative links.
6. Reproduce the Sanofi mixed banner hydrator shape; assert only its embedded
   legacy callout engine and redraw hooks are removed, banner hydration remains,
   and the frontend-compatible MIGRATION validator passes.
7. Assert canonical v2 positions and platform-owned runtime chrome are allowed.
8. Assert unfamiliar residual legacy code fails closed.
9. Run the focused PRC template suite, Ruff, Ruff format, and mypy.

## Sandbox Verification

Run the modified MCP locally on port 8001 against the development SSH tunnel
and `sanofi_sandbox`. A verification subagent will select an existing scratch,
PRC-enabled banner operation with a readable current head and proof. It will:

1. Record the current `head_message_id`, displayed version, and proof anatomy.
2. Prepare and upload a minimally changed HTML creative with a unique sentinel.
3. Commit through `solstice_commit_operation_version` using the recorded head as
   `base_message_id`.
4. Confirm the displayed version increments, the new proof contains the
   sentinel payload, prototype iframes remain bare, live frames render, and the
   actual frontend `checkPrcContract` reports no issue.
5. Open the resulting asset in the local frontend and confirm the “Fix to v2”
   button is absent and the banner storyboard still renders.

The append is permanent and is restricted to the Sanofi development sandbox.
No production tenant or direct database write is permitted.

## Delivery

Continue on the feature branch created from local MCP `main`; do not merge or
depend on Alex's PR. After local and sandbox verification, push the feature
branch and open a pull request into `main`. The PR documents all three root
causes, test evidence, and the permanent sandbox verification asset/version.
