# Brand video generation workflow

End-to-end protocol for generating a brand-faithful video locally from Solstice
brand context, using an external video provider. Nothing lands in Solstice.

## Architecture note

- **Solstice MCP** — brand rules / claims / design assets (read-only).
- **Agent** — writes the script and storyboard, drives the provider, checks the
  cut against brand rules.
- **External provider** — renders the video. HeyGen by default; provider-agnostic
  otherwise. The agent provider runs locally, so it can register and connect a
  provider MCP or call a provider REST API directly.

Video and preview artifacts are local files. The MCP version-upload path accepts
html/pdf only, so the video is never uploaded to Solstice.

## 1. Gather brand context

Resolve workspace and brand with the `solstice-platform` skill sequence, then:

1. `solstice_brand_rules(tenant_slug, brand_id)` — guidelines, `isi`, `drug_info`
   (tone, mandatory safety chrome, do/don't language).
2. `solstice_brand_claims(tenant_slug, brand_id)` — approved claim text for
   voiceover and on-screen copy. Use `claim_text` verbatim.
3. `solstice_brand_design_assets(tenant_slug, brand_id)` — logos, hero imagery,
   and palette references (time-limited URLs) to keep the video on-brand.

## 2. Connect a video provider (connection ladder)

The agent provider runs locally, so it can register and connect. Walk the ladder
in order and stop at the first that works:

1. **HeyGen MCP already connected** — use it directly.
2. **Register HeyGen MCP** — ask the user to connect HeyGen or paste the HeyGen
   MCP URL in chat. On a pasted URL, add a new MCP server entry to the local
   provider config (for Cursor, `~/.cursor/mcp.json`) and connect. This edits the
   local provider config only, never the repository and never the Solstice
   sibling registry.
3. **HeyGen REST** — if there is no HeyGen MCP, ask the user for a HeyGen API
   token supplied locally as an environment variable (for example
   `HEYGEN_API_TOKEN`). Call HeyGen REST directly: submit a generate request,
   poll the status endpoint until complete, then download. Send the token in the
   provider's documented authentication header; if unsure of the exact header or
   payload, do step 5.
4. **Any other provider** — if the user has no HeyGen at all, ask which video
   generator they have credit for (for example Google Veo 3, Runway, Kling, Luma,
   Pika) and use that instead: via its MCP if one exists (same register-locally
   step as 2) or its REST API with a user-supplied token in a provider-named
   environment variable (for example `{PROVIDER}_API_TOKEN`).
5. **Unknown provider API** — if the agent does not know the chosen provider's
   endpoints or payload, web-search at skill-execution time for the current API
   docs, then present the concrete options and steps to the user before
   generating.
6. **Nothing available** — if there is no MCP and no token for any provider,
   stop and tell the user exactly what to provide. Never fabricate a video.

Generation proceeds only when the account has credit. Never write a real token
into any repository file or into memory.

## 3. Script and storyboard (human-in-loop)

Produce a scene-by-scene plan:

- Voiceover and on-screen text drawn only from verbatim `claim_text` and the
  brand's approved language.
- Required safety chrome from `isi` / `drug_info` placed per brand rules (for a
  DTC spot this usually means fair-balance safety and an ISI reference).
- Visual direction consistent with the design assets (logo usage, palette,
  hero imagery).

Show the plan to the user and iterate. Do not call a paid generator until the
user approves the script and storyboard.

## 4. Generate

Call the connected provider with the approved plan:

- Submit the generation request (avatar/scene/voice parameters per the plan).
- Poll the provider's status until the render completes.
- Download the finished video to a local path and report where it is.

Handle provider errors explicitly (quota, credit, moderation) and tell the user
the specific next step rather than silently retrying.

## 5. Preview and compliance self-check

1. Optionally build a local HTML preview page (embedding the local video path)
   so the user can review scenes and captions.
2. Self-check the cut against brand rules: claims verbatim, safety chrome
   present and legible, tone on-brand, logo/palette correct.
3. Iterate on the script/storyboard and regenerate as the user requests.

## Out of scope

- Any Solstice write (no create/prepare/commit). The video stays local.
- Reimplementing a self-hosted generative pipeline (Omni/Flux/Wan) or an
  in-app editor.
- Hardcoding a single vendor beyond HeyGen as the default.
- Storing tokens or generated media in Solstice memory.
