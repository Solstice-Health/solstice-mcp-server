# Solstice PRC renderer contract

This is the authoring contract for HTML passed as `templateHtml` to
`buildPrcTemplateHtmlFromStoredTemplate`. The frontend parses the template with
`DOMParser`, injects `creativeHtml`, adds its runtime bridge, and returns a full
doctype HTML document.

The canonical implementations are:

- `Solstice-Frontend/components/content-workspace/prc-template/prc-template-renderer.ts`
- `Solstice-Frontend/components/content-workspace/prc-template/prc-bridge-callouts.ts`
- `Backend-Server/migrations/seeds/prc_templates/`

The bridge generates runtime annotation DOM. Template authors provide only the
structural hosts and CSS.

## Shared document rules

- Produce one complete HTML document per template.
- Keep template HTML and creative HTML separate. Never paste the operation
  creative into the stored template.
- Prefer inline CSS and self-contained assets. This is a portability rule, not a
  renderer selector.
- Do not use Handlebars, Jinja, or Mustache. The host hydrates JSON config
  scripts, data attributes, and iframe `srcdoc`.
- Mark editable fields with `data-sol-prc-field="FIELD_ID"`. The bridge makes
  existing `input` and `textarea` elements read-only/editable and upgrades
  tagged display elements when needed.
- `data-sol-prc-mirror="FIELD_ID"` mirrors an editable field across repeated
  banner dimensions. `data-sol-prc-derived="frame_cumulative_INDEX"` receives the
  running duration sum.
- The page-container lookup order is
  `[data-sol-prc-pages]`, `.prc-pages`, then `.pages`.
- Manual-callout page lookup uses the first non-empty tier:
  1. `[data-sol-prc-page], .prc-page`
  2. `[data-banner-section] .page`
  3. `main.pages > .page, .prc-pages > .page, .pages > .page`
  4. every `.page`

## Creative injection

The renderer selects:

```text
iframe.prc-render-frame, iframe[data-width], iframe[data-prc-frame]
```

If none exist, it injects the creative into every iframe in the document. Avoid
that fallback. Mark only the intended source/render frames.

For every selected frame the renderer:

- removes `src`;
- sets `srcdoc` to the creative;
- sets `scrolling="no"` and `loading="eager"`;
- strips `.email-topper` elements from the injected creative;
- for `desktop` or `mobile`, rewrites viewport metadata and pins the frame to
  `600px` or `375px`.

Viewport detection checks, in order:

1. closest ancestor `[data-viewport]`;
2. iframe `data-width`;
3. iframe `data-sol-prc-creative`.

## Template classification

A template is treated as banner when any one is present:

- `#banner-template-data`;
- `#banner-scene-adapter`;
- document text containing `__BANNER_TEMPLATE_SRCDOC__`.

Banner detection wins over other seams. Do not mix banner markers into email or
social templates.

Email and social use `#prc-cover-data`; distinguish them with:

```html
body class `prc-doc` with `data-sol-prc-proof="email"`
body class `prc-doc` with `data-sol-prc-proof="social"`
```

## Email profile

### Cover config

```html
script `type="application/json" id="prc-cover-data" data-sol-prc-config`
{
  "filename": "",
  "to": "",
  "from": "",
  "toStyle": {},
  "fromStyle": {},
  "sectionTitle": "Subject Line/Preheaders",
  "options": [{ "subject": "", "preview": "" }]
}
end script
```

The renderer updates `filename`, `to`, `from`, and `options` from field values
and draft values while preserving extra seed keys.

Stable cover IDs and fields:

```html
section `.prc-page.prc-cover`
  `[data-page="cover"][data-prc-page="cover"]`
  `[data-sol-prc-page="page_cover"][data-sol-prc-page-type="cover"]`
input `id="prc-filename" data-sol-prc-field="file_name"`
input `id="prc-to" data-sol-prc-field="to_line"`
input `id="prc-from" data-sol-prc-field="from_line"`
heading `id="prc-section-title" data-sol-prc-page-header`
container `id="prc-options"`
```

Option rows use `#prc-option-tpl`, cloned into `#prc-options`:

```html
template `id="prc-option-tpl"`
  container class `prc-option`
    label slot `data-slot="label"`
    subject input `data-slot="subject" data-sol-prc-field=""`
    preview input `data-slot="preview" data-sol-prc-field=""`
```

The host assigns `subject_INDEX` and `preheader_INDEX`, zero-based. Optional
brand-tier targets are `#prc-from-secondary` and `#prc-section-list`.
Seed-only config may also use `fromSecondary`, `enumeration: "letter"`,
`sectionList`, and `pink` tokens.

### Render pages and annotations

Provide one desktop and one mobile page. Change all viewport-specific values
together.

```html
main `.prc-pages#prc-pages[data-sol-prc-pages]`
  section `.prc-page.prc-render-page`
    `data-prc-page="desktop"`
    `data-viewport="desktop"`
    `data-sol-prc-page="page_desktop"`
    `data-sol-prc-page-type="render"`
    `data-sol-prc-page-variant="desktop"`
    paragraph `[data-slot="page-header"][data-sol-prc-page-header]`
    stage `.prc-render-stage[data-stage="desktop"][data-sol-prc-stage]`
      left gutter `.prc-callout-gutter.prc-gutter-left[data-side="left"]`
        `[data-sol-prc-callout-gutter="left"]`
      frame wrapper `.prc-render-frame-wrap`
        iframe `.prc-render-frame[data-width="desktop"]`
          `[data-sol-prc-creative="desktop"][scrolling="no"][loading="eager"]`
        error container `.prc-frame-error`
      right gutter `.prc-callout-gutter.prc-gutter-right[data-side="right"]`
        `[data-sol-prc-callout-gutter="right"]`
      SVG `.prc-connector-svg[data-sol-prc-connector-svg]`
```

The mobile page uses `mobile`, `page_mobile`, and the `375px` slot. Desktop uses
`600px`.

The callout bridge requires, inside each `.prc-render-stage`:

- one `.prc-render-frame`;
- one `.prc-connector-svg`;
- `.prc-callout-gutter[data-side="left"]`;
- `.prc-callout-gutter[data-side="right"]`.

If any are missing, that stage gets no generated annotations.

The bridge enumerates same-origin `a[href]` elements inside the creative. It
ignores missing/empty hrefs, `#`, `javascript:` URLs, and zero-size anchors.
Generated text is `Links to: URL`.

The current stable annotation key is:

```text
VIEWPORT|Links to: TRUNCATED_URL|VERTICAL_INDEX
```

Coordinates are deliberately absent so saved overrides survive reflow. Do not
precompute this key in template HTML. The bridge adds matching
`data-sol-prc-annotation-key` values to its callout, polyline, and drag dots.

The bridge owns `.prc-callout`, `.prc-connector-line`, `.callout-dot`, and
`.sol-prc-manual-callout` elements. Supply CSS for them, not static instances.
A script containing all three strings `layoutStage`, `prc-callout-gutter`, and
`prc-connector-svg` is treated as the legacy annotation engine and removed.

### Persisted annotation positions

Positions are draft data, not template markup. The host passes a JSON string in
`draftValues.__prc_annotation_positions`; invalid or non-object JSON becomes an
empty map. Legacy five-part generated keys are migrated to the current
three-part `VIEWPORT|Links to: URL|INDEX` form. `manual|...` keys pass through.

Generated entries may contain:

- `top`, `left`;
- `coordinateSpace: "offsetParent"` for gutter positions or `"stage"` after a
  callout is dragged over the creative;
- `anchor: {x, y}` with `anchorCoordinateSpace: "stage"` after anchor drag;
- `text`, `textEdited`, and `hidden`.

Manual entries use:

- `manual: true`;
- `coordinateSpace: "page"`;
- `pageId`, resolved from `data-sol-prc-page`, `banner_INDEX`, or the
  `__idx_INDEX` fallback;
- page-relative `anchor: {x, y}` and `callout: {x, y}`;
- optional `text`, `textEdited`, and `hidden`.

Do not seed these values while recreating a template. They belong to a specific
operation/version and the bridge publishes them with the
`sol-prc-annotation-positions` message.

## Banner profile

Use:

```html
body class `banner-proof-doc`
```

### Config and adapters

```html
script `id="banner-template-data" type="application/json"`
{
  "title": "",
  "dimensions": null,
  "frames_per_row": null,
  "scenes": [],
  "show_focus_page": true,
  "expand_endpoint": "/api/content-generation-new/isi-tool/banner-expand-isi",
  "expand_model": "anthropic/claude-opus-4.7"
}
end script

script `id="banner-placeholder-srcdoc" type="text/plain"`
doctype HTML placeholder
end script

script `id="banner-scene-adapter" type="text/plain"`
adapter IIFE
end script
```

The placeholder and adapter bodies are executable behavior contracts. Start
from the current same-content-type canonical seed or supplied banner template;
do not replace them with visual approximations.

The creative should be a complete banner document with one of:

```html
`.banner[data-ad-size="300x250"]`
`.banner[data-dim="300x250"]`
`.banner-root[data-ad-size="300x250"]`
`.banner-root[data-dim="300x250"]`
```

A wrapper is also accepted when the real banner is in `.scene-viewer
iframe[srcdoc]` or `iframe#bannerIframe[srcdoc]`. The raw `#bannerIframe`
attribute is preferred.

Authored dimension resolution checks `data-ad-size`, then `data-dim`, then
known viewport/body inline sizes, then `.banner` CSS. The title's `WxH` is used
only when authored dimensions are absent or when the authored width and height
are both exactly 2x the title dimensions. In that 2x case the nominal title
size wins.

Multi-size creative is a concatenation of complete documents, each beginning
with a doctype HTML declaration. The renderer splits on those boundaries.

### Multi-banner host

The template must have:

```html
main `.pages`
  section `[data-banner-section][data-banner-index="0"]`
    article `.page[data-page="storyboard"]`
      title slot `[data-slot="title"]`
      dimensions slot `[data-slot="dimensions"]`
      frames slot `[data-slot="frames"]`
      ISI slot `[data-slot="isi"]`
```

For multiple creative documents, the renderer clones the first
`[data-banner-section]` under `main.pages`, assigns sequential
`data-banner-index` values, and inserts one
`script[type="application/json"][data-banner-config]` per section. If
`main.pages` or the seed section is absent, it falls back to the first banner
and drops the remaining dimensions.

Required clone sources:

- `template#frame-template` with `[data-slot="frame-label-left"]`,
  `[data-slot="frame-label-right"]`, a `.banner-frame` iframe, and
  `[data-slot="frame-description"]`;
- `template#isi-region-template` with a `.banner-frame` iframe and optional
  focus-spinner slots.

The renderer publishes these globals; the template consumes them:

- `window.__BANNER_TEMPLATE_SRCDOC__`
- `window.__BANNER_TEMPLATE_SRCDOCS__`
- `window.__BANNER_TEMPLATE_SRCDOC_ADCHOICES__`
- `window.__BANNER_TEMPLATE_SRCDOCS_ADCHOICES__`
- `window.__BANNER_TEMPLATE_EXPANDED_SRCDOC__`
- `window.__BANNER_TEMPLATE_EXPANDED_SRCDOCS__`
- `window.__SOL_PRC_GLOBAL_ANNOTATIONS__`

Banner field IDs:

- `file_name`
- `frame_label_INDEX`
- `frame_duration_INDEX`
- `frame_time_label_INDEX`
- `frame_cumulative_label_INDEX`
- `frame_note_label_INDEX`
- `animation_note_INDEX`
- `frame_cumulative_INDEX` is derived, not a persisted input

The first banner section owns `data-sol-prc-field`; cloned dimensions use
`data-sol-prc-mirror` for the same IDs.

## Social profile

Use:

```html
body `.prc-doc[data-sol-prc-proof="social"]`
script `type="application/json" id="prc-cover-data" data-sol-prc-config`
  `{ "filename": "", "sectionTitle": "Social Media Post" }`
end script
main `.prc-pages#prc-pages[data-sol-prc-pages]`
iframe `.prc-render-frame.prc-source-frame`
  `[data-prc-frame="social"][data-sol-prc-creative="social"]`
  `[scrolling="no"][loading="eager"]`
```

The source frame receives the full social creative. Preserve these canonical
template IDs and slots:

- `#prc-platform-page-tpl` with `[data-slot="title"]`,
  `[data-slot="grid"]`, and `[data-slot="isi"]`;
- `#prc-variant-cell-tpl` with `[data-slot="label"]` and
  `.prc-variant-frame`;
- `#prc-storyboard-page-tpl` with `[data-slot="title"]`,
  `[data-slot="frames"]`, and `[data-slot="isi"]`;
- `#prc-frame-cell-tpl` with `[data-slot="label"]` and `.prc-frame-frame`.

The social seed's page-builder script is part of the contract. It splits
concatenated doctype HTML creative documents and discovers variant containers
with `.social-container`, `.carousel-container`, `.medupdate-container`, or
`[data-platform]`. It groups by `data-platform`, defaulting class-only
containers to `OTHER`, clones the templates, and assigns runtime page IDs such
as `data-prc-page="facebook"` and `data-sol-prc-page="page_facebook"`.

## Minimum validation

Static checks:

- profile detector selects only the intended branch;
- all profile IDs, classes, slots, and data attributes above are present;
- JSON config scripts parse;
- intended creative frames are explicitly marked;
- banner multi-size templates contain a `[data-banner-section]` descendant of
  `main.pages`;
- no authored bridge output or removable legacy callout engine remains.

Runtime checks through the actual frontend composer:

- creative hydrates in every viewport/dimension/platform;
- email links generate correctly paired callouts and connector lines;
- cover fields toggle editable/read-only and persist exact field IDs;
- banner mirrors and cumulative values update across dimensions;
- interactive and `interactive: false` export builds both settle;
- no source page is missing, clipped, duplicated, or silently dropped.

