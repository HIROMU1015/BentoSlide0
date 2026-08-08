# HTML-to-Bento conversion specification

## Deterministic stages

```text
single deck HTML + registry JSON (or migrated sorted modular pairs)
→ contract, section-digest, and cross-unit validation
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

HTML layout is measured by Chromium, not inferred from CSS source text. DOM order plus computed `z-index` determines stable element order. Coordinates are rounded to three decimal places. One BrowserHarness owns a single headless Chromium process and creates isolated `sourceLayout` and `bentoCheck` contexts. Reduced motion and animation/transition/caret guards are installed before page scripts. Readiness is event-based: fonts, image decode, then two animation frames; fixed sleeps are prohibited. The document id is UUIDv5 over canonical merged registry and computed layout JSON. `document.modified` comes from the registry; absent values use the deterministic epoch value rather than current time.

Deterministic `sourceLayout` and `bentoCheck` contexts allow only `file:`, `data:`, `blob:`, and `about:`. HTTP(S), including loopback, is blocked. Any source request or unexpected Bento request is a conversion error. The immutable Bento runtime's exact release-manifest probe is also blocked and recorded, but is classified as an expected failed probe rather than an external dependency. A future localhost workflow must use a separate profile with an explicit origin; it must not weaken either conversion profile. Remote resources must first be registered and embedded locally.

## Conversion boundary

The Bento runtime, styles, and scripts outside `script#bento-doc[type="application/bento+json"]` are immutable. The builder compares the base and generated HTML outside that block byte-for-byte. JSON is serialized with finite numbers and every literal `<` escaped as `\u003c`.

The standalone `.bento.json` must equal the embedded document. The final browser check loads that embedded document, verifies all slide and element frames, exercises Bento API edits for available text/shape/equation types, calls `serialize()`, and restores the original before screenshots.

The converter writes regenerable generated HTML/JSON plus `generatedRegistry`. `bento_authoring` initializes a separate authoring HTML/JSON/registry set; all content/structure edits use both base revisions and the common journal transaction. Content approval binds the exact authoring document and registry revisions. Finalization transactionally creates final HTML/JSON/frozen registry plus immutable document and registry baselines from those approved authoring revisions—not from mutable generated output. Completion permits presentation-only differences while rejecting content, identity, data, reference, registry, or slide-structure replacement.

Browser responses temporarily inject controls, but persisted HTML is rebuilt from the current protected runtime plus validated `#bento-doc` only. `window.bento.serialize()` remains a synchronous HTML-string API: temporary UI is detached immediately before serialization and restored in `finally`, including exceptions. OS writer leases, transaction locks, durable journals, revision backups, same-directory fsynced temporary files, recovery, runtime fingerprints, and synchronized sidecars protect saves. A post-commit report failure retains valid artifacts for report retry.

## Layout and correction policy

Computed source frames are normalized from the slide rectangle to the 1280 × 720 Bento canvas. Frames crossing the canvas are clamped and the report records before/after coordinates with `contentChanged: false`. A text box whose computed scroll height exceeds its frame receives a font-size reduction capped at 20%; text content is never shortened, summarized, translated, or reflowed into another element.

Significant pairwise overlaps are interpreted using slide layout, element role, layout group, and source ordering. `two-column`/`two-column-contrast`, `observation-interpretation`, `equation-dissection`, `row`, `stack`, and a shared non-free layout group each have a relationship-preserving policy. A proposed correction is applied transactionally, reinspected, and rolled back if the target overlap remains or a new overlap appears. `free`/`custom` and ambiguous relationships produce diagnostics only. Every correction/diagnostic records `policy`, `reason`, and reinspection outcome.

## Transform geometry

Extraction records `offsetWidth/Height`, `clientWidth/Height`, the complete DOM matrix, translation, scale, rotation, transform origin, visual center, the pre-rotation scaled frame, and the axis-aligned bounding frame. For representable 2D translate/scale/rotate transforms, Bento `x/y/w/h` describes the pre-rotation scaled frame around the measured visual center and `rotation` stores the matrix rotation. This preserves non-default transform origins and combined translate/rotate/scale. Skew and 3D matrices use a localized SVG with a captured block because Bento has no equivalent transform field. The fixture covers rotate, rotated text, non-default origin, rotate+scale, translate+rotate, and skew.

## Table structure

Each HTML cell records `rowSpan`, `colSpan`, row/column index, relative rectangle, content, and computed cell style. A native table is allowed only for a rectangular grid with one header row at most, no spans, no nested table, and no image/chart/complex content. Colspan, rowspan, multilevel headers, and image-bearing cells therefore become localized SVG blocks; the ordinary table fixture stays native.

## References and source-unit merge

The pipeline rejects duplicate slide ids, duplicate per-slide element ids, conflicting registry ids, unsupported layouts/export modes, missing protected items, missing equation/asset sources, broken `stateOf` targets, and connector endpoints that do not exist on the same slide. Single-file slides also require a stable `data-section-id`; conversion revalidates approval digests over DOM, referenced registry projection/assets, and global CSS/theme. Morph relationships are carried through stable IDs/`morphId`; visual pairing is inspected in the output report.

## Portable resources

Native media preserves both `src` and an optional `poster`; each passes through the common resolver. Local resources in chart options, document assets, SVG/foreignObject markup, media, images, theme/background structures, and nested resource-bearing keys are recursively embedded or rejected. `symbols.svg#symbol-a` becomes a data URI ending in `#symbol-a`, while an internal `#symbol-a` stays unchanged. The v2 resource scan traverses dicts/lists across the complete final document, records category counts, accepts data URIs (including fragments), and avoids interpreting ordinary prose as a path.

## Visual comparison

Source and Bento slides are captured in the same Chromium engine. Every pair is normalized to 256×144 RGB and measured with 64-bit pHash distance, normalized mean absolute pixel difference, an 8×8×8 RGB distribution distance, edge-map difference, and a global SSIM-like score. Title, `main-claim`, `primary-visual`, `conclusion`, equation, table, chart, image, and SVG blocks also receive bounding-box crop comparison. Authors may add `data-bento-critical="true"` or request a non-critical crop with `data-bento-compare="true"`. Crops expand by 8 pixels and to at least 24×24 pixels before both images are normalized to the same analysis size.

Whole-slide warning thresholds are pHash ≥ 10, pixel difference ≥ 0.075, color-distribution difference ≥ 0.10, or edge difference ≥ 0.065. A pair fails at pixel ≥ 0.30, color ≥ 0.35, edge ≥ 0.25, or at combined pixel ≥ 0.16 with edge ≥ 0.12 or pHash ≥ 20. These thresholds are calibrated against `tests/fixtures/html_first`: the 23-slide fixture produced no visual failures, maximum pixel difference 0.087774, and average 0.006934, while synthetic tests with a missing background, missing primary block, or large displacement fail. Native chart/SVG/font rasterization can be warning-level without hiding the metric.

Critical crop failures escalate the owning slide to `fail`, even when the whole-slide image passes. Non-critical crop failures contribute a slide warning. Crop classification uses relaxed localized fail thresholds (pixel 0.45, color 0.75, edge 0.35, or combined pixel 0.30 with edge 0.16/pHash 32) to avoid treating renderer detail as missing content.

`conversion-report.json` records every element's source/result type, compatibility class/reasons, strategy/reason, role, critical flag/reason, status contribution, logical layout group, source/Bento frames and bounding frames, corrections, diagnostics, registry coverage, embedded asset resolutions, resource scan, runtime integrity, browser check, whole-slide metrics, prioritized crop metrics, incremental-cache hit/miss counts, and aggregate visual/critical pass-warning-fail values. `diagnostics/resource-scan.json` is a standalone CI artifact.

`diagnostics/browser-environment.json` uses `bento/browser-environment/v1` and records Playwright/Chromium versions, privacy-safe OS identity, the separate source/Bento viewport profiles, DPR, locale/timezone, render/network policy, declared/computed fonts, Chromium-observed platform font families, embedded font asset hashes, and canonical environment/font digests. It never records hostname, username, browser executable paths, or font file paths. It is observational evidence: it is uploaded by CI and may be used in incremental cache keys, but is never embedded in `#bento-doc`, the Bento JSON sidecar, registry, runtime fingerprint, document revision, content approval, or final approval.

Interactive builds may opt into `--incremental`. Each slide cache key covers canonical slide DOM, relevant registry projection, referenced asset hashes, global DOM/CSS/theme, converter cache format, Bento runtime fingerprint, and the profile environment digest. A cache hit may reuse computed layout, fallback captures, source/Bento screenshots, and visual comparison evidence. Cache writes are atomic; each screenshot record also stores and verifies the exact PNG SHA-256 before reuse, so a crash or racing write between PNG and JSON replacement becomes a cache miss. Global CSS/theme or runtime/environment changes invalidate the affected cache keys. Normal builds and every workflow conversion/approval gate ignore reuse and run a full build/full validation; `output/.bento-cache/` is never authoritative.

## Determinism evidence

`scripts.check_html_first_determinism` accepts either `--html/--registry` or the migrated modular `--html-dir/--registry-dir` pair and builds into two independent temporary directories. Raw Bento HTML and JSON must be byte-identical. Conversion reports and computed layouts are normalized only for browser labels and build-root paths, canonically serialized, and compared. The report includes SHA-256 for both copies of all four outputs. The legacy JSON-first byte comparison remains a separate CI gate.
