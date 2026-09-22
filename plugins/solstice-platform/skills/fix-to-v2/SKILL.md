---
name: fix-to-v2
description: Rebuild a Solstice asset's PRC proof at Contract v2 by re-committing its current creative unchanged, so the server composes a fresh proof. Use when a user reports a missing or broken proof, a "Fix to v2" or "proof template incomplete" badge, "legacy annotation format", or pastes an asset link and asks to fix its PRC template/proof.
---

# Fix to v2

Append a new document version to one operation with the **same creative
content** and let the server compose a fresh Contract v2 proof. This is the
default fix for missing, legacy, or "Fix to v2"-flagged proofs.

The server is the only proof composer on this path. Never hand-author,
hand-repair, or locally patch a bake here — that is the
`prc-template-recreation` skill's job, and it is the escalation target, not a
step of this skill.

## Input

Accept a Solstice asset link or a bare operation id.

- Link: `https://{subdomain}.solsticehealth.co/home/assets/<operation_id>`
  (also `/home/generating/`, `/home/review-request/`). Strip a leading `www.`
  from the subdomain; pass it as `tenant_slug` as-is (hyphens are accepted).
- Bare id: ask once which workspace, then proceed.
- If the user gives no id at all, stop and ask for the asset link.

## Proof gate script

All "is this proof v2?" judgments use the bundled checker — never eyeball
markers, never grep raw proof HTML for class names (the v2 viewer's own
platform JavaScript legitimately contains legacy-looking class hooks; raw
grep false-positives on correct proofs):

```bash
python3 <this-skill's-directory>/scripts/check_proof.py <proof.html>
```

Prints `V2` (exit 0, no reason lines), `LEGACY` (exit 1, with reasons), or
`INVALID` (exit 2, the download is not HTML — e.g. an S3 error body from an
expired presigned URL). Treat `V2` as authoritative. On `INVALID`, re-call
`solstice_operation_html` for fresh URLs and retry the download once.

## Workflow

1. **Gate.** `solstice_operation_info(tenant_slug, operation_id)` — confirm
   the operation exists; note `file_name`, `brand_id`, `project_id`. The
   response carries no `content_type`: read it from
   `solstice_project_info(tenant_slug, project_id)` — find the operation's
   dir_map leaf by matching `operation_id` (names can collide across leaves).
   Then call `solstice_prc_template(tenant_slug, brand_id,
   content_type)` with that exact content type. If no template resolves
   (`not_found`), stop: PRC is unavailable for this content type — no write,
   say so plainly.
2. **Head.** `solstice_operation_messages` → `head_message_id` and
   `display_version` (call it Vn).
3. **No-op check.** `solstice_operation_html(message_id=head_message_id)`. If
   `prc_proof_url` exists, GET it to a temp file and run the gate script. On
   `V2`, stop: the proof is already Contract v2 — make zero writes and tell
   the user. On `LEGACY` (or no proof at all), continue.
4. **Confirm.** One sentence: "This adds V(n+1) with identical content and a
   rebuilt Contract v2 proof — go ahead?" Skip only if the user already
   approved the write in their request.
5. **Re-commit.** GET the creative `url` from step 3 and keep the exact bytes.
   `solstice_prepare_operation_version(tenant_slug, operation_id,
   type="html", file_name=<file_name from step 1>)` — pass the operation's
   `file_name` verbatim, even if it has spaces or no extension; never prose
   (a guardrail scans it). PUT the bytes to `upload_url`, then
   `solstice_commit_operation_version(..., s3_key, file_name,
   base_message_id=<head from step 2>)`.
6. **Verify.** Re-read messages: a new head V(n+1) must exist. Call
   `solstice_operation_html` on it: `prc_proof_url` must be non-null. GET it
   and run the gate script — expect `V2`. Report the new version and the
   `asset_url` as a markdown link.

## Hard rules — no commit loops

- **At most 2 commit attempts, ever.** The only retryable failure is
  `conflict: not_latest_document`: re-read the head, redo step 5 once with the
  new `base_message_id`.
- Any other commit or validation error: stop immediately and report the
  verbatim server error. Do not tweak inputs to "try again".
- `confirmation_required` means a newer version exists that the caller cannot
  read: tell the user their proof is behind, and retry with `confirmed=true`
  only if they explicitly say to.
- If step 6 fails after a successful commit, do not commit again. Report the
  state and escalate: the fix from there is the `prc-template-recreation`
  skill (manual bake repair) or a platform bug report with the verbatim error.
- Never overwrite, delete, or "clean up" existing versions or S3 objects.
- The appended version's intent comes from the caller's token (staff →
  draft). Mention that staff can flip it with
  `solstice_approve_operation_version`; do not approve unprompted.

## Output contract

- One of: "already v2, nothing changed" / new version number + asset link /
  verbatim error with the named next step.
- Never present a bare operation UUID as the result; the commit response's
  `asset_url` is the deliverable.
