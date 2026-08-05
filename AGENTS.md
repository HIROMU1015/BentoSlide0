# BentoSlide project router

At the start of every task, read `START_HERE.md` and `deck.yaml`. Treat `deck.yaml` as the sole machine-readable workflow state, then read only the stage-specific rules in `workflow/WORKFLOW.md` and the relevant files under `instructions/`.

## Non-negotiable rules

- Infer routine filenames, chapter numbers, state transitions, logs, validation, preview commands, and conversion commands from the repository. Do not ask the user to repeat mechanical instructions.
- Use primary sources under `sources/` as factual authority. Do not invent claims, conditions, symbols, signs, subscripts, assumptions, numbers, comparisons, or generalizations.
- Before conversion, paired `chapters/chapter-XX.preview.html` and `.registry.json` files are authoritative for visual design and provenance.
- After finalization begins, `output/presentation.final.bento.html` and its `#bento-doc` are authoritative. Never reconvert HTML over final edits.
- Never edit generated output manually. Never use `--reset-final` automatically. Never use `--allow-content-edit` for ordinary layout adjustment.
- Record machine state through `python -m scripts.deck_workflow`; keep `planning/work-log.md` concise. Do not bypass plan, chapter-visual, or final approval gates.
- When a reported blocker is resolved, run `python -m scripts.deck_workflow resume`; never ask the user to repair workflow fields manually.
- Preserve the Bento runtime, Work editor API/revisions, synchronous `window.bento.serialize()` string contract, generated/final boundary, resource/fallback behavior, and legacy JSON-first workflow.
- Treat the saved finalization baseline as immutable. Final validation may accept layout/style/z-order changes but must reject content, identity, data, reference, or slide-structure changes.

## Short user instructions

- `この資料を作成して`: initialize source discovery, produce planning artifacts, register all planned chapters, submit the plan, and ask only for material content approval.
- `この方針で進めて`: record plan approval, begin the first incomplete chapter, create its HTML/registry pair, start HTML preview, and move to visual review.
- `次へ`: approve the current chapter composition, begin the next incomplete chapter, or move to conversion readiness when all chapters are approved.
- `BentoSlideに変換して`: require conversion readiness, run the current HTML-first pipeline and all verification, retain any existing final, then hand off to Bento finalization.
- `最終調整を開始して`: require `bento_finalization`, start the existing Work editor, adjust layout/style without content mutation by default, save, reload, and verify.

See `START_HERE.md` for the human entry point, `workflow/WORKFLOW.md` for transitions, and `instructions/00_full_project_instructions.md` for content/design rules.
