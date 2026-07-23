# Operation Asset Links Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Return a clickable Solstice asset URL after every operation create, version commit, and approval, and require agents to present that link to users.

**Architecture:** A pure helper in `tenants.py` owns tenant-host normalization and URL construction. Operation write functions add its result to existing response dictionaries without changing current fields. Server and plugin instructions make presenting the returned URL part of the agent handoff contract.

**Tech Stack:** Python 3.12, FastMCP, pytest, Ruff, Markdown plugin skills.

## Global Constraints

- Use `https://www.<tenant-host>.solsticehealth.co/home/assets/<operation_id>` for every operation category.
- Convert underscores in `tenant_slug` to hyphens in the host.
- Preserve every existing response field.
- Return no asset link from prepare or failed writes.
- End the user-facing agent response with `[Open asset in Solstice](<asset_url>)`.
- Keep authentication and RBAC unchanged.

---

### Task 1: Add asset URLs to write responses

**Files:**
- Modify: `src/solstice_mcp/tenants.py`
- Modify: `src/solstice_mcp/operations.py`
- Test: `tests/test_tenants.py`
- Test: `tests/test_create_operation.py`
- Test: `tests/test_create_edit_operation.py`
- Test: `tests/test_versions.py`
- Test: `tests/test_staff_tools.py`

**Interfaces:**
- Produces: `operation_asset_url(tenant_slug: str, operation_id: str) -> str`
- Consumes: successful response dictionaries from create, commit, and approve operations.

- [ ] **Step 1: Write failing URL-helper and response tests**

Add assertions equivalent to:

```python
assert operation_asset_url("sanofi_sandbox", "op-123") == (
    "https://www.sanofi-sandbox.solsticehealth.co/home/assets/op-123"
)
assert payload["asset_url"] == (
    f"https://www.sanofi-sandbox.solsticehealth.co/home/assets/{operation_id}"
)
```

Cover generated create, edit create, document commit, source commit, first approval,
and already-final approval. Assert prepare responses do not contain `asset_url`.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
pytest tests/test_tenants.py tests/test_create_operation.py \
  tests/test_create_edit_operation.py tests/test_versions.py \
  tests/test_staff_tools.py -x
```

Expected: failure because `operation_asset_url` or `asset_url` does not exist.

- [ ] **Step 3: Implement the helper and response fields**

Add:

```python
def operation_asset_url(tenant_slug: str, operation_id: str) -> str:
    tenant_host = tenant_slug.replace("_", "-")
    return (
        f"https://www.{tenant_host}.solsticehealth.co"
        f"/home/assets/{operation_id}"
    )
```

Import it in `operations.py`. Add:

```python
"asset_url": operation_asset_url(tenant_slug, operation_id),
```

to generated/edit create (through the shared create response), source commit,
HTML/PDF commit, first approval, and already-final approval.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the command from Step 2.

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add src/solstice_mcp/tenants.py src/solstice_mcp/operations.py \
  tests/test_tenants.py tests/test_create_operation.py \
  tests/test_create_edit_operation.py tests/test_versions.py \
  tests/test_staff_tools.py
git commit -m "Return asset links from MCP operation writes"
```

### Task 2: Require agents to present the link

**Files:**
- Modify: `src/solstice_mcp/app.py`
- Modify: `src/solstice_mcp/tools/content.py`
- Modify: `plugins/solstice-platform/skills/solstice-platform/SKILL.md`
- Modify: `plugins/solstice-platform/skills/solstice-platform/references/actions.md`
- Test: `tests/test_server.py`
- Test: `tests/test_plugin_package.py`

**Interfaces:**
- Consumes: the `asset_url` returned by Task 1.
- Produces: one consistent agent handoff instruction for all covered writes.

- [ ] **Step 1: Write failing instruction-contract tests**

Assert server instructions, affected tool descriptions, and plugin guidance contain
`asset_url`, `Open asset in Solstice`, and guidance not to use a bare UUID as the
primary handoff.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
pytest tests/test_server.py tests/test_plugin_package.py -x
```

Expected: failure because the handoff instructions are absent.

- [ ] **Step 3: Add the agent handoff contract**

Add this rule to server instructions and plugin guidance:

```text
After a successful create, version commit, or approval, end the user-facing
response with `[Open asset in Solstice](<asset_url>)` using the returned
`asset_url`. Do not give a non-technical user only an operation UUID.
```

Update the create, edit-create, commit, and approve tool docstrings to state that
their successful response includes `asset_url` and that the agent must present it.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the command from Step 2.

Expected: all selected tests pass.

- [ ] **Step 5: Run repository verification**

Run:

```bash
ruff check .
pytest
```

Expected: Ruff reports `All checks passed!`; pytest reports zero failures.

- [ ] **Step 6: Commit Task 2**

```bash
git add src/solstice_mcp/app.py src/solstice_mcp/tools/content.py \
  plugins/solstice-platform/skills/solstice-platform/SKILL.md \
  plugins/solstice-platform/skills/solstice-platform/references/actions.md \
  tests/test_server.py tests/test_plugin_package.py
git commit -m "Require clickable asset links in MCP handoffs"
```
