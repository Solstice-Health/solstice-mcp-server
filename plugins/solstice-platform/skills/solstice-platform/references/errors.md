# Errors and safe responses

- **401 or authentication required:** ask the user to reconnect Solstice OAuth, then retry after sign-in.
- **403 or missing scope:** say the connection lacks the required `mcp:connect` permission and must be reauthorized.
- **Access denied or `not_member`:** say the signed-in account cannot access the selected workspace or item. Do not confirm whether a hidden item exists.
- **Not found:** say the item could not be found among the resources available to this account. Re-list the parent collection before asking the user to choose again.
- **Result too large or truncated:** present the available summary and ask the user to narrow the brand, project, review, date range, or document.
- **Service unavailable:** say Solstice could not complete the read and suggest retrying later. Do not expose provider exception text.
- **Upload failed:** do not call the commit tool. Say the file was not added and ask whether the user wants to retry.
- **Commit failed:** do not retry automatically. Say the new version was not committed and preserve the error's safe next step.
- **Unsupported write:** say that only append-only document versions are supported and no other change was made.

Never invent missing results, retry with a different workspace without consent, or use content from an earlier failed request as if it were current.
