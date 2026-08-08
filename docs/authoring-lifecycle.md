# Bento authoring lifecycle

## Artifact sets

| Phase | Document | Registry | Mutability |
| --- | --- | --- | --- |
| HTML authoring | `deck/deck.preview.html` | `deck/deck.registry.json` | visual design, content, provenance |
| Generated | `presentation.generated.bento.html/.json` | `generatedRegistry` | reproducible, read-only |
| Bento authoring | `presentation.authoring.bento.html/.json` | `authoringRegistry` | content and structure |
| Finalization | `presentation.final.bento.html/.json` | `finalRegistry` | presentation only; registry frozen |

HTML and generated registries remain historical evidence. Authoring saves never rewrite either. Finalization begins only by transactionally snapshotting the approved authoring artifacts.

## Rolling section handoff

The primary new-deck route repeats `HTML authoring -> HTML review -> promotion -> Bento authoring -> accepted` for each planned section. Promotion builds a section-only HTML/registry candidate, inserts it at planning order, or replaces the section's prior contiguous Bento range with an N-to-M range. `slideIds` follows the current canonical source; `bentoSlideIds` preserves the installed range while an accepted section is redesigned through HTML. The replacement may change every section-local slide/element ID, but it rejects collisions and dangling external references, removes obsolete protected membership, merges the candidate registry, and commits authoring HTML/JSON/registry with workflow state. Unrelated slide hashes and generated/final artifacts must remain unchanged. HTML approval and Bento acceptance are distinct human decisions; neither is inferred by `advance`.

Section acceptance binds a digest of the installed slide projection and the exact referenced equation, chart, table, figure, asset, protected metadata, source, and provenance closure. Unreferenced registry changes do not invalidate unrelated sections. Referenced definition or provenance changes do. Every route into content review, content approval, or final initialization requires all rolling sections to be accepted and their digests to match one consistent authoring document/registry snapshot.

## Authoring save contract

The authoring API request carries `baseDocumentRevision`, `baseRegistryRevision`, `serializedHtml`, and optionally a complete `registry`. The response returns both result revisions, `contentApprovalInvalidated`, and `transactionId`. HTTP 409 indicates a stale document or registry revision. HTTP 422 indicates schema, registry, reference, resource, runtime, or protected-metadata failure. A fully validated save whose document and registry are unchanged returns `noOp: true` and `transactionId: null`; it creates neither a backup nor a journal/report write.

If registry is omitted, the base registry revision must still match and the document is validated against the current registry. New/changed registry references, provenance/source metadata, LaTeX linked to an equation ID, or protected references therefore fail unless matching registry definitions are included in the same transaction. A chart/table/equation without its provenance ID may exist only as an authoring draft; the content-review gate rejects it.

Content review requires `equationId` for equation-like text, `chartId` for charts, `tableId` for tables, and `figureId` or `assetId` for source-backed images/SVG. It also rejects every element marked `unprovenancedDraft`. This keeps free authoring available without allowing an unregistered research claim or data object into approved final content.

Authoring permits text/notes, slides/elements, data/media, and link/morph/state/connector changes. Changing an existing element's `id` or `type` in place is rejected; use an explicit slide replace. All persistence uses the API or common storage layer, never direct file writes.

## Approval and final handoff

`begin-content-review` calculates a consistent snapshot. `approve-content` records both `sha256:` revisions and the canonical `bento/content-approval/v1` digest. Revision drift makes approval pending on every status/transition/mutation check; no watcher is required.

`begin-finalization` revalidates the exact approved revisions and atomically creates:

- final Bento HTML and JSON;
- final registry;
- immutable baseline Bento JSON;
- immutable baseline registry;
- baseline revision/fingerprint metadata in `deck.yaml`.

Finalization changes may affect only geometry, presentation style, theme/background, and z-order. Final validation compares against both baselines and rejects content, structure, identity, data, references, or registry changes.

Final Work editor validation always compares the current/proposed final document with the immutable document baseline, never with the final itself. `approve-final` is run only after stopping the editor and records the exact document, HTML, registry, and runtime revisions. `complete` accepts only that unchanged revision tuple. Use `reopen-finalization` to clear approval before editing an approved or completed deck.

An explicit full authoring reset requires `--confirm RESET-AUTHORING-FROM-HTML`, is allowed only in `bento_authoring`, creates a backup, rebuilds generated/authoring transactionally, invalidates content approval, and proves final artifacts were unchanged.
