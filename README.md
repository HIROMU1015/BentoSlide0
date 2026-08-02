# BentoSlide0

Deterministic conversion tools for producing editable Bento Slides HTML. The default pipeline is HTML-first: GPT authors fixed-size HTML/CSS plus a registry JSON, Chromium resolves the computed layout, and the converter emits native Bento elements into an unchanged official Bento runtime.

The previous coordinate-design JSON pipeline remains available as a legacy compatibility path.

## HTML-first build

Requirements:

```powershell
python -m pip install -r requirements-browser.txt
python -m playwright install chromium
```

Build one or more lexically sorted chapters:

```powershell
python -m scripts.build_bento_from_html `
  --html-dir input/ `
  --registry-dir input/ `
  --base Bento_Slides.base.bento.html `
  --output output/presentation.bento.html
```

The command creates:

```text
output/
├── presentation.bento.html
├── presentation.bento.json
├── conversion-report.json
├── screenshots/
│   ├── source/
│   └── bento/
└── diagnostics/
    ├── browser-check.json
    ├── computed-layout.json
    └── merged-registry.json
```

`presentation.bento.html` differs from the selected base only inside `script#bento-doc`. The command fails on malformed registries, duplicate IDs, missing protected content, broken state/connector references, out-of-contract source sizes, Bento validation failures, runtime mutations, UI startup failures, serialize round-trip failures, or a fail-level source/Bento image comparison. Warning-level native rendering differences remain visible in the report.

See [the authoring contract](docs/html-first-authoring-contract.md), [conversion specification](docs/html-to-bento-conversion-spec.md), [native support matrix](docs/bento-native-feature-support.md), and [fallback policy](docs/fallback-policy.md).

## Legacy JSON-first build

The old deterministic entry point is intentionally preserved:

```powershell
python -m scripts.build_bento `
  --base Bento_Slides.base.bento.html `
  --design gpt_bento_design.json `
  --output demo.generated.bento.html
```

Its contract remains documented in [bento-conversion-spec.md](docs/bento-conversion-spec.md).

## Verification

```powershell
python -m unittest discover -v
$env:BENTO_BROWSER_TEST = "1"
python -m unittest discover -v
python -m scripts.check_html_first_determinism --html-dir tests/fixtures/html_first --registry-dir tests/fixtures/html_first --base Bento_Slides.base.bento.html --report determinism-report.json
Remove-Item Env:BENTO_BROWSER_TEST
```

The browser-gated suite covers the feature matrix under `tests/fixtures/html_first`, including chapter combination, native feature rendering, CSS compatibility classification, six transform cases, simple and complex tables, morph/state metadata, localized fallback, runtime integrity, Bento API editing, serialization, perceptual screenshot comparison, and independent-directory determinism.

GitHub Actions uploads the complete `html-first-evidence` artifact and writes a job summary with test count, native/fallback counts, visual pass/warning/fail counts, unresolved diagnostics, serialize status, determinism status, and HTML/Bento JSON SHA-256 values.
