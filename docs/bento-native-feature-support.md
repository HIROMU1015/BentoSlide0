# Bento native feature support

## Bento Slides runtime capabilities

The checked-in official runtime accepts `bento/slides` version 1 documents with native `text`, six shape primitives plus paths/connectors, `table`, ECharts-shaped `chart`, `image`, `svg`, and `media` elements. It also preserves slide notes, morph transitions through shared element ids, named state slides and links, document assets/fonts, and the common frame/rotation/opacity fields. These fields were checked by loading them in the base runtime and serializing the document back through `window.bento.serialize()`. The public schema reference is the [Bento agent guide](https://bento.page/agents.md).

## HTML-first converter implementation

The mappings implemented by this repository are:

| HTML/registry source | Bento output | Editable | Notes |
|---|---|---:|---|
| text / inline rich text | `text` | yes | Safe inline tags retained; scripts/styles removed |
| equation + registry LaTeX | `text` with `$$…$$`, `equationId`, `latexSource` | yes | Registry LaTeX overrides preview glyphs |
| rect / rounded / ellipse / triangle / arrow / line | `shape` | yes | Rounded maps to rect + radius |
| path | `shape:path` | yes | `d` and a local path box retained when available |
| connector | `shape:line` + `from` / `to` | yes | Endpoints must exist on the same slide |
| rectangular HTML table | `table` | yes | At most one header row; no spans, nesting, or rich visual cell content |
| complex HTML table | localized `svg.markup` | limited | Cell rectangles/styles preserved for colspan, rowspan, multilevel header, or image/chart/complex content |
| structured chart JSON | `chart` | yes | `bar`, `line`, `pie`, `scatter`; ECharts-shaped pure JSON |
| image + registry asset | `image` | yes | Original path/data is embedded in `doc.assets` and used directly as a data URI for compatibility |
| inline SVG | `svg.markup` | markup-editable | Preserves the localized SVG block and embeds local `href`/`xlink:href` resources |
| video/audio | `media` | yes | Source, playback flags, fit, and radius retained |
| morph metadata | stable `id` / `morphId`, morph transition | yes | Consecutive slides remain separate native slides |
| state/link metadata | `stateOf`, `name`, `link` | yes | Targets are validated |
| presenter notes | `slide.notes` | yes | `[data-speaker-notes]` is not a visible element |
| complex CSS / unknown block | `svg.markup` | limited | Localized fallback; foreignObject and CSS local resources are embedded; other elements remain native |
| explicit raster fallback | `image` with PNG data URI | limited | Requires a slide-local unique `data-bento-id`; the same ID may be shared across slides |
| canvas / WebGL output | captured `image` | limited | Raster is required because no editable DOM representation exists |
| simple slide gradient/image | background-only shape/image | partly | Content remains native; the whole slide is never flattened |

The converter intentionally does not turn a whole slide into one SVG while any smaller semantic/native decomposition is available. `align: justify` is accepted because the checked-in Bento runtime preserves it even though older public examples list only left/center/right.

Generated `.bento.html` files are portable and self-contained: local image/media/SVG/foreignObject/CSS resources resolve relative to the source chapter and become data URIs. A structured post-conversion scan fails when a local resource URL remains. Visual checks treat headings, critical roles, equations, tables, charts, images, SVGs, and `data-bento-critical="true"` elements as critical; a fail-level critical crop fails its slide while a requested non-critical crop can remain a warning.

## Native compatibility classes

| Class | Meaning | Typical inputs |
|---|---|---|
| `native-safe` | Direct native fields preserve the block | plain text/shapes, ordinary table, simple shadow |
| `native-with-adjustment` | Native output plus an explicit geometry/style mapping | padding, letter spacing, flex centering, object-fit, simple linear gradient, 2D translate/scale/rotate |
| `localized-svg-recommended` | Only the smallest affected block becomes SVG | clip-path, mask, filter/backdrop-filter, blend mode, visible pseudo-elements, writing mode, multiple/CSS background layers, skew/3D, complex table |
| `image-required` | DOM semantics cannot reproduce rendered pixels | canvas/WebGL |

`auto` follows this classification. An explicit `native` request still attempts native conversion but falls back locally with a report reason when representation is impossible. Computed CSS evidence includes background properties, clipping/masking/filtering, shadow, pseudo-element dependence, flex/grid values, padding, letter spacing, object fit/position, overflow/text behavior, writing mode, blending, and complete transform metadata.
