---
name: browser-tenant-launch
description: Fall back to driving the Solstice web UI in a browser when the user's goal is not achievable with the available Solstice MCP tools. Use when a requested action has no matching MCP tool (e.g. deleting an asset, creating a project or folder, managing brand members, sending a review, admin dashboard work), or when the user explicitly asks to "open Solstice", "show me in the app", or "do it on the site". Launches https://{tenant-slug}.solsticehealth.co, asks the user to log in, and navigates the UI using the bundled route/action map.
---

# Browser tenant launch (UI fallback)

The Solstice MCP covers a deliberate subset of the platform: discovery, reads,
append-only document writes, memory, and a few staff actions. Everything else
lives only in the web UI. When the user's goal needs one of those UI-only
capabilities, do not improvise with MCP tools or claim the change is
unsupported and stop — offer to drive the web UI in a browser instead.

## When to use this skill

1. The user's goal has **no matching MCP tool**. Common examples:
   - delete / archive an asset, project, or folder
   - create a project or a folder inside a project
   - send an asset for review, request changes, leave review comments
   - invite / remove brand team members, change member roles
   - brand setup: guidelines, design library, claims upload
   - anything on the admin dashboard not covered by the request-triage tools
2. The user explicitly asks to see or do something "in Solstice" / "in the app".

If an MCP tool DOES cover the goal, always prefer the tool — it is faster,
auditable, and role-safe. This skill is the fallback, not the default.

## Protocol — ask first, then launch

1. **Ask before launching.** Tell the user the action is not available through
   this Solstice connection and ask whether they want to do it in the web app.
   Never open the browser unprompted.
2. **Derive the URL** from the tenant slug: `https://www.{slug}.solsticehealth.co`,
   where underscores in the slug become hyphens
   (`sanofi_sandbox` → `www.sanofi-sandbox.solsticehealth.co`). Discover slugs
   with `solstice_list_tenants` if not already known.
3. **Hand login to the user.** The app authenticates via the tenant's login
   page. Never ask for, capture, or type the user's credentials; wait until
   they confirm they are signed in.
4. **Navigate using the map.** Use [UI map: main app](references/ui-map-main.md)
   and [UI map: admin dashboard](references/ui-map-admin.md) to find the route
   and the on-page actions for the goal. Deep links follow
   `/home/...` patterns (e.g. `/home/assets/{operation_id}`), so IDs learned
   from MCP tools can be pasted straight into the URL.
5. **Confirm destructive steps.** Deleting or archiving anything, removing a
   member, or dismissing a request in the UI needs an explicit user yes for
   that specific item, even mid-flow.
6. **Report what happened.** Summarize the UI steps taken and the end state.
   If a page or control does not match the map (the UI evolves), say so and
   let the user take over rather than guessing through unfamiliar screens.

## Role awareness

The UI shows different controls per role (MEMBER, ADMIN, SOLSTICE_STAFF —
staff role is per-brand). If an expected control is missing, the signed-in
user likely lacks the role; report that instead of hunting for workarounds.
The maps note role-gated pages and actions.

## References

- [UI map: main app](references/ui-map-main.md) — routes, navigation, and
  per-page actions for the user-facing app.
- [UI map: admin dashboard](references/ui-map-admin.md) — staff/admin routes,
  triage queue, and management actions.
