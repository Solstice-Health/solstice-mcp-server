# Operation types: create vs edit

Every Solstice asset (operation) is one of two request types. The database
column `operation_category` records which one; the right MCP tool depends on
it. Route by user intent and confirm the choice back in plain words before
writing.

## The two request types

| | Create request | Edit request |
|---|---|---|
| User says | "make me an email", "generate a banner" | "here is my HTML/PDF", "put this file in Solstice", "edit this" |
| Plain-language name | "Create with Solstice AI" | "Edit an existing HTML" / "Edit an existing PDF" |
| Category | `SOLSTICE_GENERATED` | `EDIT_HTML` or `EDIT_PDF` (by file type) |
| MCP tool | `solstice_create_operation` | `solstice_create_edit_operation` (`kind="html"` or `"pdf"`) |
| Content at create | none — Solstice produces it later | the user's finished document, landed as v1 via prepare → upload → commit |
| Dashboard bucket | "Generated" | "Existing Content" |

Never pick silently. If the user's intent is ambiguous ("I need this email in
Solstice" with a file attached), ask one short question.

## What each category means technically

- **SOLSTICE_GENERATED** — born from a brief. Document versions accumulate as
  the content is generated and revised.
- **EDIT_HTML** — the user brought a finished HTML file. The v1 commit marks
  the operation as having saved HTML. Ask nothing beyond file, name, and
  content type.
- **EDIT_PDF** — the user brought an approved PDF. The v1 commit records the
  working PDF pointer and completes the operation. Two artifacts exist:
  - the **working PDF** (what reviewers see) — landed with `type="pdf"`;
  - the optional **design source file** (InDesign package, ZIP, PPTX, HTML —
    what a designer edits) — attached with `type="source"`, which records a
    pointer and is not a version. If the user did not provide a source file,
    ask ONCE whether they have it; "I don't have it" is acceptable — proceed
    without.
  - When the source file is HTML, ask ONCE more: "Do you want the HTML source
    viewable next to the PDF in Solstice (a PDF/Source toggle on the asset
    page)?" On an explicit yes, pass `show_source_on_ui=true` on the
    `type="source"` commit — after the PDF version is committed, never
    before. Skip the question entirely for non-HTML sources; the flag is
    rejected for them.

The category is fixed at creation and is not editable afterwards; choosing
wrong means recreating the asset. `content_type` (EMAIL, BANNER, SOCIAL...)
is a separate field — it says what the asset is, while the category says how
it originated — and is required for both request types.

## Fixing fields after creation

`solstice_update_operation` (Solstice staff only) edits an existing asset's
display name, content type, or owner. Use it when an asset was created with
the wrong `content_type` or name; there is no tool that changes
`operation_category`. Non-staff users must ask a Solstice staff member.
