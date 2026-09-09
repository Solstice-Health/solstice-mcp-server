# MCP PRC Banner Prototype and Contract Validation Design

## Goal

Prevent MCP HTML commits from corrupting banner proof prototypes, and prevent
comment text from satisfying Contract v2 element-attribute requirements.

## Scope

The change is limited to the MCP PRC composer and its focused tests:

- Banner composition must preserve empty prototype iframes inside
  `template#frame-template` and `template#isi-region-template`.
- Banner creative HTML remains available through the existing
  `window.__BANNER_TEMPLATE_SRCDOC__` payload written by the composer.
- Banner validation accepts empty prototype iframes but continues to reject
  empty rendered/live banner iframes.
- Contract checks for `data-sol-prc-pages`, `data-sol-prc-page`, and
  `data-sol-prc-field` require attributes on parsed HTML elements. Mentions in
  comments, scripts, styles, textareas, or text do not satisfy the contract.
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
5. Run the focused PRC template suite, Ruff, Ruff format, and mypy.

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
   frontend contract check reports no issue.

The append is permanent and is restricted to the Sanofi development sandbox.
No production tenant or direct database write is permitted.

## Delivery

After local and sandbox verification, push the feature branch and open a pull
request into `main`. The PR documents both root causes, test evidence, and the
permanent sandbox verification asset/version.
