# Solstice PRC Template Contract v2

Contract version: `v2`

This is the authoring contract for every stored PRC proof template: email,
banner, social, and website. A template declares its profile, lays out pages,
provides creative slots, seeds presentation config, and marks editable fields.
The platform hydrates values, injects creative and runtime behavior, paints
everything around the pages, and draws every annotation.

## Mental model

A template owns six layers and nothing outside them. It draws the inside of each
page and stops at the page edge: the backdrop behind the pages, the spacing
between them, their centering, and their drop shadow all belong to the platform,
which paints them differently in the workspace and in an export. Each page rect
is the geometry boundary: fields, mirrors, derived values, creative slots,
callouts, and arrow endpoints stay clamped inside their assigned page. Cover-edit
writes layout, inserted L5 nodes, slot boxes, and annotation pins into the next
operation bake (`prc_template_s3_key`). Catalog templates stay annotation-free;
the overlay is still injected at view time. If an edit preserves layers L0-L5
and does not author anything in the reserved namespace, the proof can render and
export through the same contract.

## The six layers

| Layer | Responsibility | Required vocabulary |
|---|---|---|
| **L0 Declaration** | Contract version and profile | `<meta name="sol-prc-contract" content="v2" data-profile="...">` and the instruction comment |
| **L1 Profile** | Selects exactly one compose branch | `body[data-sol-prc-proof="email\|banner\|social\|website"]` |
| **L2 Pages** | Defines page rectangles and annotation boundaries | `main[data-sol-prc-pages]` containing `[data-sol-prc-page="ID"][data-sol-prc-page-type="cover\|render\|storyboard"]` |
| **L3 Creative slots** | Marks only the iframes the platform hydrates | `iframe[data-sol-prc-creative="desktop\|mobile\|social\|banner"]` |
| **L4 Config seed** | Supplies presentation-only JSON | one `script#sol-prc-config[type="application/json"]` |
| **L5 Fields** | Marks editable, mirrored, and derived values | `data-sol-prc-field`, `data-sol-prc-mirror`, `data-sol-prc-derived`, and stable cover IDs |
| **Reserved** | Runtime-owned and forbidden in templates | the namespace listed below |

### L0 — Declaration

Every template starts with one declaration whose profile matches L1:

```html
<meta name="sol-prc-contract" content="v2" data-profile="email">
<!-- SOLSTICE PRC TEMPLATE - CONTRACT v2 (profile: email)
  Editing this file outside Solstice (Cursor, etc.):
  - NEVER remove/rename anything carrying data-sol-prc-*
  - NEVER add annotation/callout markup, CSS, or JS - the platform
    draws all annotations bounded within each page
  - NEVER declare --prc-* CSS variables - runtime-owned
  - NEVER style outside the page box (backdrop, page gaps,
    centering, shadow, @page) - the platform paints those
  - NEVER link external fonts - platform-listed fonts only
  - Keep operation creative out of this file - it is injected
  Full rules: solstice_prc_template_rules via the Solstice MCP -->
```

The meta element is machine-readable. The adjacent comment protects the same
seams when a human or agent edits a downloaded file without MCP context.

### L1 — Profile

The body has exactly one explicit `data-sol-prc-proof` value. This applies to
banner too; v1's banner detection by config or script contents is only a
migration alias. Cross-profile markers are invalid because banner detection
historically won and silently selected the wrong compose branch.

### L2 — Pages

- One `main[data-sol-prc-pages]` wraps the proof pages.
- Every page has a stable `data-sol-prc-page` ID and
  `data-sol-prc-page-type="cover|render|storyboard"`.
- Page IDs are unique in the composed document. Runtime-cloned banner sections
  are restamped with unique composed IDs (`page_banner_0`, `page_banner_1`, ...)
  before annotation discovery; a template never authors duplicate page IDs.
- Email and website render pages carrying creative also have
  `data-viewport="desktop|mobile"`.
- The rendered page rect is the geometry boundary for fields, slots, callouts,
  and arrow endpoints. Page CSS may use padding, borders, and brand chrome, but
  the catalog template provides no annotation gutters, connector SVG, stages, or
  reserved whitespace.
- A template styles the inside of a page only. The canvas behind the pages, the
  gap between them, their horizontal centering, their drop shadow, and all
  `@page` / `@media print` geometry belong to the platform, which paints a gray
  canvas with gaps in the workspace and a flush white sheet on export.

### L3 — Creative slots

Only `iframe[data-sol-prc-creative]` elements receive creative. The platform
sets `srcdoc`, `scrolling`, `loading`, viewport metadata, and slot width
(600px desktop, 375px mobile). The v1 fallback that injected into every iframe
is not part of v2.

Banner keeps one authored `[data-banner-section]`; the platform clones it for
multiple dimensions. Banner slot iframes can live in `#frame-template` and
`#isi-region-template`, but they still carry
`data-sol-prc-creative="banner"`. The movable box is `[data-sol-prc-slot]`
around that iframe — CSS classes are visual only. Cover-edit Next may
reposition a slot inside its page; that box is written onto
`[data-sol-prc-slot]` in the next bake, or onto the iframe when the marker is
absent. Catalog templates still author the slot in place.

### L4 — Config seed

Exactly one `script#sol-prc-config[type="application/json"]` contains a JSON
object. It is a presentation seed for layout chrome, not operation field
values or platform state.

| Profile | Template-owned keys | Platform-owned keys that must not be seeded |
|---|---|---|
| email | `sectionTitle`, `enumeration`, `toStyle`, `fromStyle`, `fromSecondary`, `sectionList` | `filename`, `to`, `from`, `options[]` values |
| website | email layout keys plus `url`, `pageTitle`, `description` layout hints | `filename` and operation field values |
| banner | `frames_per_row`, `show_focus_page`, `inline_isi_columns`, `dim_layouts`, `banner_scale`, `isi_eyebrow_html`, `isi_columns`, `footer` | `title`, `dimensions`, `scenes`, `expand_endpoint`, `expand_model`, tenant/auth values |
| social | `sectionTitle` | `filename` and platform/variant grouping |

Banner retains the executable behavior seams `#banner-scene-adapter`,
`#banner-placeholder-srcdoc`, `[data-banner-section]`, `#frame-template`, and
`#isi-region-template`. Social retains `#prc-platform-page-tpl`,
`#prc-variant-cell-tpl`, `#prc-storyboard-page-tpl`, and
`#prc-frame-cell-tpl`. Those seams build profile pages; they do not draw
annotations.

### L5 — Fields

- Every visible template value exposed to field editing carries exactly one
  role: `data-sol-prc-field="FIELD_ID"`,
  `data-sol-prc-mirror="FIELD_ID"`, or
  `data-sol-prc-derived="FIELD_ID"`.
- IDs are normalized canonical IDs for logical values. The same logical value
  uses the same exact `FIELD_ID` on every rendered page instance; page, clone,
  or dimension suffixes do not create new field identities.
- `data-sol-prc-field` is the primary value owner and the only value-editable
  role. Mirrors repeat that primary value and derived fields display runtime
  computations. Mirrors and derived fields may be selected for field layout
  and style controls, but their values are locked.
- Repeated banner dimensions mirror the first section with the same canonical
  `data-sol-prc-mirror="FIELD_ID"`. Computed banner cumulative durations use
  `data-sol-prc-derived="frame_cumulative_INDEX"`.
- Every node carrying a `data-sol-prc-*` editing marker — field, mirror,
  derived, inserted, or slot — is selectable, movable, and deletable in
  cover-edit with the standard engine chrome, on any content type's template.
  Nodes stay authored in-flow until a layout gesture; the first drag/resize
  pins that instance page-absolute (clamped to its page) and freezes into the
  next bake. Deletion removes the node from the proof DOM and the next bake.
- Allowlisted text-style changes still write onto matching instances. The next
  bake freezes the proof HTML. Next-engine does not persist new
  `__prc_field_overrides`.
- Cover-edit Next may insert Text / Image / Button onto a page as
  `data-sol-prc-field="inserted_{kind}_{n}"` plus `data-sol-prc-inserted`.
  Inserts are page-local.
- Stable email cover IDs are `#prc-filename`, `#prc-to`, `#prc-from`,
  `#prc-options`, and `#prc-option-tpl`. Option rows become `subject_INDEX` and
  `preheader_INDEX`.
- Stable banner fields include `file_name`, `frame_label_INDEX`,
  `frame_duration_INDEX`, `frame_time_label_INDEX`,
  `frame_cumulative_label_INDEX`, `frame_note_label_INDEX`, and
  `animation_note_INDEX`.
- New field IDs may use the generic path. Renaming or deleting an existing ID
  is a breaking edit.

## Reserved runtime-owned namespace

A v2 template must never author:

- any `--prc-*` CSS custom property, including `--prc-desktop-width`,
  `--prc-mobile-width`, `--prc-fit-scale`, `--prc-user-zoom`, and
  `--prc-total-scale`;
- annotation DOM, CSS, or JavaScript, including `.prc-callout`,
  `.prc-connector-line`, `.callout-dot`, `.prc-callout-gutter`,
  `.prc-connector-svg`, `.prc-render-stage`, `.callout-overlay`,
  `.callout-box`, `.callout-line`, `data-sol-prc-annotation-key`, or any
  script that computes callout geometry;
- `__prc_annotation_positions`, which is legacy operation draft data;
- `__prc_field_overrides` or generated field-override CSS, which are legacy;
- `script#sol-prc-annotation-positions` in a **catalog** template (operation
  bakes may carry it after a drag; overlay DOM is still injected);
- `window.__BANNER_TEMPLATE_*`, `#sol-prc-template-runtime*`, or
  `#sol-prc-banner-template-data`, which compose injects;
- the canvas outside the pages: a backdrop on `html` or `body`, the gap or
  margin between pages, page centering, or a page drop shadow;
- `@page` or `@media print` rules, which set export pagination and geometry;
- external font links such as `fonts.googleapis.com`. Use platform-listed or
  inlined fonts.

Templates never author callout chrome. This prohibition includes visually
similar custom classes: if markup, CSS, or JavaScript hosts, styles, places, or
connects annotations, it belongs to the runtime.

## Page-bound annotation model

1. **Anchors:** the runtime discovers creative `a[href]`, cover fields, and
   manual points. A creative anchor counts only when its center lies inside the
   iframe viewport; clipped overflow does not produce an annotation. Generated identity remains
   `VIEWPORT|Links to: URL|INDEX`; coordinates are absent so overrides survive
   reflow.
2. **Default placement:** the callout is placed at a fixed inset from the page
   border on the side nearest its anchor and aligned vertically to that anchor.
3. **Bounding:** the runtime clamps the full callout and its arrow endpoint
   inside their source page rect during and after drag. Neither endpoint changes
   its assigned page.
4. **Collision:** callouts on the same side stack vertically in anchor order;
   connectors route to the stacked positions.
5. **Override:** manual drag is the only placement override. Dragging the
   callout moves the box while preserving its anchor. Dragging the arrowhead
   moves only the anchor endpoint while pinning the box. A click (not a drag)
   on the arrowhead selects the connector for marker / stroke width / dash.
   Pins freeze into the operation bake as
   `script#sol-prc-annotation-positions[type="application/json"]` with
   source-page ID plus page-space coordinates. Catalog templates must not
   include that script. Legacy `__prc_annotation_positions` draft extras fold
   into the bake script on the next save.
6. **Rendering:** the runtime paints boxes, connectors, and dots on one overlay
   per page. Overlay DOM is stripped on bake and re-injected on adapt. Templates
   do not host or script that overlay. They may theme it
   only through `--sol-prc-annotation-color`,
   `--sol-prc-annotation-background`, `--sol-prc-annotation-text-color`,
   `--sol-prc-annotation-font`, `--sol-prc-annotation-padding`,
   `--sol-prc-annotation-radius`, `--sol-prc-annotation-border-width`, and
   `--sol-prc-annotation-line-width` on `:root` or `body`; selector overrides
   and any other annotation variables remain reserved.

The model is identical for email, banner, social, and website. There is no
profile-specific annotation stage.

## Per-profile normative rules

The block below is both the human rule list and the source parsed by
`solstice_prc_template_rules`. Keep each rule on one bullet line with a stable
backticked ID. Changing this block changes the MCP payload on its next call;
there is no Python copy of these rules.

<!-- PRC_RULES_START -->
### All profiles

#### MUST
- `common.complete_html`: Produce one complete HTML document with a doctype, html, head, and body.
- `common.declaration`: Declare exactly one `<meta name="sol-prc-contract" content="v2">` whose `data-profile` equals the requested profile and include the adjacent Contract v2 instruction comment.
- `common.profile`: Put exactly one matching `data-sol-prc-proof` value on body and include no cross-profile markers.
- `common.pages`: Wrap pages in one `main[data-sol-prc-pages]`, give every page a stable `data-sol-prc-page` plus `data-sol-prc-page-type`, and ensure every rendered page ID is unique in the composed document.
- `common.creative_slots`: Mark every intended creative iframe with one valid `data-sol-prc-creative` value; only marked iframes are creative slots.
- `common.slot_marker`: Stamp the movable creative box with `data-sol-prc-slot` around the creative iframe; CSS class names are visual only and are not editor discovery.
- `common.config`: Include exactly one parseable JSON object in `script#sol-prc-config[type="application/json"]`.
- `common.fields`: Mark every visible template value exposed to field editing with exactly one normalized `data-sol-prc-field`, `data-sol-prc-mirror`, or `data-sol-prc-derived` role and preserve existing stable IDs.
- `common.field_instances`: Use the same canonical field ID for the same logical value across every rendered page instance so one cover-edit applies to every match.
- `common.field_value_ownership`: Make `data-sol-prc-field` the only value-editable role; keep mirrors and derived values locked while allowing their rendered instances to receive layout and style edits.
- `common.field_page_bound`: Keep every field, mirror, and derived instance clamped inside its assigned page rect during drag, resize, and rail geometry edits.
- `common.field_editing`: Keep every marked field, mirror, derived, inserted, and slot instance selectable, movable, and deletable through the standard engine chrome in cover-edit; a layout gesture pins that instance page-absolute and the result freezes into the next bake.
- `common.inserted_fields`: If inserting Text, Image, or Button during cover-edit, stamp `data-sol-prc-field="inserted_{kind}_{n}"` plus `data-sol-prc-inserted` on that page only; freeze the node in the next bake.
- `common.slot_geometry_in_bake`: If a creative slot is moved or resized, keep it inside its page and write the box onto `[data-sol-prc-slot]` in the next bake, falling back to the iframe when that marker is absent.
- `common.annotation_pages`: Provide unique page boundaries and real anchors; the runtime ignores creative anchors clipped outside the iframe viewport and keeps each callout and arrow endpoint bound to its source page.
- `common.annotation_positions_in_bake`: After a callout drag or arrow-style change, freeze page-space pins in `script#sol-prc-annotation-positions` inside the operation bake; catalog templates must not include that script.
- `common.layer_separation`: Keep reusable proof-template chrome separate from operation creative, values, and bake-resident runtime data.

#### SHOULD
- `common.self_contained`: Keep CSS and portable assets inline and use only platform-listed or inlined fonts.
- `common.compose_check`: Validate both interactive and export composition through the real frontend composer before publishing.
- `common.minimal_shell`: Author only layers L0-L5; let the platform supply values, creative, behavior, sizing, and annotations.
- `common.annotation_theme`: If theming runtime annotations, use only the allowlisted `--sol-prc-annotation-color`, `--sol-prc-annotation-background`, `--sol-prc-annotation-text-color`, `--sol-prc-annotation-font`, `--sol-prc-annotation-padding`, `--sol-prc-annotation-radius`, `--sol-prc-annotation-border-width`, and `--sol-prc-annotation-line-width` variables; theming is optional.

#### MUST NOT
- `common.reserved_namespace`: Author any reserved `--prc-*`, annotation, banner-global, template-runtime, or operation-draft namespace.
- `common.field_overrides`: Author `__prc_field_overrides`, generated field-override CSS, or a catalog-template `script#sol-prc-annotation-positions`.
- `common.field_value_lock`: Make a mirror or derived role independently value-editable or assign a different field ID only because the value renders on another page, clone, or dimension.
- `common.callout_chrome`: Author callout boxes, connector lines or SVG, dots, gutters, stages, overlays, callout CSS, or callout geometry JavaScript.
- `common.catalog_positions`: Author `script#sol-prc-annotation-positions` in a reusable catalog template; only an operation bake may carry it after a drag.
- `common.canvas_chrome`: Author the canvas outside the pages, including an html or body backdrop, the gap or margin between pages, page centering, or a page drop shadow.
- `common.print_rules`: Author `@page` or `@media print` rules; the platform owns export pagination and print geometry.
- `common.external_fonts`: Link external font services; use platform-listed or inlined fonts.
- `common.platform_values`: Seed platform-owned config keys or operation-specific values.
- `common.template_language`: Add Handlebars, Jinja, Mustache, or another host-unrecognized template language.
- `common.unmarked_fallback`: Depend on injection into unmarked iframes.

### Email

#### MUST
- `email.profile`: Use `body[data-sol-prc-proof="email"]` and `data-profile="email"` in the v2 declaration.
- `email.cover`: Provide a cover page with stable `#prc-filename`, `#prc-to`, `#prc-from`, and `#prc-options` hosts.
- `email.options`: Provide `template#prc-option-tpl`; generated rows use `subject_INDEX` and `preheader_INDEX`.
- `email.render_pages`: Provide at least one render page with `data-viewport="desktop|mobile"` and a matching `iframe[data-sol-prc-creative]`.

#### SHOULD
- `email.dual_viewport`: Provide both a 600px desktop slot and a 375px mobile slot unless the approved proof is intentionally single-viewport.
- `email.presentation_seed`: Keep only email presentation keys such as section labels, enumeration, styles, and section lists in the config seed.

#### MUST NOT
- `email.cross_profile`: Include banner adapters or sections, social builder templates, or a non-email creative slot.
- `email.operation_seed`: Seed filename, to, from, subject, preheader, or other operation values in `#sol-prc-config`.

### Banner

#### MUST
- `banner.profile`: Use `body[data-sol-prc-proof="banner"]` and `data-profile="banner"` in the v2 declaration.
- `banner.section`: Author exactly one `[data-banner-section]` under `main[data-sol-prc-pages]`; the platform owns multi-dimension cloning.
- `banner.page`: Give the banner section a stable page marker with `data-sol-prc-page-type="storyboard"`.
- `banner.behavior_seams`: Preserve `#banner-scene-adapter` and `#banner-placeholder-srcdoc` as executable behavior seams.
- `banner.clone_templates`: Provide `#frame-template` and `#isi-region-template` with their required slots and `iframe[data-sol-prc-creative="banner"]`.
- `banner.fields`: Put primary editable banner values on the first section, mirrors on clones, and cumulative duration in `data-sol-prc-derived`, using the same canonical field IDs across all rendered dimensions.

#### SHOULD
- `banner.standard_shape`: Start from the annotation-free v2 form of the banner-standard-srcdoc-shell exemplar and preserve its section, adapter, clone-template, and slot shape.
- `banner.hollow_seed`: Keep the config presentation-only; let the platform inject title, dimensions, scenes, expansion settings, and tenant/auth data.

#### MUST NOT
- `banner.authored_clones`: Author multiple dimension sections or per-operation banner documents in the template.
- `banner.platform_seed`: Seed title, dimensions, scenes, expand_endpoint, expand_model, tenant, or auth values.
- `banner.annotation_engine`: Retain or create the exemplar's compatibility callout overlay, callout CSS, annotation position store, or geometry engine.

### Social

#### MUST
- `social.profile`: Use `body[data-sol-prc-proof="social"]` and `data-profile="social"` in the v2 declaration.
- `social.source_slot`: Provide one source `iframe[data-sol-prc-creative="social"]` that receives the full social creative.
- `social.builders`: Preserve `#prc-platform-page-tpl`, `#prc-variant-cell-tpl`, `#prc-storyboard-page-tpl`, and `#prc-frame-cell-tpl` with their canonical slots.
- `social.pages`: Provide `main[data-sol-prc-pages]`; the social builder may populate its page children at runtime.

#### SHOULD
- `social.minimal_seed`: Keep the config seed to presentation labels such as `sectionTitle`.
- `social.platform_grouping`: Let the builder derive platform and variant grouping from the injected creative.

#### MUST NOT
- `social.operation_seed`: Seed filename, platform groups, variants, or ad copy in `#sol-prc-config`.
- `social.cross_profile`: Include email viewport stages or banner sections and adapters.

### Website

#### MUST
- `website.profile`: Use `body[data-sol-prc-proof="website"]` and `data-profile="website"` in the v2 declaration.
- `website.cover`: Provide stable fields for `file_name`, `url`, `page_title`, and `meta_description`.
- `website.render_pages`: Provide at least one render page with `data-viewport="desktop|mobile"` and a matching `iframe[data-sol-prc-creative]`.

#### SHOULD
- `website.dual_viewport`: Provide both desktop and mobile render pages when the approved proof covers both viewports.
- `website.presentation_seed`: Keep only website presentation hints such as labels, url, pageTitle, and description layout in the config seed.

#### MUST NOT
- `website.cross_profile`: Include banner adapters or sections, social builder templates, or a non-website render shape.
- `website.operation_seed`: Seed operation-specific filename, URL, title, description, or other field values in `#sol-prc-config`.
<!-- PRC_RULES_END -->

## Baked proofs

A baked proof is a frontend compose freeze, not another renderer. New bakes are
stamped `<meta name="sol-prc-contract-baked" content="v2">`; live composition,
non-interactive export, and baked-view adaptation use the same annotation
runtime. Cover-edit freezes field/slot geometry, inserted L5 nodes, inner
creative, and `script#sol-prc-annotation-positions` in that HTML. Overlay chrome
is stripped on bake and re-injected on adapt. Catalog templates must not ship
the positions script. Historical v1 bakes are immutable snapshots. Their legacy
annotation chrome is neutralized during adaptation, and a bake without usable
page markers falls back to recomposition from its pinned template version.

## Validation and migration

The target accept-time prepass rejects non-HTML, missing or duplicate L0/L1,
cross-profile markers, absent marked creative slots, invalid config JSON,
reserved namespace use, and removed core field IDs. Platform-owned seed keys,
legacy v1 annotation hosts, and unavailable fonts warn during migration.

Migration aliases may map v1 seams to v2 during compose:

- `iframe.prc-render-frame`, `data-viewport`, `data-width`, and
  `data-prc-frame` to `data-sol-prc-creative`;
- `#prc-cover-data` and `#banner-template-data` to `#sol-prc-config`;
- `.prc-page`, `main.pages`, and `.prc-pages` to v2 page markers;
- the v1 banner detection triad to the explicit banner profile.

Aliases are compatibility inputs, not authoring guidance. New or edited
templates use the v2 vocabulary, and the composer neutralizes legacy stages,
gutters, connector SVG, and annotation engines.
