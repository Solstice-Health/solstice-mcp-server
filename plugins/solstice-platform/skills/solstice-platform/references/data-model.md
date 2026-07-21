# Data and access model

- A **workspace** is a tenant with its own database. Every workspace-bound call includes the selected `tenant_slug`.
- A **brand** belongs to a workspace. The server returns only brands where the signed-in user has a live membership.
- A **project** belongs to a brand and may contain a folder map whose leaves point to content review operations.
- A **content review** is an operation with metadata, conversation messages, and document versions.
- A **message** may contain text or refer to an HTML, PDF, or blueprint document. HTML bodies may be returned as time-limited links or fetched on explicit request.

The server derives access from the OAuth token and checks it again on each request. Brand roles are `MEMBER`, `ADMIN`, and `SOLSTICE_STAFF`. Roles are brand-specific, not workspace-wide.

`MEMBER` and `ADMIN` users see final document messages. `SOLSTICE_STAFF` users may also see drafts. The server enforces this rule. The client must not infer that an absent draft or inaccessible resource exists.
