# HTML-to-Bento conversion specification

## Deterministic stages

```text
sorted chapter HTML + registry JSON
→ contract and cross-chapter validation
→ Chromium load/font completion
→ getBoundingClientRect + getComputedStyle
→ normalize frames to 1280×720
→ native mapping or localized fallback
→ bounds/text-overflow correction without copy changes
→ Bento document validation
→ inject only #bento-doc into the selected base
→ Bento UI startup + API edit + serialize round-trip
→ paired source/Bento screenshots and report
```

HTML layout is measured by Chromium, not inferred from CSS source text. DOM order plus computed `z-index` determines stable element order. Coordinates are rounded to three decimal places. The document id is UUIDv5 over canonical merged registry and computed layout JSON. `document.modified` comes from the registry; absent values use the deterministic epoch value rather than current time.

## Conversion boundary

The Bento runtime, styles, and scripts outside `script#bento-doc[type="application/bento+json"]` are immutable. The builder compares the base and generated HTML outside that block byte-for-byte. JSON is serialized with finite numbers and every literal `<` escaped as `\u003c`.

The standalone `.bento.json` must equal the embedded document. The final browser check loads that embedded document, verifies all slide and element frames, exercises Bento API edits for available text/shape/equation types, calls `serialize()`, and restores the original before screenshots.

## Layout and correction policy

Computed source frames are normalized from the slide rectangle to the 1280 × 720 Bento canvas. Frames crossing the canvas are clamped and the report records before/after coordinates with `contentChanged: false`. A text box whose computed scroll height exceeds its frame receives a font-size reduction capped at 20%; text content is never shortened, summarized, translated, or reflowed into another element.

Significant pairwise overlaps are diagnosed. They are not automatically moved because overlap can be intentional and arbitrary relocation would change composition. Source clipping and complex visual effects select a localized SVG fallback when requested or when native conversion is unsafe.

## References and chapter merge

The pipeline rejects duplicate slide ids, duplicate per-slide element ids, conflicting registry ids, unsupported layouts/export modes, missing protected items, missing equation/asset sources, broken `stateOf` targets, and connector endpoints that do not exist on the same slide. Morph relationships are carried through stable IDs/`morphId`; visual pairing is inspected in the output report.

## Visual comparison

Source and Bento slides are captured in the same Chromium engine. The comparison is semantic rather than pixel-exact: slide order, emitted/ignored element inventory, normalized coordinate basis, runtime render success, and one-to-one screenshot pairs must agree. Font shaping, native chart/table rendering, and deliberate fallback mean exact pixels are not a valid correctness requirement.

`conversion-report.json` records every element's source type, result type, strategy, reason, logical layout group, corrections, diagnostics, registry coverage, runtime integrity, browser check, and screenshot pairing.
