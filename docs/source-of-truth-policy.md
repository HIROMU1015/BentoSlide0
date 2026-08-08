# Source-of-truth policy

`deck.yaml` is always the only machine-readable workflow state. Planning Markdown is human evidence, not a second state store. Artifact authority changes by stage:

1. `initialized` through plan approval: manifest-listed sources plus approved planning.
2. HTML authoring/review: single `deck/deck.preview.html` plus `deck/deck.registry.json`; migrated modular projects use registered chapter pairs.
3. Conversion/validation: generated Bento HTML/JSON and generated registry are reproducible outputs and are never hand-edited.
4. Bento authoring/content review: authoring HTML/JSON/registry are the only editable content/structure set. HTML and generated registries remain unchanged.
5. Finalization/complete: final `#bento-doc`, frozen final registry, and immutable document/registry baselines are authoritative.

All Bento persistence goes through revision-aware API/storage transactions. Editing a JSON sidecar or registry alone is unsupported. Generated, authoring, and final artifacts use distinct paths. Segment operations change only authoring; final presentation edits change only final document fields allowed by the baseline.

For the rolling schema v2 route, authority is also tracked per section. `planned` is planning-canonical; `html_authoring`, `html_review`, and `bento_integration` are HTML-canonical; `bento_authoring` and `accepted` are Bento-canonical. A successful promotion changes that section's canonical source once and records the authoring document/registry revisions plus a section projection digest. The HTML remains historical evidence and is never synchronized from Bento. Reopening through HTML is an explicit redesign route; reopening through Bento continues from the latest revision.

Content approval binds the exact authoring document and registry revisions. Any drift makes it pending before status, save, review, approval, finalization, segment, offline, or migration operations continue. Finalization snapshots only freshly approved authoring content—not mutable generated output.

Never convert into final. Rebuilding generated does not reset authoring/final or replace baselines. `--reset-final`, `--allow-content-edit`, and full HTML-to-authoring reset are explicit exceptional operations, never default routing.

State changes use `scripts.deck_workflow`; multi-artifact state changes use the durable journal transaction. A blocked state preserves the full prior tuple for validated `resume`. Schema migration is idempotent and preserves late-stage final authority; it stops without modifying artifacts if required registry/baseline evidence is absent.
