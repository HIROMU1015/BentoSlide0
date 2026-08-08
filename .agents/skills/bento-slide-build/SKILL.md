---
name: bento-slide-build
description: Manage the repository-centered BentoSlide workflow and build, inspect, validate, author, browser-check, fast-edit, and finalize deterministic Bento Slides from a single fixed-size HTML/registry deck, migrated modular HTML chapters, imported static HTML, or legacy coordinate JSON. Use for short deck commands, deck.yaml stages, section approval, HTML preview/conversion, Bento authoring and content approval, segment import/replace, localhost Work editor persistence, transaction recovery and writer leases, final presentation edits, runtime integrity, screenshots, or final validation in this repository.
---

# Bento slide build

Work from the repository root. Read `START_HERE.md`, `deck.yaml`, and `python -m scripts.deck_workflow status --json` first. Route through `workflow/WORKFLOW.md`; do not ask the user for routine paths, IDs, logs, ports, or CLI steps. Use the manifest-listed sources as factual authority. Do not redesign approved composition, rewrite copy, or modify the Bento runtime.

## Route the workflow

- `この資料を作成して`: discover sources, create planning artifacts, configure all sections, submit the plan, and request material approval.
- `この方針で進めて`: approve the plan, author the first incomplete section in the single HTML/registry pair, start HTML preview, and request visual approval.
- `次へ`: approve the current section digest and select the next; become conversion-ready only when all current digests pass.
- `BentoSlideに変換して`: require conversion readiness, build and verify generated output, then enter `bento_authoring`; do not create final yet.
- `BentoSlideで編集を開始して`: require `bento_authoring` and start the shared authoring-mode Work editor without crossing an approval gate.
- `内容を確定して`: validate authoring artifacts, enter `content_review`, and request content/structure approval; do not approve automatically.
- `この内容で最終調整へ`: treat the phrase as approval of the currently reviewed two revisions, record their canonical digest, and initialize final artifacts and both baselines transactionally.
- `最終調整を開始して`: require `bento_finalization`, use presentation-only final editing, save/reload, and complete final validation.

Use `scripts.deck_workflow` for every state change. Recompute revision/digest validity rather than trusting chat history. Run `resume` after resolving a blocker. For schema v1, run `migrate --dry-run` before `migrate`; migration is stage-preserving and late-stage evidence must validate before any change.

For schema v2 project metadata, use `python -m scripts.deck_workflow set-project --kind ... --title ...` only in `initialized` or `planning`. It is an agent-facing setup command rather than a ninth user short phrase; it changes neither workflow stage nor approvals.

## HTML authoring and conversion

For schema v2 `single`/`imported`, use the paths in `authoring.entryHtml` and `authoring.registry`. Each slide is a 1280 x 720 `section.slide` with stable `data-slide-id` and `data-section-id`. Read `docs/html-first-authoring-contract.md`. Treat the pair as the pre-conversion source of truth; a section approval includes DOM, registry projection, asset hashes, and global CSS/theme.

Build the configured single pair:

```powershell
python -m scripts.build_bento_from_html --html deck/deck.preview.html --registry deck/deck.registry.json --base Bento_Slides.base.bento.html --output output/presentation.generated.bento.html
```

Migrated modular decks instead use `--html-dir chapters/ --registry-dir chapters/`. Preserve native semantic elements and localize fallback to the smallest block. Inspect conversion report, computed layout, resource scan, browser check, browser-environment fingerprint, screenshots, native/fallback classes, crop results, reference/protected checks, serialize round-trip, and runtime fingerprint. Browser conversion uses one shared Chromium with isolated deterministic contexts and blocks all HTTP(S), including loopback; the known Bento release-manifest probe remains blocked/recorded while any other blocked request fails. For interactive iteration only, `--incremental` may reuse slide evidence from `output/.bento-cache/` when its PNG revision also matches; never use it at conversion, content/final approval, or completion gates, which require full build/full validation. The cache is neither source of truth nor approval evidence. For reproducibility:

```powershell
python -m scripts.check_html_first_determinism --html deck/deck.preview.html --registry deck/deck.registry.json --base Bento_Slides.base.bento.html --report output/determinism-report.json
```

## Bento authoring

After `mark-converted` and `begin-authoring`, use `start_deck_workspace.cmd`. Authoring mode may change content/structure and its registry, but every save must use the Work editor API or common storage transaction with both base revisions. Never overwrite authoring HTML/JSON/registry directly. Existing ID/type changes require explicit slide replacement.

The server holds the artifact-set OS writer lease. An offline tool may write only after acquiring the same lease, or through a localhost API whose repository/mode/targets match exactly. Recover unfinished journals before reads/writes. Never roll back a valid commit because only its report failed.

Writer exclusion is per canonical artifact, not merely per complete set: any overlapping target conflicts, while disjoint sets may proceed. Treat `noOp: true` saves as successful validation without a backup or transaction.

For an added or targeted replacement slide during `bento_authoring`:

```powershell
python -m scripts.bento_segment import --html scratch/segments/add.preview.html --registry scratch/segments/add.registry.json
python -m scripts.bento_segment replace --html scratch/segments/replacement.preview.html --registry scratch/segments/replacement.registry.json --slide-id target-slide
```

Require browser round-trip evidence, outside-slide hash invariance, cross-slide reference validity, and shared-registry safety. Do not change generated or final. A full HTML reset is exceptional and requires `reset-authoring-from-html --confirm RESET-AUTHORING-FROM-HTML` in authoring stage.

Content approval must match both current `sha256:` revisions and the canonical `bento/content-approval/v1` digest. Any authoring document/registry mutation invalidates it. Only approved current revisions may be copied to final HTML/JSON/registry plus document/registry baselines.

Allow provenance drafts while authoring, but before content review require `equationId` for equations, `chartId` for charts, `tableId` for tables, and `figureId` or `assetId` for source-backed image/SVG elements. Reject `unprovenancedDraft` at that gate. Treat revision backups as complete only when their HTML/JSON/registry byte revisions match the transactionally written manifest.

## Finalization

In finalization, final `#bento-doc`, frozen final registry, and immutable baselines are authoritative. Use the browser UI for judgment/direct manipulation. For exact geometry, style, theme, background, or z-order, read `docs/fast-final-editing.md`, batch all requested changes, dry-run if uncertain, then save once:

```powershell
python -m scripts.apply_bento_final_edits --patch path/to/final-edit.json
```

Do not use fast-final editing for text/equations, data/media, IDs/types, slides, notes, behavior, or references. Do not reconvert into final, use `--reset-final`, or relax content protection for an ordinary layout request.

The injected toolbar must preserve `window.bento.serialize()` as a synchronous HTML-string API. It is detached before serialization and restored in `finally`; toolbar/host/loader/style identifiers never persist. Describe `loadDoc()` mutation as API editing, not simulated typing/dragging.

Final validation checks HTML/JSON equality, runtime fingerprint, recursive resources, references, frozen registry, both baselines, protected fingerprint, revisions/backups, serialize round-trip, and browser evidence.

Before `approve-final`, stop the final Work editor so its lifetime lease is released. Approval is bound to the exact document, HTML, registry, and runtime revisions; `complete` must recompute them. If edits are needed after approval or completion, run `python -m scripts.deck_workflow reopen-finalization` first and never write while the old approval remains current.

## Static HTML import

Treat general HTML as untrusted. Read `docs/html-import.md`, keep the original under `imports/`, and use:

```powershell
python -m scripts.import_html_deck --input imports/source.html --slide-selector ".slide"
```

Never execute/import scripts or fetch remote resources. Require an explicit selector when ambiguous. Preview/convert only normalized static output.

## Legacy JSON-first

Keep the legacy path unchanged:

```powershell
python -m scripts.build_bento --base Bento_Slides.base.bento.html --design gpt_bento_design.json --output demo.generated.bento.html
python -m scripts.validate_bento demo.generated.bento.html --base Bento_Slides.base.bento.html
```

For all routes, run the relevant focused tests, full `python -m unittest discover -v`, and the browser-gated suite when browser evidence is required. Keep implementation logic in `bento_converter/` and `scripts/`; do not duplicate it in this skill.
