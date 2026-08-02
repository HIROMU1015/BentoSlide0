# HTML-to-Bento conversion specification

## Deterministic stages

```text
sorted chapter HTML + registry JSON
→ contract and cross-chapter validation
→ Chromium load/font completion
→ geometry/DOMMatrix/table-structure + getComputedStyle extraction
→ normalize frames to 1280×720
→ native compatibility classification
→ native mapping, adjustment, localized SVG, or required image fallback
→ bounds/text-overflow/layout-aware overlap correction without copy changes
→ Bento document validation
→ inject only #bento-doc into the selected base
→ Bento UI startup + API edit + serialize round-trip
→ paired source/Bento screenshots and report
```

HTML layout is measured by Chromium, not inferred from CSS source text. DOM order plus computed `z-index` determines stable element order. Coordinates are rounded to three decimal places. Animations, transitions, and carets are disabled before measurement. The document id is UUIDv5 over canonical merged registry and computed layout JSON. `document.modified` comes from the registry; absent values use the deterministic epoch value rather than current time.

## Conversion boundary

The Bento runtime, styles, and scripts outside `script#bento-doc[type="application/bento+json"]` are immutable. The builder compares the base and generated HTML outside that block byte-for-byte. JSON is serialized with finite numbers and every literal `<` escaped as `\u003c`.

The standalone `.bento.json` must equal the embedded document. The final browser check loads that embedded document, verifies all slide and element frames, exercises Bento API edits for available text/shape/equation types, calls `serialize()`, and restores the original before screenshots.

## Layout and correction policy

Computed source frames are normalized from the slide rectangle to the 1280 × 720 Bento canvas. Frames crossing the canvas are clamped and the report records before/after coordinates with `contentChanged: false`. A text box whose computed scroll height exceeds its frame receives a font-size reduction capped at 20%; text content is never shortened, summarized, translated, or reflowed into another element.

Significant pairwise overlaps are interpreted using slide layout, element role, layout group, and source ordering. `two-column`/`two-column-contrast`, `observation-interpretation`, `equation-dissection`, `row`, `stack`, and a shared non-free layout group each have a relationship-preserving policy. A proposed correction is applied transactionally, reinspected, and rolled back if the target overlap remains or a new overlap appears. `free`/`custom` and ambiguous relationships produce diagnostics only. Every correction/diagnostic records `policy`, `reason`, and reinspection outcome.

## Transform geometry

Extraction records `offsetWidth/Height`, `clientWidth/Height`, the complete DOM matrix, translation, scale, rotation, transform origin, visual center, the pre-rotation scaled frame, and the axis-aligned bounding frame. For representable 2D translate/scale/rotate transforms, Bento `x/y/w/h` describes the pre-rotation scaled frame around the measured visual center and `rotation` stores the matrix rotation. This preserves non-default transform origins and combined translate/rotate/scale. Skew and 3D matrices use a localized SVG with a captured block because Bento has no equivalent transform field. The fixture covers rotate, rotated text, non-default origin, rotate+scale, translate+rotate, and skew.

## Table structure

Each HTML cell records `rowSpan`, `colSpan`, row/column index, relative rectangle, content, and computed cell style. A native table is allowed only for a rectangular grid with one header row at most, no spans, no nested table, and no image/chart/complex content. Colspan, rowspan, multilevel headers, and image-bearing cells therefore become localized SVG blocks; the ordinary table fixture stays native.

## References and chapter merge

The pipeline rejects duplicate slide ids, duplicate per-slide element ids, conflicting registry ids, unsupported layouts/export modes, missing protected items, missing equation/asset sources, broken `stateOf` targets, and connector endpoints that do not exist on the same slide. Morph relationships are carried through stable IDs/`morphId`; visual pairing is inspected in the output report.

## Visual comparison

Source and Bento slides are captured in the same Chromium engine. Every pair is normalized to 256×144 RGB and measured with 64-bit pHash distance, normalized mean absolute pixel difference, an 8×8×8 RGB distribution distance, edge-map difference, and a global SSIM-like score. Title, `main-claim`, `primary-visual`, equation, table, chart, image, and SVG blocks also receive bounding-box crop comparison.

Warning thresholds are pHash ≥ 10, pixel difference ≥ 0.075, color-distribution difference ≥ 0.10, or edge difference ≥ 0.065. A pair fails at pixel ≥ 0.30, color ≥ 0.35, edge ≥ 0.25, or at combined pixel ≥ 0.16 with edge ≥ 0.12 or pHash ≥ 20. These thresholds are calibrated against `tests/fixtures/html_first`: the 22-slide fixture produced no visual failures, maximum pixel difference 0.087774, and average 0.007179, while synthetic tests with a missing background, missing primary block, or large displacement fail. Native chart/SVG/font rasterization can be warning-level without hiding the metric.

`conversion-report.json` records every element's source/result type, compatibility class/reasons, strategy/reason, role, logical layout group, source/Bento frames and bounding frames, corrections, diagnostics, registry coverage, runtime integrity, browser check, whole-slide metrics, prioritized crop metrics, and aggregate visual pass/warning/fail/max/average values.

## Determinism evidence

`scripts.check_html_first_determinism` builds into two independent temporary directories. Raw Bento HTML and JSON must be byte-identical. Conversion reports and computed layouts are normalized only for browser labels and build-root paths, canonically serialized, and compared. The report includes SHA-256 for both copies of all four outputs. The legacy JSON-first byte comparison remains a separate CI gate.
