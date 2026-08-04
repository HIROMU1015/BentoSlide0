---
name: bento-slide-build
description: Build, inspect, validate, and browser-check deterministic Bento Slides HTML from HTML/CSS plus registry JSON, or from the legacy coordinate design JSON, using an existing Bento base HTML. Use when asked to generate or regenerate a .bento.html deck, verify Bento runtime integrity, inspect native-versus-fallback decisions, compare source and Bento screenshots, or refresh slide evidence in this repository.
---

# Bento slide build

Work from the repository root. For new work, treat chapter HTML/CSS and registry JSON together as the source of truth. Do not redesign, rewrite copy, or modify the Bento runtime. The coordinate design JSON flow is legacy compatibility only.

## HTML-first flow

1. Read `docs/html-first-authoring-contract.md` and identify matching sorted `*.preview.html` / `*.registry.json` chapters.
2. Build the complete evidence bundle:

```powershell
python -m scripts.build_bento_from_html --html-dir input/ --registry-dir input/ --base Bento_Slides.base.bento.html --output output/presentation.bento.html
```

3. Inspect `conversion-report.json`. Report every native compatibility class, fallback, embedded/unresolved local resource count, correction policy/reinspection, unresolved overlap diagnostic, protected-content check, actual screenshot metric/crop result, critical reason/status contribution, and runtime result. Treat Bento API mutations through `loadDoc()` as API edits, not simulated user typing/dragging.
4. Keep semantic elements native. Use SVG/image only for the smallest block that requires fallback. Never silently flatten a whole slide.
5. Verify the deterministic double build when reproducibility evidence is requested:

```powershell
python -m scripts.check_html_first_determinism --html-dir input/ --registry-dir input/ --base Bento_Slides.base.bento.html --report output/determinism-report.json
```

6. Run the full test matrix shown below. Confirm `diagnostics/resource-scan.json` passes, `summary.criticalElementFail` is zero, fallback capture is scoped to each slide, and missing/duplicate captures include contextual errors. For CI handoff, confirm the `html-first-evidence` artifact contains the HTML/JSON, report, resource scan, computed layout, browser check, paired screenshots, tests, and determinism report.

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
