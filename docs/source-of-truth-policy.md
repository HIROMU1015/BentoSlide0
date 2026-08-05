# Source-of-truth policy

The authoritative representation changes by workflow stage. `deck.yaml` is always the sole machine-readable source for stage, approvals, chapter status, selected source, output paths, ports, and the current localhost URL. Markdown planning files are human-readable artifacts; they must not become a second state store.

1. During intake and planning, the source selected in `deck.yaml` plus the approved files under `planning/` are authoritative for the requested explanation.
2. During HTML authoring and review, sorted `chapters/chapter-NN.preview.html` plus matching registry JSON is authoritative. A chapter may advance only after its visual review is recorded in `deck.yaml`.
3. Immediately after conversion, `output/presentation.generated.bento.html` is a reproducible derived artifact.
4. Once final editing starts, `output/presentation.final.bento.html` and specifically its `#bento-doc` is authoritative for visible content, placement, and style. Its `.bento.json` sidecar is a synchronized editing aid, not an independently writable source.

Never rerun HTML-first conversion into the final path. Rebuild generated independently, then explicitly decide whether to reset final. GPT/Work automation must use the same save API or `WorkEditorStorage` rather than editing the JSON sidecar alone. Generated remains unchanged during final edits.

Default final saves permit geometry and presentation styling while protecting existing content, element/slide identity, equation and figure metadata, links, state/morph/connector references, registry-protected IDs, and required text. `--allow-content-edit` relaxes ordinary content comparison but does not disable Bento schema, references, registry-protected items, resource portability, revision conflicts, or runtime integrity.

Stage changes must go through `python -m scripts.deck_workflow ...`; direct YAML rewrites are unsupported. Each transition validates its inputs and atomically replaces `deck.yaml`, so a failed check leaves the previous state intact.
Generated/final HTML and JSON paths must all be distinct. A `blocked` state keeps its owner and reason in `deck.yaml`, not only in the human-readable work log.
