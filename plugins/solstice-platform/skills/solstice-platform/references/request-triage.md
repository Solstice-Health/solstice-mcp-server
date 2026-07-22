# Request triage (Solstice staff)

A **request** is one row the platform records each time a user asks for something: saving an asset to a project (`initial_save`), submitting a change-request batch (`change_request_complex` / `change_request_review`), or asking for a manual approval such as a Veeva upload (`approval_request`). Status is `pending`, `completed`, or `dismissed`. Requests are permanent audit records: they survive even when their asset is deleted (such rows return `operation_deleted: true`) and can never be deleted — only dismissed.

## "What's on my plate today?"

1. Call `solstice_list_tenants` to get the user's workspaces.
2. For each workspace, call `solstice_list_requests(tenant_slug)` — the default `status="pending"` is the open queue. The server allows this only when the signed-in user is Solstice staff on at least one brand in that workspace; skip workspaces that return an authorization error.
3. Present the pending items grouped by brand, newest first (the server's order). Each row carries `display_name`, `request_type`, `requester` (name/email), `priority`, `assigned_to`, and for approval requests the user's `message`.
4. "Mine" = rows whose `assigned_to.user_id` matches the caller (get it from `solstice_whoami`). Show unassigned and other-staff rows separately rather than hiding them.

Narrow with `brand_id` or `status` (`pending` / `completed` / `dismissed` / `all`) when the user asks for a specific slice. Change-request rows include a `comment_count` but not the full comment payloads; the platform dashboard is the place to read those.

## Dismissing a request

`solstice_dismiss_request(tenant_slug, request_id, reason_category, reason_text)` closes one **pending** request without producing an asset — for duplicates, mistakes, or out-of-scope asks.

- **Always ask the user why before dismissing.** The reason is mandatory: `reason_category` is one of `duplicate`, `invalid`, `out_of_scope`, `other`; `reason_text` is an optional note (max 500 characters) — use it whenever the category alone doesn't tell the story.
- Dismissal requires Solstice staff **on that request's own brand** — being staff elsewhere in the workspace is enough to read the queue but not to dismiss.
- Dismissal is terminal: `completed` and already-`dismissed` rows are rejected; there is no undo tool. Confirm the target with the user before calling.
- Dismissing never touches the linked asset, its review, or the requester's notifications, and it never deletes the request row. Rows with `operation_deleted: true` can still be dismissed.

Completing a request (publishing the asset back to the user) is a platform action, not available through this connection — offer only dismissal here.
