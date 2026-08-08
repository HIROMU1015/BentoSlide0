# BentoSlide project router

At the start of every task, read `START_HERE.md`, `deck.yaml`, and `python -m scripts.deck_workflow status --json`. Treat `deck.yaml` as the sole machine-readable workflow state, then read the stage-specific rules in `workflow/WORKFLOW.md` and only the relevant specifications.

## Non-negotiable rules

- Infer routine paths, section/chapter selection, state transitions, logs, validation, preview, and conversion commands from the repository. Do not ask the user to repeat mechanical instructions.
- Use the manifest-listed primary sources as factual authority. Do not invent claims, conditions, symbols, signs, subscripts, assumptions, numbers, comparisons, or generalizations.
- Evaluate every slide for whether a diagram improves understanding. Prefer editable native text/shape/connector diagrams; bind source-derived native components through one assetless `figureId`, and bind every origin-bearing image to its registry `contentDigest`. Distinguish source-original, source-derived, and generated visuals through the registry. Never generate data, experimental/benchmark results, quantitative plots, or equations. Use the transactional visual-asset/PDF extraction route in `docs/visual-workflow.md`.
- For schema v2 `single`/`imported` authoring, `deck/deck.preview.html` and `deck/deck.registry.json` are the pre-conversion visual/provenance source of truth. Migrated `modular` decks retain paired chapter files.
- After conversion, generated artifacts are reproducible and read-only. During `bento_authoring`, authoring HTML/JSON/registry are authoritative. After content approval and final handoff, final `#bento-doc`, frozen final registry, and immutable baselines are authoritative.
- Never write Bento HTML/JSON/registry directly. Use the revision-checked Work editor API or the common transaction/storage layer. Never expose a partially replaced artifact set.
- Work editor authoring may change content and structure, but in-place existing `id`/`type` changes require explicit replacement. Finalization permits only geometry/presentation style/theme/background/z-order changes.
- If a user asks for content or structure changes during finalization/complete, route back through `reopen-current-section`, edit the authoring canonical section, re-accept sections, and obtain a fresh whole-deck approval. Before restarting finalization, stop the final editor and archive the complete previous final/baseline set through the dedicated transaction; never relax finalization protection, edit final content in place, or discard the old final silently.
- Use `python -m scripts.apply_bento_final_edits` only in finalization. Never use it for content or structure. Never reconvert over final, automatically reset final, or use `--allow-content-edit` for ordinary final adjustment.
- Preserve the Bento runtime, synchronous HTML-string `window.bento.serialize()` contract, resource/fallback rules, and legacy JSON-first behavior.
- Update state only through `scripts.deck_workflow`. Respect plan, section/chapter visual, Bento content-revision, and final approval gates. On a resolved blocker, run `resume`; never repair YAML fields manually.
- A content approval is valid only for the current authoring document and registry revisions. Recompute it on status and every relevant transition or mutation; stale approval is pending.
- A final approval is valid only for the exact final document, HTML, registry, and runtime revisions. Stop the editor before approval; use `reopen-finalization` before any later edit.
- Keep unregistered equations, charts, tables, source-backed media, and `unprovenancedDraft` elements in authoring only. Reject them at content review until their required registry IDs and provenance are complete.
- Server writers hold the OS-level artifact lease for their lifetime. Offline writers must acquire the same lease or use a positively identified matching localhost API; otherwise refuse.
- Recover unfinished transaction journals before serving reads or writes. A report-only failure keeps committed artifacts and is retried; unsafe recovery changes nothing.
- Never mutate generated/final during segment import or targeted replacement. Treat imported HTML as untrusted and preview only sanitized static output.

## Natural-language routing

Natural conversation is the primary UX. Infer the matching high-level operation, stop at the next human checkpoint, and report the current section in user language. Never require a stage name, section/slide ID, revision, registry field, file path, or CLI command. Persist a substantive new brief with `capture-request`; use `advance`, `approve-current`, `promote-current-section`, `edit-current`, `finish-current-section`, `reopen-current-section`, and `review-whole-deck` internally. `advance` never records approval.

For new schema v2 single/imported decks, use `planned -> html_authoring -> html_review -> bento_integration -> bento_authoring -> accepted`. Exactly one canonical source exists per section: planning, HTML, or Bento. Promotion converts only the approved section, preserves planning order, and leaves unrelated authoring slide hashes plus generated/final artifacts unchanged. Accepted sections remain reopenable. Require whole-deck content review after all sections are accepted.

The former fixed phrases remain compatibility aliases only. Their exact checkpoint behavior is documented in `docs/legacy-command-aliases.md`; do not present them as the primary UX.

Legacy schema v1 decks must be dry-run migrated with `deck_workflow migrate`; never move a late-stage deck back to Bento authoring merely because of migration.

See `START_HERE.md`, `workflow/WORKFLOW.md`, `docs/source-of-truth-policy.md`, `docs/authoring-lifecycle.md`, and `docs/artifact-transactions.md`.
