---
name: bento-slide-build
description: Build, inspect, validate, browser-check, and finalize deterministic Bento Slides HTML from HTML/CSS plus registry JSON, or from the legacy coordinate design JSON, using an existing Bento base HTML. Use when asked to generate or regenerate a .bento.html deck, run the localhost Work editor, save final Bento edits, verify runtime integrity, inspect native-versus-fallback decisions, compare screenshots, or refresh slide evidence in this repository.
---

# Bento slide build

Work from the repository root. For new work, treat chapter HTML/CSS and registry JSON together as the source of truth. Do not redesign, rewrite copy, or modify the Bento runtime. The coordinate design JSON flow is legacy compatibility only.

## HTML-first flow

Use these four ordered stages. Do not skip from source authoring directly to final editing.

### 1. HTML-first build

Read `docs/html-first-authoring-contract.md`, identify matching sorted `*.preview.html` / `*.registry.json` chapters, and build the complete evidence bundle:

```powershell
python -m scripts.build_bento_from_html --html-dir input/ --registry-dir input/ --base Bento_Slides.base.bento.html --output output/presentation.generated.bento.html
```

Keep semantic elements native. Use SVG/image only for the smallest block that requires fallback. Never silently flatten a whole slide.

### 2. Conversion verification

Inspect `conversion-report.json`. Report every native compatibility class, fallback, embedded/unresolved local resource count, correction policy/reinspection, unresolved overlap diagnostic, protected-content check, actual screenshot metric/crop result, critical reason/status contribution, and runtime result. Treat Bento API mutations through `loadDoc()` as API edits, not simulated user typing/dragging.

Verify the deterministic double build when reproducibility evidence is requested:

```powershell
python -m scripts.check_html_first_determinism --html-dir input/ --registry-dir input/ --base Bento_Slides.base.bento.html --report output/determinism-report.json
```

Run the full test matrix shown below. Confirm `diagnostics/resource-scan.json` recursively covers the generated document and passes, media posters/fragments are portable, `summary.criticalElementFail` is zero, and fallback capture remains slide-scoped.

### 3. Work editor finalization

Complete the HTML-first build and conversion verification first. Then start finalization with:

```powershell
python -m scripts.run_bento_work_editor --source output/presentation.generated.bento.html --target output/presentation.final.bento.html --registry output/diagnostics/merged-registry.json --port 8765
```

Open the localhost URL in the Work browser and use the injected controls to save, validate, revert, or reload. Describe Bento canvas interaction as UI editing and the persistence step as Work editor API saving. Treat an existing final `#bento-doc` as authoritative. The injected guard must preserve `window.bento.serialize()` as a synchronous HTML-string API: detach temporary Work editor UI immediately before serialization, restore it in `finally`, and persist only the validated `#bento-doc`. Never change the runtime API return type for Work editor needs. Never rerun HTML-first conversion into the final path or overwrite it unless the user explicitly requests `--reset-final`.

### 4. Final validation

After editing, validate final HTML/JSON equality, runtime fingerprint, resource scan, protected content, revision/backup evidence, browser serialize round-trip, and the usual Bento browser check. For CI handoff, confirm `html-first-evidence` includes `work-editor-evidence/`.

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
