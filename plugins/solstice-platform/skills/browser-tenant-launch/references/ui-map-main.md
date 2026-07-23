# UI map: main app (all authenticated users)

All paths are relative to `https://www.{tenant-subdomain}.solsticehealth.co`.
The modern app lives under `/home/*`; avoid legacy routes (`/dashboard`,
`/content-dashboard`, `/library`, `/claim-studio` at root) unless the user
points there.

## Login and tenant context

- Visiting `/` unauthenticated redirects to the **Auth0 hosted login**
  (`/auth/login`). There is no in-app password form — the user signs in on the
  Auth0 page. Never type or capture credentials; hand this step to the user.
- There is no tenant picker: the tenant IS the subdomain. After login the user
  lands on `/home` with a brand auto-selected (last used, else first).
- Brand switching: the **brand switcher** in the sidebar masthead (search,
  switch, Brand Profile link, Create new brand).

## Navigation (sidebar + top bar)

| Sidebar label | Path | Notes |
|---|---|---|
| Home | `/home` | Desk: stats, Awaiting Review, Recent Activity, **Create new** / **Edit content** CTAs |
| Recents | `/home/recents` | Recent operations, filter by type/status/time |
| Projects | `/home/projects` | Project list → `/home/projects/{projectId}` file tree |
| Library | `/home/library` | Tabs: references, approved-pieces, design-elements, claims (`?tab=`) |
| Claim Studio | `/home/claim-studio` | Generate/curate claims |
| Veeva Vault | `/home/veeva-vault` | Connect with Vault credentials, browse |
| Requests / Memory Lens | `/home/requests`, `/home/memory` | Staff-only Admin group (see admin map) |

Top bar: **⌘K command palette** (jump anywhere, "Create new content", search
projects/files/brands), notification bell, avatar menu (**My Profile** →
`/profile`, staff: **Admin Dashboard**, **Log out**).

## Deep links (IDs from MCP tools paste straight in)

- Asset workspace: `/home/assets/{operation_id}` (+ `/report-card`)
- Project file tree: `/home/projects/{project_id}`
- Review request (staff): `/home/review-request/{operation_id}`
- Generation progress: `/home/generating/{operation_id}`

## Key flows

### Create new content (intake)
1. Home → **Create new** → `/home/intake/new`: guided Q&A (what are you
   creating — Email / Banner ad / Social ad / Something else; audience,
   dimensions; optional reference materials; objective). **Continue** →
   **Start**.
2. Agent chat at `/home/intake/{operationId}/ask` refines the request.
3. Blueprint review at `/home/blueprint/{requestId}`: **Edit**/**Save**,
   **Revise with AI**, then **Submit request** (goes to staff review) or
   **Generate** (auto-approve brands) with a save-to-project step
   (pick or create project, file name, folder).
4. Auto-approve path shows `/home/generating/{id}` then lands on the asset.

### Edit existing content (bring a file)
Home → **Edit content** → wizard: job code → content type → PRC template →
source file (upload or from project) → name & save → opens the asset editor.

### Asset workspace (`/home/assets/{id}`)
- Header: version dropdown (Vn), **PRC | Content** view toggle.
- **Approve** (non-staff): opens "Approve version Vn" dialog, requires a note,
  creates an approval request and locks the asset "in review".
- **Edit content** → inline HTML editing → **Save Changes** (members save
  final; staff save draft until Publish).
- **Tools/Actions** menu (content-dependent): Swap ISI, Email Settings,
  Rearrange References, QC, Add Job Code, Veeva JobCode Injection, etc.
- **Export** dropdown: Create Shareable Link, Export to Veeva, HTML/PDF.
- Right rail: **Brief** / **Comments** (annotations via comment/callout
  markup tools).
- Bottom: **Run/View content report card** → `/home/assets/{id}/report-card`
  with **Fact Check / ISI / Quality Check** steps.

### Projects
- `/home/projects`: **+ New project** (name dialog), rename/delete, search.
- `/home/projects/{id}`: **Upload File**, **New Folder**; per-file menu:
  View, Download, Edit details, Move File, Duplicate, Delete.
  (Folder creation and deletes exist ONLY here — no MCP tool covers them.)

### Review lifecycle (member view)
Submit request → asset shows under Awaiting Review on `/home` and
`/home/in-review-requests` → staff processes it (see admin map) → member gets
a notification (bell) that deep-links back to the asset.

### Brand and account settings
- `/brand-profile`: brand metadata, rules, ISI entries
  (`/brand-profile/isi/{isiType}`), team invites; role changes staff-only.
- `/profile`: profile + password tabs.
- `/home/create-brand`: brand creation wizard (creator becomes brand ADMIN).
- `/brand`: first-brand onboarding stepper for new accounts.

## Role differences on shared pages

- Members see **Approve** on assets; staff see draft-save + Publish instead.
- Content saved by a member becomes a **final** version; by staff a **draft**.
- Project rows gain staff-only items (Open in workspace, Change owner,
  Upload Source Material).
- `/home/requests` and `/home/review-request/*` redirect non-staff to `/home`.
