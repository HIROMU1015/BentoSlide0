"""Browser-level checks for a generated, editable Bento Slides document."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .bento_validator import validate_bento_doc
from .design_loader import load_design
from .errors import BrowserCheckError
from .html_document import extract_bento_doc

CHECKED_ELEMENT_IDS = (
    "slide-1-title",
    "slide-1-shape",
    "hamiltonian-equation",
)


@dataclass(frozen=True)
class BrowserCheckReport:
    browser: str
    slide_count: int
    element_count: int
    checked_coordinates: dict[str, dict[str, float]]
    ui_selection: bool
    text_edit: bool
    shape_move: bool
    equation_rerender: bool
    serialized_document_valid: bool
    equation_id_preserved: bool
    latex_source_preserved: bool
    latex_source_auto_synced: bool
    metadata_source_of_truth: str
    screenshots: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _fail(message: str) -> None:
    raise BrowserCheckError(message)


def _require(condition: bool, message: str) -> None:
    if not condition:
        _fail(message)


def _chrome_candidates(explicit: str | Path | None) -> list[Path]:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    configured = os.environ.get("BENTO_CHROME_PATH")
    if configured:
        candidates.append(Path(configured))
    candidates.extend(
        [
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        ]
    )
    return candidates


def find_browser_executable(explicit: str | Path | None = None) -> Path | None:
    for candidate in _chrome_candidates(explicit):
        if candidate.is_file():
            return candidate
    return None


def _source_coordinates(design: dict[str, Any]) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for slide in design["slides"]:
        for element in slide["elements"]:
            if element["id"] in CHECKED_ELEMENT_IDS:
                result[element["id"]] = {
                    field: element[field] for field in ("x", "y", "w", "h")
                }
    return result


def _native_coordinates(document: dict[str, Any]) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for slide in document["slides"]:
        for element in slide["elements"]:
            if element["id"] in CHECKED_ELEMENT_IDS:
                result[element["id"]] = {
                    field: element[field] for field in ("x", "y", "w", "h")
                }
    return result


def _element(document: dict[str, Any], element_id: str) -> dict[str, Any]:
    for slide in document["slides"]:
        for element in slide["elements"]:
            if element.get("id") == element_id:
                return element
    _fail(f"Browser document does not contain element {element_id!r}.")


def run_browser_check(
    html_path: str | Path,
    *,
    design_path: str | Path | None = None,
    screenshots_dir: str | Path | None = None,
    screenshot_prefix: str = "demo-slide",
    browser_executable: str | Path | None = None,
) -> BrowserCheckReport:
    """Open a local deck, verify rendering/API round-trip, and optionally capture slides."""

    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserCheckError(
            "Playwright is not installed. Run: python -m pip install -r requirements-browser.txt"
        ) from exc

    path = Path(html_path).resolve()
    if not path.is_file():
        _fail(f"Bento HTML does not exist: {path}")
    design = load_design(design_path) if design_path is not None else None
    executable = find_browser_executable(browser_executable)
    screenshots: list[str] = []

    try:
        with sync_playwright() as playwright:
            launch_options: dict[str, Any] = {
                "headless": True,
                "args": ["--allow-file-access-from-files"],
            }
            browser_name = "Playwright Chromium"
            if executable is not None:
                launch_options["executable_path"] = str(executable)
                browser_name = executable.name
            browser = playwright.chromium.launch(**launch_options)
            page = browser.new_page(
                viewport={"width": 1600, "height": 1000},
                device_scale_factor=1,
            )
            page_errors: list[str] = []
            console_errors: list[str] = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on(
                "console",
                lambda message: console_errors.append(message.text)
                if message.type == "error"
                else None,
            )
            page.goto(path.as_uri(), wait_until="load")
            page.wait_for_function(
                "window.bento && window.bento.doc && typeof window.bento.serialize === 'function'"
            )
            page.wait_for_timeout(500)

            _require(not page_errors, f"JavaScript page errors: {page_errors}")
            _require(not console_errors, f"Browser console errors: {console_errors}")
            _require(page.locator(".ed-root").count() == 1, "Bento editor UI did not start.")

            original_document = page.evaluate(
                "JSON.parse(JSON.stringify(window.bento.doc))"
            )
            validate_bento_doc(original_document)
            slide_count = len(original_document["slides"])
            element_count = sum(
                len(slide["elements"]) for slide in original_document["slides"]
            )
            _require(slide_count == 2, f"Expected 2 slides, found {slide_count}.")
            _require(
                page.locator(".ed-thumb").count() == slide_count,
                "The editor thumbnail count does not match the document.",
            )
            _require(
                page.get_by_text("GPTが座標まで設計する", exact=True).count() >= 1,
                "Expected slide-1 title was not rendered.",
            )
            _require(
                page.locator('[data-el-id="slide-1-shape"] rect').count() >= 1,
                "Expected rounded rectangle was not rendered.",
            )

            coordinates = _native_coordinates(original_document)
            _require(
                set(coordinates) == set(CHECKED_ELEMENT_IDS),
                f"Missing checked element coordinates: {coordinates}",
            )
            if design is not None:
                expected_coordinates = _source_coordinates(design)
                _require(
                    coordinates == expected_coordinates,
                    f"Browser coordinates differ from GPT design: actual={coordinates!r}; "
                    f"expected={expected_coordinates!r}",
                )

            title_locator = page.locator(
                '.ed-stage [data-el-id="slide-1-title"]'
            )
            _require(title_locator.count() == 1, "Slide-1 title is not selectable.")
            title_locator.click(position={"x": 20, "y": 20})
            selection = page.evaluate("Array.from(window.bento.selection || [])")
            ui_selection = "slide-1-title" in selection
            _require(ui_selection, f"UI selection failed: {selection!r}")

            before_equation = _element(original_document, "hamiltonian-equation")
            before_title = _element(original_document, "slide-1-title")
            before_shape = _element(original_document, "slide-1-shape")
            new_latex = r"H = H_0 + \alpha H_2"
            new_equation_html = f"$${new_latex}$$"
            edit_result = page.evaluate(
                """
                ({ titleHtml, shapeX, equationHtml }) => {
                  const doc = JSON.parse(JSON.stringify(window.bento.doc));
                  const all = doc.slides.flatMap(slide => slide.elements);
                  all.find(element => element.id === 'slide-1-title').html = titleHtml;
                  all.find(element => element.id === 'slide-1-shape').x = shapeX;
                  all.find(element => element.id === 'hamiltonian-equation').html = equationHtml;
                  return window.bento.loadDoc(JSON.stringify(doc));
                }
                """,
                {
                    "titleHtml": before_title["html"] + " [browser-check]",
                    "shapeX": before_shape["x"] + 1,
                    "equationHtml": new_equation_html,
                },
            )
            _require(edit_result is True, "window.bento.loadDoc() rejected edited JSON.")
            page.wait_for_timeout(400)

            page.locator(".ed-thumb").nth(1).click()
            page.wait_for_timeout(400)
            rendered_math = page.locator(
                '.ed-stage [data-el-id="hamiltonian-equation"] math'
            )
            equation_rerender = (
                rendered_math.count() >= 1
                and "2" in (rendered_math.first.text_content() or "")
            )
            _require(equation_rerender, "Edited LaTeX did not re-render as MathML.")

            serialized_html = page.evaluate("window.bento.serialize()")
            serialized_document = extract_bento_doc(serialized_html)
            validate_bento_doc(serialized_document)
            after_title = _element(serialized_document, "slide-1-title")
            after_shape = _element(serialized_document, "slide-1-shape")
            after_equation = _element(serialized_document, "hamiltonian-equation")
            text_edit = after_title["html"].endswith(" [browser-check]")
            shape_move = after_shape["x"] == before_shape["x"] + 1
            equation_id_preserved = (
                after_equation.get("equationId") == before_equation.get("equationId")
            )
            latex_source_preserved = (
                after_equation.get("latexSource") == before_equation.get("latexSource")
            )
            latex_source_auto_synced = after_equation.get("latexSource") == new_latex
            _require(text_edit, "Text edit was lost during window.bento.serialize().")
            _require(shape_move, "Shape move was lost during window.bento.serialize().")
            _require(
                after_equation.get("html") == new_equation_html,
                "Equation source edit was lost during window.bento.serialize().",
            )
            _require(equation_id_preserved, "equationId was removed during Bento save.")
            _require(latex_source_preserved, "latexSource was removed during Bento save.")

            restored = page.evaluate(
                "json => window.bento.loadDoc(json)",
                json.dumps(original_document, ensure_ascii=False),
            )
            _require(restored is True, "Could not restore the original document for screenshots.")
            page.wait_for_timeout(300)

            if screenshots_dir is not None:
                output_dir = Path(screenshots_dir)
                output_dir.mkdir(parents=True, exist_ok=True)
                for index in range(slide_count):
                    page.locator(".ed-thumb").nth(index).click()
                    page.wait_for_timeout(300)
                    page.evaluate(
                        """
                        () => {
                          document.querySelector('#bento-check-screenshot')?.remove();
                          const source = document.querySelector('.ed-stage .bento-slide');
                          const clone = source.cloneNode(true);
                          clone.id = 'bento-check-screenshot';
                          Object.assign(clone.style, {
                            position: 'fixed',
                            left: '0',
                            top: '0',
                            margin: '0',
                            transform: 'none',
                            zIndex: '2147483647'
                          });
                          document.body.appendChild(clone);
                        }
                        """
                    )
                    slide = page.locator("#bento-check-screenshot")
                    _require(slide.count() == 1, "Active slide surface is missing.")
                    box = slide.bounding_box()
                    expected_size = original_document["size"]
                    _require(
                        box is not None
                        and round(box["width"]) == expected_size["width"]
                        and round(box["height"]) == expected_size["height"],
                        f"Screenshot surface size differs from canvas: {box!r}",
                    )
                    output = output_dir / f"{screenshot_prefix}-{index + 1}.png"
                    slide.screenshot(path=str(output))
                    screenshots.append(str(output.resolve()))
                    page.evaluate("document.querySelector('#bento-check-screenshot')?.remove()")

            browser.close()
    except BrowserCheckError:
        raise
    except PlaywrightError as exc:
        raise BrowserCheckError(f"Playwright browser check failed: {exc}") from exc

    return BrowserCheckReport(
        browser=browser_name,
        slide_count=slide_count,
        element_count=element_count,
        checked_coordinates=coordinates,
        ui_selection=ui_selection,
        text_edit=text_edit,
        shape_move=shape_move,
        equation_rerender=equation_rerender,
        serialized_document_valid=True,
        equation_id_preserved=equation_id_preserved,
        latex_source_preserved=latex_source_preserved,
        latex_source_auto_synced=latex_source_auto_synced,
        metadata_source_of_truth="html",
        screenshots=tuple(screenshots),
    )
