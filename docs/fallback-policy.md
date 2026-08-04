# Fallback policy

Fallback is decided per element, never per deck, and normally never per full slide.

Priority:

1. Bento native semantic element.
2. Native decomposition into multiple semantic elements when the author supplied those elements separately.
3. Decoration-only SVG.
4. Localized complex-block SVG.
5. Localized PNG image when explicitly requested.
6. Full-slide SVG only as a last-resort future extension; the current converter does not silently choose it.

`data-bento-export` controls the choice:

- `auto`: use the computed `native-safe`, `native-with-adjustment`, `localized-svg-recommended`, or `image-required` class.
- `native`: require a native attempt; a failure is visible in the report and falls back to localized SVG so the build remains inspectable.
- `svg`: preserve the selected block as SVG markup.
- `image`: capture that explicit element in Chromium and embed the PNG.
- `ignore`: omit the element and record the decision.

Each decision includes a human-readable reason in `conversion-report.json`. Native failures therefore cannot disappear behind a successful build. Image/SVG fallbacks keep the original frame and stable element id, but internal pixels or SVG substructure are not fully editable through Bento's native text/shape controls.

Fallback screenshots are selected inside the current slide locator. A fallback-capable element without an explicit ID, no matching node, or multiple same-slide matches is a hard validation error. Cross-slide reuse of the same ID is valid.

Localized SVG/foreignObject output must be self-contained. Resolve relative paths against the source chapter, resolve `asset:` references through the registry, embed local payloads as data URIs, and preserve remote/data/fragment references. Preserve the fragment when an external SVG is embedded (`symbols.svg#id` → `data:image/svg+xml;...#id`). Recursively scan document assets, media posters, chart options, SVG markup, theme/background and nested resource-bearing values; do not scan ordinary prose for path-like examples.

Auto-correction is conservative. Out-of-bounds frames are clamped and minor text overflow can reduce font size; source wording is immutable. Overlap correction is permitted only when layout/role/group/source order proves a safe relationship: two-column side preservation, observation-before-interpretation, equation-above-explanations, row order, stack order, or a shared layout-group axis. The converter reinspects the full slide and rolls back any change that leaves the pair overlapping or creates another overlap. Free/custom and uncertain composition remain diagnostics. A developer should update the HTML source when a diagnostic reflects an authoring defect.

Slide backgrounds are handled independently from slide content. A simple gradient becomes a full-canvas native shape, a supported non-repeating image becomes a full-canvas image, and a complex background becomes a background-only localized SVG. None of these choices flatten the semantic foreground.
