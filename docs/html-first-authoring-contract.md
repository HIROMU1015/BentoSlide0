# HTML-first authoring contract

## Sources of truth

Each chapter is represented by two files:

- `chapter-NN.preview.html`: visual hierarchy, composition, text presentation, and fixed layout.
- `chapter-NN.registry.json`: original LaTeX, paper/figure/table/chart provenance, assets, document metadata, and protected content that must not disappear.

Files are combined in lexical HTML filename order. For `chapter-01.preview.html`, the required registry name is `chapter-01.registry.json`. Registry ids are global across chapters; conflicting duplicate definitions fail the build.

## HTML contract

Every slide must be exactly 1280 × 720 computed CSS pixels and use:

```html
<section class="slide" data-slide-id="method-overview" data-layout="two-column">
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
  "format": "bento/html-registry/v1",
  "chapterId": "chapter-01",
  "document": {
    "title": "Paper title",
    "modified": "2026-08-02T00:00:00Z",
    "theme": {"background":"#fff","color":"#111","accent":"#2563eb","fontFamily":"Arial"}
  },
  "assets": {
    "figure-1": {"path":"assets/figure-1.png","mimeType":"image/png","paperSource":"Fig. 1"}
  },
  "equations": {
    "loss-eq": {"latex":"\\mathcal L=...","paperSource":"Eq. (3)"}
  },
  "figures": {}, "tables": {}, "charts": {},
  "protected": {
    "slideIds": [], "elementIds": [], "requiredText": []
  }
}
```

Unknown registry format versions fail explicitly. `protected` is a deletion guard: conversion never rewrites prose and fails if named slides, elements, or literal required text are missing.

## Authoring prohibitions

Conversion must not summarize or rewrite copy, add claims absent from the paper, change formulas/signs/subscripts/numbers/conditions, change chart type or comparison axes, delete elements, merge or split slides, reverse a flow, exchange primary and supporting content, or replace the deck's semantic color system. Large visual gaps are solved by native re-layout or the smallest possible fallback block, never by inventing content or changing the Bento runtime.
