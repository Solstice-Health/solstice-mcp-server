# ISI replacement workflow

The protocol mirrors the platform's admin ISI Tool wizard (4 steps). Collect
each step's inputs before transforming anything. Ask for what the user has not
already given; never guess. All extras default to OFF, exactly like the UI.

## Step 1 — Select documents

1. Resolve the workspace and brand (`solstice_list_tenants`,
   `solstice_list_brands`). Ask when ambiguous.
2. List projects (`solstice_list_projects`) and let the user pick one, then
   read its folder map (`solstice_project_info`).
3. Offer the project's HTML-like assets (leaf names ending `.html` / `.htm`,
   or with no extension). The user may pick **one or many**.
4. Confirm the final list back to the user before proceeding.

## Step 2 — ISI source (exactly one)

Ask which source to use — the same three modes as the UI:

- **Brand ISI** — call `solstice_brand_rules`; the `isi` field holds the
  stored variants (`physician_isi`, `rendered_physician_isi`, `patient_isi`,
  `rendered_patient_isi`; each may have multiple versions). Present the
  available types/versions and let the user pick one. Use its HTML verbatim.
- **Pasted HTML** — the user supplies the ISI HTML directly.
- **User file (DOCX / PDF)** — the user supplies the new ISI as a file. Convert
  it to HTML faithfully: keep the text verbatim (headings, bullets, bolding);
  use the brand's existing rendered ISI (from `solstice_brand_rules`) as the
  styling reference so the block looks native.

Optional: the user may give one-off edit instructions for the ISI fragment
(e.g. "bold the boxed warning heading"). Apply them ONCE to the fragment,
show the result, and get a thumbs-up before using it for the batch. Never
apply instructions that change the safety language itself — formatting only;
flag anything that would alter wording.

## Step 3 — Additional updates (ask once, defaults OFF)

Ask one compact question covering the four extras; anything not requested
stays off:

- **Date / copyright update** — off | same target date for all operations |
  a different date per operation. When on, update material/copyright dates
  **outside the ISI block only**.
- **Subject / preheader (emails)** — per operation, optional new subject and
  preheader pairs to swap in the email topper.
- **Veeva job codes** — off | manual current→new code pairs per operation |
  a user-supplied spreadsheet of mappings (read it and confirm the parsed
  pairs back to the user).
- **Manual find → replace pairs** — exact literal text swaps, applied outside
  the ISI block.

## Step 4 — Transform, preview, and land (per operation)

Process the queue one operation at a time:

1. **Fetch** — find the latest final HTML version via
   `solstice_operation_messages`, then `solstice_operation_html` with
   `fetch=true`.
2. **Swap the ISI block**:
   - Locate the region by its headings — the block starting at
     "IMPORTANT SAFETY INFORMATION" (often paired with an INDICATION heading)
     through the end of that safety section. Banner/social assets may mark it
     with `data-isi-slot="isi"` — when present, swap exactly that element's
     contents.
   - Replace the whole block with the chosen ISI fragment, keeping the
     document's own wrapper/table structure and styles so the block renders
     native to the asset.
   - Everything outside the ISI stays byte-identical except the extras the
     user enabled.
3. **Apply extras** — date pairs, subject/preheader, job codes, manual pairs —
   each outside the ISI block.
4. **Preview** — show the user what changed (before/after of the ISI region
   and each extra). Ask: accept, reject (skip this operation, no write), or
   redo with corrections.
5. **Land on accept** — only after approval:
   `solstice_prepare_operation_version` with `type="html"` and a bare
   `file_name` → PUT the HTML bytes → `solstice_commit_operation_version`
   with the unchanged prepare values. Report the committed version number and
   server-derived intent. If the caller is Solstice staff (intent `draft`),
   offer `solstice_approve_operation_version` to publish it.
6. Move to the next operation. A failure on one operation never blocks the
   rest — record it and continue.

## Wrap-up

Summarize the batch: per operation — accepted (version + intent), skipped, or
failed (why). Remind the user that rejected/failed operations were not
modified.

## Out of scope

- The Backend-Server `/isi-tool/*` endpoints (Celery jobs, accept-async) are
  not reachable from this connection; do not try them. If a document's ISI
  cannot be located confidently, say so and skip rather than guessing.
- Saving a generated ISI back to the brand record is a platform admin action —
  point staff to the admin UI.
- Deleting or overwriting existing versions is unsupported.
