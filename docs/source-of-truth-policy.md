# Source-of-truth policy

The authoritative representation changes at the finalization boundary.

1. Before conversion, sorted chapter HTML plus registry JSON is authoritative.
2. Immediately after conversion, `presentation.generated.bento.html` is a reproducible derived artifact.
3. Once final editing starts, `presentation.final.bento.html` and specifically its `#bento-doc` is authoritative for visible content, placement, and style. Its `.bento.json` sidecar is a synchronized editing aid, not an independently writable source.

Never rerun HTML-first conversion into the final path. Rebuild generated independently, then explicitly decide whether to reset final. GPT/Work automation must use the same save API or `WorkEditorStorage` rather than editing the JSON sidecar alone. Generated remains unchanged during final edits.

Default final saves permit geometry and presentation styling while protecting existing content, element/slide identity, equation and figure metadata, links, state/morph/connector references, registry-protected IDs, and required text. `--allow-content-edit` relaxes ordinary content comparison but does not disable Bento schema, references, registry-protected items, resource portability, revision conflicts, or runtime integrity.
