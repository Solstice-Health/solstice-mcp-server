# Errors and safe responses

- **401 or authentication required:** ask the user to reconnect Solstice OAuth, then retry after sign-in.
- **403 or missing scope:** say the connection lacks the required `mcp:connect` permission and must be reauthorized.
- **Access denied or `not_member`:** say the signed-in account cannot access the selected workspace or item. Do not confirm whether a hidden item exists.
- **Not found:** say the item could not be found among the resources available to this account. Re-list the parent collection before asking the user to choose again.
- **Result too large or truncated:** present the available summary and ask the user to narrow the brand, project, review, date range, or document.
- **Service unavailable:** say Solstice could not complete the read and suggest retrying later. Do not expose provider exception text.
- **Upload failed:** do not call the commit tool. Say the file was not added and ask whether the user wants to retry.
- **Commit failed:** do not retry automatically. Say the new version was not committed and preserve the error's safe next step.
- **`conflict: not_latest_document`:** a new version landed while you were working, so nothing was written. For a document commit, re-read the review, reapply the change on top of the new `head_message_id` (row `id`), and commit that. For an operation bake, re-read, rebuild from the new head, prepare and upload again, then retry `solstice_create_prc_template_version`. Never re-send the stale `base_message_id`.
- **`confirmation_required` on commit:** a newer version exists that this account cannot read, so re-reading will not help. Say the edit is not based on the latest version and that saving it supersedes that newer version, then retry with `confirmed=true` only on an explicit yes. In a multi-asset batch, skip the asset and report it instead.
- **`invalid_request: base_message_id is required`:** the read was skipped. Call `solstice_operation_messages` and take `head_message_id`. Use it for the document commit, or rebuild the operation bake from that head and pass it to `solstice_create_prc_template_version`. Document commits omit it only when `head_message_id` is null; operation bakes always require it.
- **PRC template conflict:** say another version was created concurrently and ask whether to retry appending the next version; never overwrite.
- **Unsupported write:** say the requested change is not supported and no change was made.

Never invent missing results, retry with a different workspace without consent, or use content from an earlier failed request as if it were current.
