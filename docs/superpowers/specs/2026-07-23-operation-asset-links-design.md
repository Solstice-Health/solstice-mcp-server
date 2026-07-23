# Operation Asset Links

## Goal

After an MCP agent creates an operation, commits a document version, or approves
a version, it must give the user a clickable Solstice asset link instead of
leaving them with only an operation UUID.

## Link contract

Every operation uses this user-facing URL:

`https://www.<tenant-host>.solsticehealth.co/home/assets/<operation_id>`

The tenant host is the tenant slug with underscores converted to hyphens. For
example, `sanofi_sandbox` becomes `sanofi-sandbox`.

The link does not bypass authentication or authorization. Solstice continues to
enforce the signed-in user's tenant and brand access.

## Server behavior

Add one shared URL-building helper and include an `asset_url` field in successful
responses from:

- `solstice_create_operation`
- `solstice_create_edit_operation`
- `solstice_commit_operation_version` for HTML, PDF, and source commits
- `solstice_approve_operation_version`, including idempotent already-final calls

Do not add a link to `solstice_prepare_operation_version`; preparation does not
mean the upload was committed successfully.

Existing response fields, including `operation_id`, remain unchanged for
backward compatibility.

## Agent behavior

Update the MCP server instructions, affected tool descriptions, and Solstice
Platform skill guidance so that after a successful covered write the agent ends
its user-facing response with:

`[Open asset in Solstice](<asset_url>)`

The agent may include the operation UUID for technical debugging, but it must not
present the UUID as the primary handoff to a non-technical user.

## Failure behavior

No link is returned or presented when the write fails. URL construction is
deterministic and local; it introduces no additional database, frontend, or
network dependency.

## Verification

- Unit-test underscore-to-hyphen tenant normalization.
- Assert `asset_url` on create and edit-create responses.
- Assert `asset_url` on HTML/PDF/source commit responses.
- Assert `asset_url` on first approval and already-final approval responses.
- Run Ruff and the focused MCP test files.
