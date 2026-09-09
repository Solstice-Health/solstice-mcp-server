# /fix-to-v2 Skill Design

## Goal

Give a non-technical platform user a one-prompt fix for an operation whose PRC
proof is missing, legacy v1, or flagged "Fix to v2": point any agent at the
asset link and get a new document version with identical creative content and a
freshly composed Contract v2 proof.

## Origin

Manual repair of operation `ab35fc54` (sanofi_sandbox, 2026-09-09) proved two
failure modes of the agent-authored path:

1. Hand-authoring a Contract v2 bake is multi-round and expert-only.
2. Weak models loop on the server validator, burning repeated
   prepare/PUT/commit attempts against an opaque error.

The MCP server already composes a Contract v2 proof server-side on every HTML
commit (PR #41 parity work, verified on sanofi_sandbox). The skill therefore
contains no bake authoring at all.

## Design

Skill file: `plugins/solstice-platform/skills/fix-to-v2/SKILL.md`.

Flow: parse asset deep link (subdomain → tenant, trailing UUID → operation
id) → gate on PRC availability via `solstice_prc_template` → no-op stop when
the existing proof is already v2-clean → one user confirmation → re-commit the
head creative's exact bytes (`solstice_prepare_operation_version` → PUT →
`solstice_commit_operation_version` with `base_message_id`) → verify the new
head carries a `prc_proof_url` whose body has the v2 + baked markers and no
legacy annotation tokens → report the asset link.

Anti-loop hard rules:

- never hand-author or hand-repair a bake on this path;
- at most 2 commit attempts; the only retryable failure is
  `conflict: not_latest_document` (re-read head, rebase once);
- `confirmation_required` is explained to the user and retried only with
  explicit approval;
- reuse the operation's existing file name for `file_name` (PromptAttack
  guardrail scans this field);
- verification failure after a successful commit → stop and escalate to
  `prc-template-recreation`, do not keep committing.

## Testing

Isolated subagent (fresh context, skill file + asset URL only) against
sanofi_sandbox:

1. `b015a5a5-e0ec-41cb-804f-6729727e8926` — legacy/missing proof → expect one
   new draft version, v2-clean proof, asset link.
2. `ab35fc54-f4b9-4cfa-aa8f-8eee19429b18` — repaired 2026-09-09 → expect the
   already-v2 no-op stop with zero writes.

## Delivery

Feature branch from `main`, version bump 0.3.28 → 0.3.29 in the five plugin
manifests, PR into `main`.
