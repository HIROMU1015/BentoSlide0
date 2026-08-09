# Visual proposal, origin, and asset workflow

GPT Work evaluates every planned slide for whether a visual materially improves understanding. It may propose and create a concept, structure, relationship, flow, architecture, hierarchy, before/after, timeline, or state-change visual without waiting for the user to ask for a diagram. Users describe the communication goal; they do not edit visual YAML, crop files, write `<img>` tags, or update the registry.

## Decision order

1. If prose is already the clearest representation, use no diagram.
2. If boxes, text, arrows, connectors, simple shapes, flow, hierarchy, comparison, architecture, or states are sufficient, author a Bento-native HTML diagram.
3. If the original figure itself is evidence or the author's unique presentation matters, extract and use the source figure.
4. Only when visual richness or an abstract metaphor is the value and native elements are insufficient, use a generated local image.

Native diagrams are preferred because their text, geometry, links, states, and morph relationships remain editable and resolution-independent. A source figure is not recreated when the original result or design matters. A generated image is explanatory art, never source evidence.

The repository does not implement a particular image-generation provider. GPT Work may use its available image capability and then registers the returned local image through the transactional asset interface.

## Non-fabrication boundary

Generated visuals must never supply numerical data, experimental results, measurements, benchmark results, quantitative plots, or equations. Quantitative visuals must be built from registered source data. Equations remain registry-backed LaTeX and Bento equation/text elements; they are never drawn by an image generator. A generated asset with an evidence source or a prohibited result role is rejected.

## Planning contract

New plans may include `planning/visual-plan.yaml`. The file is internal machine metadata and is validated automatically when present:

```yaml
schemaVersion: 1
slides:
  - id: method-overview
    purpose: explain algorithm structure
    visual:
      recommended: true
      type: native-diagram
      intent: show the relationship between deterministic and randomized parts
      originKind: source-derived
```

`type` is `none`, `native-diagram`, `generated-image`, or `source-figure`. A recommended visual requires a non-empty intent. Source figures use `source-original`; generated images use `generated`. Work communicates this naturally, for example: “This sequence is easier to follow as a flow diagram, so I will add one.”

Validate the optional plan with:

```powershell
python -m scripts.validate_visual_plan planning/visual-plan.yaml
```

## Origin contract

Registry v2 assets and figures may carry one of three origins:

- `source-original`: the original source visual. It requires one registered `sourceId` and a non-empty `locator`.
- `source-derived`: an explanatory reconstruction. It requires one or more `{sourceId, locator}` entries in `sources` and must not be described as original.
- `generated`: explanatory visual not present in the source. It must not carry `sourceId`, `sources`, `locator`, or source provenance.

For backward compatibility, registry definitions without `origin` remain valid. New visual work records it. Every origin-bearing asset also records `contentDigest` as `sha256:` plus the SHA-256 of its decoded bytes. When a figure links to an asset with `assetId`, both origin objects must match. `source-original` also mirrors the same single reference into the existing `provenance` field so older content-review logic and tooling keep their meaning.

```json
{
  "assets": {
    "paper-fig-3": {
      "path": "assets/source/paper-fig-3.png",
      "contentDigest": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "role": "source-figure",
      "origin": {"kind":"source-original","sourceId":"paper","locator":"Fig. 3, p. 7"},
      "provenance": {"sourceId":"paper","locator":"Fig. 3, p. 7"}
    },
    "method-concept": {
      "path": "assets/generated/method-concept.png",
      "contentDigest": "sha256:fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210",
      "role": "conceptual-illustration",
      "origin": {"kind":"generated"}
    }
  }
}
```

## Asset and PDF figure flow

The visible `images/` directory is the local image library:

```text
images/user/        images supplied by the user
images/extracted/   source figures extracted by Work/GPT
images/generated/   generated explanatory images
```

Library files are local intake/history, not registry authority. Registration copies the selected bytes into the hidden `deck/assets/` tree and binds them to the registry digest. PDF extraction preserves its crop in `images/extracted/` and registers the same bytes transactionally. Image-generation output must first be saved under `images/generated/`; user-provided images remain under `images/user/`.

The internal registration command copies an image, computes its `contentDigest`, and updates asset and figure definitions in the same crash-recoverable transaction. Destinations are selected by origin:

```text
deck/assets/source/      source-original
deck/assets/local/       source-derived raster/SVG
deck/assets/generated/   generated
```

Examples for agent/tooling use:

```powershell
python -m scripts.register_visual_asset --root . --registry deck/deck.registry.json register `
  --input images/user/paper-fig-3.png --asset-id paper-fig-3 --kind source-original `
  --role source-figure --source-ref "paper::Fig. 3, p. 7" --caption "Method overview"

python -m scripts.register_visual_asset --root . --registry deck/deck.registry.json register `
  --input images/generated/method-concept.png --asset-id method-concept --kind generated `
  --role conceptual-illustration --description "Intuition, not evidence" --generator "GPT image capability"

python -m scripts.register_visual_asset --root . --registry deck/deck.registry.json extract-pdf `
  --source-id paper --page 7 --crop 72 144 540 510 --asset-id paper-fig-3 `
  --locator "Fig. 3, p. 7" --figure-number "Fig. 3" --caption "Method overview"
```

PDF extraction records the one-based page, figure number, caption, crop rectangle in PDF points, render DPI, and persistent `images/extracted/` library path. It resolves the PDF from the registered repository-relative source path and needs no manual user crop. `--replace` is explicit; ID/path collisions otherwise fail. The supported `pymupdf` module is used only for PDF rendering.

## HTML, Bento, and rolling sections

Native diagrams are authored as stable-ID text/shape/connector elements in HTML and pass through Chromium computed layout to editable Bento elements. A source-derived native diagram uses one assetless registry figure with `origin.kind: source-derived`; every text, shape, and connector that belongs to it carries the same `data-figure-id`. This keeps the diagram editable while making its source closure explicit.

```html
<div data-bento-id="step-a" data-bento-type="shape" data-figure-id="method-flow"></div>
<div data-bento-id="arrow" data-bento-type="shape" data-bento-shape="connector" data-figure-id="method-flow"></div>
<div data-bento-id="label" data-bento-type="text" data-figure-id="method-flow">Method</div>
```

```json
{"figures":{"method-flow":{"role":"derived-diagram","origin":{"kind":"source-derived","sources":[{"sourceId":"paper","locator":"Sec. 3 method flow"}]}}}}
```

Images reference both `data-asset-id` and `data-figure-id`; their local bytes become data URIs, while those stable IDs remain on the Bento image element. Before conversion, the registry digest is checked against the local/data asset. At content review, the embedded image bytes are checked again against the immutable digest. An ordinary Work editor save cannot add, relabel, or change the identity fields of `source-original` assets/figures; use the registered HTML/segment asset workflow for that change. The merged registry retains complete origin metadata. Remote resources remain prohibited and generated Bento stays self-contained.

Visual changes follow the normal rolling lifecycle: section HTML authoring, visual review, promotion, Bento authoring, and acceptance. Position or size changes of a native/image element can be made in Bento. A substantial redesign uses a temporary HTML candidate and targeted section replacement. The section digest includes the referenced visual definitions, transitive figure-to-asset dependencies, source/origin metadata, and asset bytes. Changing a referenced locator or generated description invalidates that section; adding an unrelated visual does not.
