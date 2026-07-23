# UI map: admin dashboard (Solstice staff)

All paths are relative to `https://www.{tenant-subdomain}.solsticehealth.co`.

## Who sees this

Admin surfaces are gated on **SOLSTICE_STAFF** (per-brand super user), enforced
server-side by the app's middleware. Brand-level ADMIN or MEMBER roles do NOT
unlock any `/admin/*` route — a brand ADMIN is still a regular `/home` user.
Non-staff visitors are redirected to `/home`. If an expected admin control is
missing, the signed-in user lacks SOLSTICE_STAFF on that brand; report it, do
not hunt for workarounds.

## Getting there

- **Avatar menu (top bar)** → "Admin Dashboard" → lands on `/admin/templates`
  (there is no `/admin` landing page — deep-link a specific tool).
- **Sidebar on `/home`** → "Admin" group (staff only): **Requests**
  (`/home/requests`) and **Memory Lens** (`/home/memory`, feature-flagged).
- Pick the brand in the sidebar brand picker BEFORE using brand-scoped tools;
  staff role is per-brand.

## Admin sidebar tools (label → path)

| Label | Path | What it does |
|---|---|---|
| Templates | `/admin/templates` | Brand template library: search, Add, Bulk Upload header/footer zip, edit / Edit HTML / delete per row |
| Core Brand Information | `/admin/isi` | Brand type (Promotional / Medical Affairs) + Patient/Physician ISI entries: add, edit (`/admin/isi/{isiType}`), delete, PDF ISI upload |
| Clinical Claim | `/admin/clinical-claim` | Claims table + Veeva claims: Add, edit, Edit HTML, delete |
| Group Review | `/admin/groups-review` | Grouped claim review from source docs: create/delete groups and claims, references, annotations |
| Veeva Annotations | `/admin/veeva-annotations` | Brand annotation store: add via Veeva file search, edit, delete |
| Brand Metadata | `/admin/brand-metadata` | Brand settings: CEL (default assignee), feature toggles (auto-approve, bypass MLR/QC, fact-check, ISI tool permission), job-code schema, PRC templates, logo crop, raw JSON |
| User Management | `/admin/user-management` | Company-wide: expand brand → members, Invite, remove, bulk create from pasted emails |
| Prompt Management | `/admin/prompt-management` | Pipeline prompt registry: default prompts + per-brand overrides |
| ISI Tool | `/admin/isi-tool` | Bulk ISI update wizard: Select Documents → Select ISI → Additional Updates → Review Changes (diff/accept) |
| Solstice Studio | `/admin/claude-studio` | AI HTML/file workspace: file explorer, chat, preview, save as PRC template |
| Visual Assets Scraper | `/admin/social-assets-scraper` | Scrape platform URLs → select assets → save to template library |
| Cron Job | `/admin/cron-job` | Company cron + operation GC settings, run-now buttons |
| Veeva Sync | `/admin/veeva-sync` | Vault credentials/config (`/sync-settings`), synced doc browser (`/vault`, `/vault/{docNumber}`) |
| Public Asset Uploader | `/admin/public-asset-uploader` | Upload public CDN assets, copy URL |

## Request triage (`/home/requests`)

The staff queue of user requests (initial_save, change requests, approvals).

- Filters: Status / Type / Brand / Priority / Requester / Assignee; search by
  file name or operation ID; per-row Priority select (High/Medium/Low/Backlog).
- Row actions: **Details** → `/home/review-request/{operationId}`;
  **Open workspace** / **View asset**. Row click routes by asset readiness:
  pending without HTML → review-request page; with HTML → the asset editor.

### Review-request page (`/home/review-request/{operationId}`)

- Request info, chat, and source files.
- **Upload final asset** / **Upload as draft** (HTML or PDF), **Upload source file**.
- **Dismiss request** → dialog with mandatory category
  (Duplicate / Invalid / Out of scope / Other) + optional note (max 500 chars).
- **Mark completed** — approval requests only.

## Publish / approve flow (report card)

1. From Requests or a project: **Open workspace** → asset editor
   (`/home/assets/{id}`) in admin mode (draft saves, QC rail, Publish button).
2. **Publish / Review QC** → report card
   (`/home/assets/{id}/report-card` or the content-dashboard equivalent):
   version compare, MLR check, ISI check (**Change ISI** → **Apply ISI**), then
   **Publish**. Publishing also resolves the linked pending request rows.

## Staff-only controls on shared pages

- `/home/projects/{projectId}`: row menu → **Open in workspace**,
  **Change owner** (pick a brand member).
- Brand profile members table: **Change role**
  (MEMBER / ADMIN / SOLSTICE_STAFF); only staff can remove another staff.
- `/home/memory` (Memory Lens): scope Personal/Brand, search, Why / Replace /
  Forget. Brand-scope mutations need brand ADMIN or SOLSTICE_STAFF.

## Legacy surfaces (avoid unless asked)

`/content-dashboard/**`, `/dashboard/**`, `/fact-check`, `/reviewed-document/**`
are the older staff dashboards, all staff-gated. Day-to-day staff work happens
on `/home` + Requests + the `/admin/*` tools; only use legacy routes when the
user explicitly points there.
