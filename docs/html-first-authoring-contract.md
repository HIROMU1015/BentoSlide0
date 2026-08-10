# HTML-first authoring contract

## Sources of truth

New schema v2 decks use one pair:

- `deck/deck.preview.html`: all fixed-size slides, grouped by stable `data-section-id`.
- `deck/deck.registry.json`: original LaTeX, source/provenance, assets, document metadata, and protected content.

Migrated `authoring.mode: modular` decks retain lexically sorted `chapters/chapter-NN.preview.html` / `.registry.json` pairs. Registry IDs are global; conflicting definitions fail the build. HTML and its registry are inseparable evidence in either mode.

Every single-file slide must include `data-section-id`. Section approval covers canonical slide DOM, referenced registry definitions/source provenance, referenced local asset content, and global CSS/theme. A changed local dependency invalidates the affected section; changed global CSS/theme invalidates all sections. Conversion requires current approval digests.

New single/imported decks author and review the complete HTML file as one user-facing checkpoint. Sections remain deterministic digest and impact scopes. After the first review, corrections must use a temporary candidate and the revision-bound impact/confirmation flow in `html-change-review.md`; reviewed canonical HTML must not be overwritten directly.

## HTML contract

Every slide must be exactly 1280 × 720 computed CSS pixels and use:

```html
<section class="slide" data-slide-id="method-overview" data-section-id="method" data-layout="two-column">
  <h1 data-bento-id="method-title">Method</h1>
  <div data-bento-id="loss" data-bento-type="equation" data-equation-id="loss-eq">
    L = data + regularizer
  </div>
</section>
```

`data-slide-id` is globally unique. `data-bento-id` is stable and unique within its slide. Elements without `data-bento-id` are auto-discovered only for common semantic tags (`h1`–`h6`, `p`, `li`, `table`, `img`, `svg`, `video`, `audio`, and `canvas`); explicit IDs are recommended for reproducibility and references.

Supported element annotations:

| Attribute | Meaning |
|---|---|
| `data-bento-type` | `text`, `equation`, `shape`, `table`, `chart`, `image`, `svg`, `media`, or a custom complex block |
| `data-bento-export` | `native`, `svg`, `image`, `auto`, or `ignore`; default is `auto` |
| `data-bento-critical` | `true` explicitly makes a localized crop failure fail the slide/build |
| `data-bento-compare` | `true` requests a crop metric without making the element critical |
| `data-bento-z` | Explicit stable z-order override; otherwise computed `z-index` and DOM order are used |
| `data-paper-source` | Per-element paper provenance copied into the conversion report |
| `data-equation-id` | Registry equation whose original LaTeX must be used |
| `data-asset-id` | Registry asset to embed and use as the editable image source |
| `data-chart-id`, `data-table-id`, `data-figure-id` | Provenance link into the registry |
| `data-bento-shape` | `rect`, `rounded`, `ellipse`, `triangle`, `arrow`, `line`, `path`, or `connector` |
| `data-from`, `data-to` | Connector endpoint in `element-id:side` form |
| `data-morph-id` | Stable relationship metadata carried to Bento `morphId` |
| `data-link` | Target slide/state id (`data-bento-link` is accepted as a compatibility alias) |
| `data-layout`, `data-layout-id` | Logical group type and stable group name |

Supported `data-layout` values are `figure-reading-guide`, `equation-dissection`, `observation-interpretation`, `claim-evidence-boundary`, `before-gap-paper-view`, `evaluation-protocol`, `input-process-output`, `two-column-contrast`, `matrix-positioning-map`, and `custom`. The utility layouts `free`, `stack`, `row`, `grid`, and `two-column` are also accepted. They describe relationships for reports and validation; Chromium remains authoritative for final element frames.

Use `data-transition="morph"`, `data-state-of="slide-id"`, and `data-slide-name="…"` on slides. Presenter notes belong in `[data-speaker-notes]` and are copied to `slide.notes`, not rendered as an element.

Simple flex centering, padding, letter spacing, object-fit, gradients, shadows, and 2D translate/scale/rotation can remain native with reported adjustments. Skew/3D, clip-path, masks, filters, backdrop-filter, blend modes, visible pseudo-element-dependent visuals, non-horizontal writing, and complex/multiple background layers should be expected to use a localized fallback. Complex slide backgrounds are isolated behind native foreground elements.

Any fallback-capable element must have an explicit `data-bento-id`. Capture lookup is scoped to its containing slide, so the same stable ID may be reused on different slides for morph/state continuity. Duplicate matches within one slide fail with the slide ID, element ID, capture reason, and matched count.

Local references in image/media sources, SVG `href`/`xlink:href`, foreignObject HTML, inline styles, style blocks, and CSS `url(...)` are resolved relative to the chapter HTML and embedded as data URIs. `asset:id` resolves through the registry. `data:`, `http:`, `https:`, and `#fragment` references remain unchanged. Missing local files fail with redacted source-relative context; generated Bento JSON must contain no unresolved local resource URL in a structured resource field.

A native HTML table must be a rectangular grid with no rowspan/colspan, no nested table, no image/chart/complex cell, and at most one header row. Other table structures are supported through localized SVG and retain per-cell content, indices, spans, rectangles, and computed styles.

Charts require pure JSON in a descendant script:

```html
<div data-bento-id="chart" data-bento-type="chart">
  <script type="application/json" data-chart-option>
    {"_bentoPreset":"bar","xAxis":{"data":["A","B"]},"series":[{"type":"bar","data":[2,5]}]}
  </script>
</div>
```

`data-bento-chart` is accepted as an alias for `data-chart-option`.

## Registry contract

```json
{
  "format": "bento/html-registry/v2",
  "unitId": "deck",
  "sources": {
    "paper": {"path":"sources/private/paper.pdf","type":"application/pdf","role":"primary"}
  },
  "document": {
    "title": "Paper title",
    "modified": "2026-08-02T00:00:00Z",
    "theme": {"background":"#fff","color":"#111","accent":"#2563eb","fontFamily":"Arial"}
  },
  "assets": {
    "figure-1": {"path":"assets/source/figure-1.png","mimeType":"image/png","contentDigest":"sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","origin":{"kind":"source-original","sourceId":"paper","locator":"Fig. 1"},"provenance":{"sourceId":"paper","locator":"Fig. 1"}}
  },
  "equations": {
    "loss-eq": {"latex":"\\mathcal L=...","provenance":{"sourceId":"paper","locator":"Eq. (3)"}}
  },
  "figures": {}, "tables": {}, "charts": {},
  "protected": {
    "slideIds": [], "elementIds": [], "requiredText": []
  }
}
```

Unknown registry formats fail explicitly. v1 remains accepted only for migrated/legacy sources and is normalized into the v2 lifecycle form. `protected` is a deletion guard: conversion never rewrites prose and fails if named slides, elements, or literal required text are missing. `sources/source-manifest.yaml` and registry `sources` use repository-relative paths; provenance references stable source IDs.

New asset/figure definitions use `origin.kind`: `source-original` requires one `sourceId` and locator; `source-derived` requires a non-empty `sources` list of sourceId/locator pairs; `generated` must not claim source provenance. Every origin-bearing asset requires a SHA-256 `contentDigest`; conversion checks it against the local/data bytes, and content review checks the embedded image bytes again. Images use `data-asset-id` and `data-figure-id`; conversion embeds local bytes but preserves both stable IDs and the merged registry metadata. A source-derived native diagram instead uses an assetless figure definition and places its `data-figure-id` on every participating text/shape/connector. See `docs/visual-workflow.md` for planning, PDF extraction, generated assets, and the prohibition on generated data/results/plots/equations.

Generated, authoring, and final registry files are separate lifecycle snapshots. HTML authoring never rewrites generated/authoring/final registries; Bento authoring changes only authoring registry in the same transaction as its document; finalization freezes a copied final registry and baseline.

## Authoring prohibitions

Conversion must not summarize or rewrite copy, add claims absent from the paper, change formulas/signs/subscripts/numbers/conditions, change chart type or comparison axes, delete elements, merge or split slides, reverse a flow, exchange primary and supporting content, or replace the deck's semantic color system. Large visual gaps are solved by native re-layout or the smallest possible fallback block, never by inventing content or changing the Bento runtime.
