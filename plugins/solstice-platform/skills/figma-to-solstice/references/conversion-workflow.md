# Conversion workflow

End-to-end protocol for Figma → Solstice HTML via agent conversion + MCP landing.

## Architecture note

The platform's in-app path is Backend-Server `src/figma_to_html/` (`POST /api/figma/import-url/stream` and admin draft-convert). That pipeline owns Figma PAT, OpenAI vision, footer/ISI splice, and S3 image rewrite. MCP does **not** proxy it.

This skill's path:

- **Figma MCP** — pull design context (node tree, styles, screenshot)
- **Solstice MCP** — brand rules / design assets / claims + create/prepare/commit
- **Agent** — convert HTML locally and iterate with the user before any write

When full platform fidelity (footer compiler, hosted image rewrite) is required and the user is working in-app, point them at the Solstice Figma import wizard instead of this skill.

## 1. Pull the design

1. Call `solstice_list_sibling_mcps` (requires Solstice email). Find the `figma` entry (`https://mcp.figma.com/mcp`).
2. If Figma MCP is connected, resolve the user's file/frame URL. Prefer a frame-scoped link (`node-id` in the URL). Pull:
   - frame metadata / node tree
   - relevant styles / variables
   - a rendered screenshot of the target frame
3. If Figma MCP is not connected, ask the user to connect it or export the frame (PNG/PDF) plus any copy they care about. Do not invent layout from a bare URL alone.

Ask which frame to convert when the file has several plausible targets.

## 2. Gather Solstice brand context

Resolve workspace and brand using the `solstice-platform` skill sequence (`solstice_list_tenants` → `solstice_whoami` → `solstice_list_brands`). Then:

1. `solstice_brand_rules(tenant_slug, brand_id)` — guidelines plus `design_bible`, `isi`, `drug_info`.
2. `solstice_brand_design_assets(tenant_slug, brand_id)` — logos/heroes with time-limited URLs.
3. `solstice_brand_claims(tenant_slug, brand_id)` — extracted clinical claims. Use `claim_text` verbatim.
4. Format exemplar (strongly preferred):
   - `solstice_list_operations` for the brand
   - pick one final HTML asset of the same content type (email, banner, etc.)
   - `solstice_operation_messages` → find a final `html` message
   - `solstice_operation_html(..., fetch=true)` only for that exemplar, to match structure (footer/ISI placement, references style, document chrome)

If no exemplar exists, match a reasonable self-contained HTML document and still honor brand rules + ISI from `solstice_brand_rules`.

## 3. Convert

Produce **one** self-contained HTML document:

- Match the exemplar's document structure and brand chrome when available.
- Apply brand rules and design bible constraints (colors, typography, spacing).
- Place ISI / safety content from `isi` / `drug_info` where the exemplar places them.
- Use only claims returned by `solstice_brand_claims`.
- Inline images as base64 `data:` URIs (MCP version upload accepts html/pdf only — no separate image upload).

ponytail: inlining images as data URIs avoids a parallel asset host; ceiling is large HTML bodies and no shared CDN rewrite. Upgrade path: Backend `figma_to_html` S3 rewrite / public asset upload when platform fidelity is required.

Do not call any Solstice write tool in this step.

## 4. Human-in-loop preview

1. Save the draft locally (e.g. a temp `.html` file in the workspace) and show / summarize it for the user.
2. Apply requested edits and re-preview.
3. Repeat until the user explicitly approves publishing.

Hard stop: if the user has not said to create / save / publish the asset, stop after preview. Do not create an empty operation "to hold" the draft.

## 5. Land the asset (only after approval)

Confirm before writing:

- workspace (`tenant_slug`)
- brand
- project + folder path (folder must already exist)
- file / asset name
- content type (required — e.g. `EMAIL`, `BANNER`, `SOCIAL`; if the user has not stated one, ask before creating — never guess or default)

Sequence:

1. `solstice_create_operation(tenant_slug, project_id, name, content_type, folder_path?)` → retain `operation_id`.
2. `solstice_prepare_operation_version(tenant_slug, operation_id, type="html", file_name?)` → retain `upload_url`, `s3_key`, `type`, `file_name`. `file_name` is a bare filename only (e.g. `apretude_hero.html`); never pass instructions or prose — the gateway's prompt-attack guardrail scans this field and will deny instruction-like text.
3. HTTP PUT the HTML bytes to `upload_url` with `Content-Type: text/html`. If the upload fails, stop without committing.
4. `solstice_commit_operation_version(tenant_slug, operation_id, type, s3_key, file_name?)` with the unchanged prepare values (same `file_name`).
5. Report:
   - committed `version_number` and server-derived `intent`
   - the `asset_url` returned by the commit response, ending the reply with a markdown link titled "Open asset in Solstice" pointing at that URL — never hand the user a bare operation UUID

Intent is derived from the token (SOLSTICE_STAFF → draft; MEMBER/ADMIN → final). Never pass intent or role as an argument.

## Unsupported

- Proxying Backend `/api/figma/import-url/stream` or admin convert through MCP
- Editing / overwriting an existing document version
- Creating folders in a project dir_map
- Inventing claims, ISI, or brand rules not returned by Solstice tools

For those requests, explain the limit and offer the supported path above (or the in-app Figma import wizard when platform convert is what they need).
