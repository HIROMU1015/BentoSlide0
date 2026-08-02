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
| HTML table | `table` | yes | Emits weighted columns and `rows[].cells[].html` |
| structured chart JSON | `chart` | yes | `bar`, `line`, `pie`, `scatter`; ECharts-shaped pure JSON |
| image + registry asset | `image` | yes | Original path/data is embedded in `doc.assets` and used directly as a data URI for compatibility |
| inline SVG | `svg.markup` | markup-editable | Preserves the localized SVG block |
| video/audio | `media` | yes | Source, playback flags, fit, and radius retained |
| morph metadata | stable `id` / `morphId`, morph transition | yes | Consecutive slides remain separate native slides |
| state/link metadata | `stateOf`, `name`, `link` | yes | Targets are validated |
| presenter notes | `slide.notes` | yes | `[data-speaker-notes]` is not a visible element |
| complex CSS / unknown block | `svg.markup` | limited | Localized fallback; other elements remain native |
| explicit raster fallback | `image` with PNG data URI | limited | Requires explicit `data-bento-id` |

The converter intentionally does not turn a whole slide into one SVG while any smaller semantic/native decomposition is available. `align: justify` is accepted because the checked-in Bento runtime preserves it even though older public examples list only left/center/right.
