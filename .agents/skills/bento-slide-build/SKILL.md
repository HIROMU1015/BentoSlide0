---
name: bento-slide-build
description: Manage the repository-centered BentoSlide workflow and build, inspect, validate, browser-check, and finalize deterministic Bento Slides HTML from chapter HTML/CSS plus registry JSON, or from legacy coordinate design JSON. Use for short deck commands, deck.yaml stages, chapter preview, HTML-first conversion, localhost Work editor finalization, runtime integrity, native/fallback evidence, screenshots, or final slide validation in this repository.
---

# Bento slide build

Work from the repository root. Read `START_HERE.md` and `deck.yaml` first. Route the request through `workflow/WORKFLOW.md` and use `python -m scripts.deck_workflow status --json`; do not ask the user for routine filenames, chapter numbers, logs, or CLI steps. Treat chapter HTML/CSS and registry JSON together as the source of truth before conversion. Do not redesign, rewrite copy, or modify the Bento runtime. The coordinate design JSON flow is legacy compatibility only.

## Local workflow

- For `この資料を作成して`, resolve sources, create planning artifacts, register all chapters, and submit the plan for content approval.
- For `この方針で進めて`, record plan approval, author the first incomplete HTML/registry pair, start `start_html_preview.cmd`, and request visual approval.
- For `次へ`, approve the current chapter through the workflow CLI and select the next automatically; become conversion-ready only when every pair is approved.
- For `BentoSlideに変換して`, require `ready_for_conversion`, run the verified HTML-first flow below, mark conversion only after evidence exists, and begin finalization.
- For `最終調整を開始して`, require `bento_finalization`, prefer `start_deck_workspace.cmd` or the existing Bento launcher, retain final as authoritative, and run final verification after save/reload.

Use `deck.yaml` as the only machine state source. State changes must go through `scripts.deck_workflow` so schema, approvals, files, atomic writes, and handoffs are validated. Do not infer state from `planning/work-log.md` or chat history.
When a blocker is resolved, run `python -m scripts.deck_workflow resume`; it revalidates the saved pre-block stage. Never repair stage fields manually.

## HTML-first flow

Use these four ordered stages. Do not skip from source authoring directly to final editing.

### 1. HTML-first build

Read `docs/html-first-authoring-contract.md`, identify matching sorted `*.preview.html` / `*.registry.json` chapters, and build the complete evidence bundle:

```powershell
python -m scripts.build_bento_from_html --html-dir chapters/ --registry-dir chapters/ --base Bento_Slides.base.bento.html --output output/presentation.generated.bento.html
```

Use the output path recorded in `deck.yaml`.

Keep semantic elements native. Use SVG/image only for the smallest block that requires fallback. Never silently flatten a whole slide.

### 2. Conversion verification

Inspect `conversion-report.json`. Report every native compatibility class, fallback, embedded/unresolved local resource count, correction policy/reinspection, unresolved overlap diagnostic, protected-content check, actual screenshot metric/crop result, critical reason/status contribution, and runtime result. Treat Bento API mutations through `loadDoc()` as API edits, not simulated user typing/dragging.

Verify the deterministic double build when reproducibility evidence is requested:

```powershell
python -m scripts.check_html_first_determinism --html-dir chapters/ --registry-dir chapters/ --base Bento_Slides.base.bento.html --report output/determinism-report.json
```

Run the full test matrix shown below. Confirm `diagnostics/resource-scan.json` recursively covers the generated document and passes, media posters/fragments are portable, `summary.criticalElementFail` is zero, and fallback capture remains slide-scoped.

### 3. Work editor finalization

Complete the HTML-first build and conversion verification first. On Windows, prefer the repository-root one-click launcher over asking the user to enter a command manually:

```powershell
.\start_bento_editor.cmd
```

It starts the default localhost editor in the background without `--reset-final` or `--allow-content-edit`; keep the Work browser tab open and reload it. Use `stop_bento_editor.cmd` only when the editor should stop. Read `docs/work-editor-finalization.md` for custom ports/paths, session files, and safe PID validation.

Use the direct command only for non-Windows or explicitly customized/manual operation:

```powershell
python -m scripts.run_bento_work_editor --source output/presentation.generated.bento.html --target output/presentation.final.bento.html --registry output/diagnostics/merged-registry.json --port 8765
```

Open the localhost URL in the Work browser and use the injected controls to save, validate, revert, or reload. Describe Bento canvas interaction as UI editing and the persistence step as Work editor API saving. Treat an existing final `#bento-doc` as authoritative. The injected guard must preserve `window.bento.serialize()` as a synchronous HTML-string API: detach temporary Work editor UI immediately before serialization, restore it in `finally`, and persist only the validated `#bento-doc`. Never change the runtime API return type for Work editor needs. Never rerun HTML-first conversion into the final path or overwrite it unless the user explicitly requests `--reset-final`.

### 4. Final validation

After editing, validate final HTML/JSON equality, runtime fingerprint, resource scan, protected content, immutable finalization-baseline fingerprint, revision/backup evidence, browser serialize round-trip, and the usual Bento browser check. The baseline permits geometry/style/theme/z-order edits but rejects content, IDs/types, slide structure, data, notes, behavior, and references. For CI handoff, confirm `html-first-evidence` includes `work-editor-evidence/`.

## Legacy JSON-first flow

1. Identify the design JSON and `Bento_Slides.base.bento.html`.
2. Build to a path different from the base:

```powershell
python -m scripts.build_bento --base Bento_Slides.base.bento.html --design gpt_bento_design.json --output demo.generated.bento.html
```

3. Validate the document and prove that only `#bento-doc` differs from the base:

```powershell
python -m scripts.validate_bento demo.generated.bento.html --base Bento_Slides.base.bento.html
python -m scripts.inspect_bento demo.generated.bento.html
```

4. Run the tests:

```powershell
python -m unittest discover -v
$env:BENTO_BROWSER_TEST = "1"
python -m unittest discover -v
Remove-Item Env:BENTO_BROWSER_TEST
```

5. When browser evidence or screenshots are required, install `requirements-browser.txt` if needed and run:

```powershell
python -m scripts.check_bento_browser demo.generated.bento.html --design gpt_bento_design.json --screenshots-dir . --screenshot-prefix demo-slide
```

Before replacing an existing official demo, compare its hash and extracted Bento document with the generated file. Replace it only after classifying every difference. Report build warnings, test results, runtime integrity, browser results, screenshot paths, and the observed `equationId` / `latexSource` save behavior. Describe clicks as UI selection, but describe text/shape/equation mutations performed through `loadDoc()` as Bento API edits, not UI edits.

Read `docs/html-to-bento-conversion-spec.md`, `docs/bento-native-feature-support.md`, and `docs/fallback-policy.md` for HTML-first mapping changes. Read `docs/bento-conversion-spec.md` for the legacy path. Keep transformation logic in `bento_converter/` and `scripts/`; never duplicate it in this skill.
