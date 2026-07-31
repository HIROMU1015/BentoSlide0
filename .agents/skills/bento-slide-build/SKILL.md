---
name: bento-slide-build
description: Build, inspect, validate, and browser-check deterministic Bento Slides HTML from a GPT-authored coordinate design JSON and an existing Bento base HTML. Use when asked to generate or regenerate a .bento.html deck, verify Bento runtime integrity, compare a generated deck with the checked-in demo, or refresh Bento slide screenshots in this repository.
---

# Bento slide build

Work from the repository root. Treat the GPT design JSON as the content and visual source of truth. Do not redesign, rewrite copy, adjust coordinates, or modify the Bento runtime.

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

Read `docs/bento-conversion-spec.md` when changing mappings, validators, supported fields, or metadata policy. Keep all transformation logic in `bento_converter/` and `scripts/`; never duplicate it in this skill.
