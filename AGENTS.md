# BentoSlide project router

At the start of every task, read `START_HERE.md`, `deck.yaml`, and `python -m scripts.deck_workflow status --json`. Treat `deck.yaml` as the sole machine-readable workflow state, then read the stage-specific rules in `workflow/WORKFLOW.md` and only the relevant specifications.

## Non-negotiable rules

- Infer routine paths, section/chapter selection, state transitions, logs, validation, preview, and conversion commands from the repository. Do not ask the user to repeat mechanical instructions.
- Use the manifest-listed primary sources as factual authority. Do not invent claims, conditions, symbols, signs, subscripts, assumptions, numbers, comparisons, or generalizations.
- For schema v2 `single`/`imported` authoring, `deck/deck.preview.html` and `deck/deck.registry.json` are the pre-conversion visual/provenance source of truth. Migrated `modular` decks retain paired chapter files.
- After conversion, generated artifacts are reproducible and read-only. During `bento_authoring`, authoring HTML/JSON/registry are authoritative. After content approval and final handoff, final `#bento-doc`, frozen final registry, and immutable baselines are authoritative.
- Never write Bento HTML/JSON/registry directly. Use the revision-checked Work editor API or the common transaction/storage layer. Never expose a partially replaced artifact set.
- Work editor authoring may change content and structure, but in-place existing `id`/`type` changes require explicit replacement. Finalization permits only geometry/presentation style/theme/background/z-order changes.
- Use `python -m scripts.apply_bento_final_edits` only in finalization. Never use it for content or structure. Never reconvert over final, automatically reset final, or use `--allow-content-edit` for ordinary final adjustment.
- Preserve the Bento runtime, synchronous HTML-string `window.bento.serialize()` contract, resource/fallback rules, and legacy JSON-first behavior.
- Update state only through `scripts.deck_workflow`. Respect plan, section/chapter visual, Bento content-revision, and final approval gates. On a resolved blocker, run `resume`; never repair YAML fields manually.
- A content approval is valid only for the current authoring document and registry revisions. Recompute it on status and every relevant transition or mutation; stale approval is pending.
- Server writers hold the OS-level artifact lease for their lifetime. Offline writers must acquire the same lease or use a positively identified matching localhost API; otherwise refuse.
- Recover unfinished transaction journals before serving reads or writes. A report-only failure keeps committed artifacts and is retried; unsafe recovery changes nothing.
- Never mutate generated/final during segment import or targeted replacement. Treat imported HTML as untrusted and preview only sanitized static output.

## Short user instructions

- `この資料を作成して`: discover manifest sources, create planning artifacts, register all planned sections, submit the plan, and request only material approval.
- `この方針で進めて`: approve the plan, author the first incomplete section in the single HTML/registry source, start HTML preview, and request visual approval.
- `次へ`: approve the current section and select the next; when all are current and approved, become conversion-ready.
- `BentoSlideに変換して`: validate approved section digests, convert to generated artifacts, collect all evidence, initialize Bento authoring, and start the authoring Work editor.
- `この内容で確定`: validate the current authoring document/registry, record their revisions and canonical approval digest, then initialize final artifacts and baselines transactionally.
- `最終調整を開始して`: require `bento_finalization`, start finalization mode, edit presentation only, save/reload, and run final technical validation.

Legacy schema v1 decks must be dry-run migrated with `deck_workflow migrate`; never move a late-stage deck back to Bento authoring merely because of migration.

See `START_HERE.md`, `workflow/WORKFLOW.md`, `docs/source-of-truth-policy.md`, `docs/authoring-lifecycle.md`, and `docs/artifact-transactions.md`.
