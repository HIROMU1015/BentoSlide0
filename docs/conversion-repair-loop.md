# Converter repair loop

HTML-to-Bento conversion defects are repaired in an isolated workspace. The repair loop is not a workflow transition, source of truth, approval artifact, or formal conversion. It exists to shorten the cycle from a failed converter check to a focused regression result.

Before formal conversion starts, `prepare-conversion` runs the browser-free HTML preflight and changes workflow state only when it passes. A preflight failure therefore produces diagnostics without creating or replacing formal generated Bento artifacts. The formal build repeats preflight, but converter repair iterations stay in this isolated workspace; after focused checks pass, run one cache-disabled formal conversion and one final full suite.

## Run the repair build

From the repository root:

```powershell
python -m scripts.run_conversion_repair
```

The runner infers the configured single or modular HTML source, registry, and Bento base from `deck.yaml`. It writes only under `output/conversion-repair/`, holds a repository-local OS writer lease for the repair output, and never writes generated, authoring, final, canonical HTML/registry, or workflow state.

The first run renders complete source and Bento evidence. Later runs reuse only cache records whose slide input, relevant implementation phase, runtime, browser/font environment, and PNG revision still match. A converter-only fix can reuse source layout evidence; a browser-check fix invalidates Bento render evidence without invalidating source layout; a visual-comparison fix invalidates comparison records. Formal conversion and every approval/completion gate continue to use `incremental=False` and full validation.

## Failure evidence

`output/conversion-repair/status.json` records the current phase, elapsed time, per-phase timings, failure, retryability, diagnostics, selected focused tests, and cache hit/miss counts. Each failed attempt is retained under:

```text
output/conversion-repair/attempts/<attempt-id>/
├─ diagnostics.json
├─ evidence/
│  ├─ diagnostics/
│  ├─ screenshots/
│  └─ conversion-report.json
└─ reproducers/
   └─ <slide>/
      ├─ reproducer.json
      ├─ deck/...
      └─ Bento_Slides.base.bento.html
```

When a `ValidationError` contains several issue records, every issue becomes a separate structured diagnostic. The runner extracts slide/element/field/fix context and creates one single-slide reproducer per identified slide while retaining global HTML/CSS and hashed local dependencies. If the failure cannot identify a slide, it stores a complete input snapshot. Invalid discovery inputs still preserve the original HTML, registry, and Bento base. Modular failures retain available evidence; per-slide modular reproducers are a later extension.

The reproducer manifest includes revisions and a standalone command. It is diagnostic evidence only and must not be adopted as canonical content.

The `preflighting` phase runs before Chromium. Static safety errors therefore produce a reproducer and `evidence/diagnostics/html-preflight.json` without paying the browser startup/render cost. Editability warnings are retained in a successful conversion report and may be reviewed independently with `scripts.check_html_preflight`.

## Focused validation after a fix

Tell the runner which implementation paths changed and optionally execute the selected tests:

```powershell
python -m scripts.run_conversion_repair `
  --changed-path bento_converter/html_converter.py `
  --run-focused-tests
```

Selection combines the last failed phase with changed implementation paths. Unit and browser targets are deduplicated; browser targets run with `BENTO_BROWSER_TEST=1`. Results are stored in `output/conversion-repair/focused-tests.json`.

Use this order during repair:

1. Repair-runner build with persisted diagnostics and cache.
2. Exact single-slide reproducer when one exists.
3. Selected unit/browser regression tests.
4. Repeat until the repair build and selected tests pass.
5. Run one cache-disabled formal conversion.
6. Run the normal full test/CI-equivalent suite once before handoff.

Do not repeatedly run the complete suite after every small code edit. Do not accept a repair-cache hit as approval evidence, skip the final full build, modify canonical HTML to hide a converter defect, or publish `output/conversion-repair/`.
