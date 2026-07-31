"""Generic browser-level checks for generated, editable Bento Slides documents."""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator

from .bento_validator import validate_bento_doc, validate_conversion
from .design_loader import load_design
from .design_validator import validate_design
from .errors import BrowserCheckError
from .html_document import extract_bento_doc

Frame = dict[str, float]
ElementLocation = tuple[int, str, str]


@dataclass(frozen=True)
class BrowserCheckReport:
    browser: str
    slide_count: int
    rendered_slide_count: int
    element_count: int
    checked_coordinates: dict[str, Frame]
    detected_types: dict[str, int]
    ui_selection: bool | None
    api_text_edit: bool | None
    api_shape_move: bool | None
    api_equation_edit: bool | None
    api_equation_rerender: bool | None
    serialize_roundtrip: bool
    equation_id_preserved: bool | None
    latex_source_preserved: bool | None
    latex_source_auto_synced: bool | None
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


def _key(slide_id: str, element_id: str) -> str:
    return f"{slide_id}/{element_id}"


def _iter_elements(document: dict[str, Any]) -> Iterator[tuple[int, str, dict[str, Any]]]:
    for slide_index, slide in enumerate(document["slides"]):
        for element in slide["elements"]:
            yield slide_index, slide["id"], element


def _coordinates(document: dict[str, Any]) -> dict[str, Frame]:
    return {
        _key(slide_id, element["id"]): {
            field: element[field] for field in ("x", "y", "w", "h")
        }
        for _, slide_id, element in _iter_elements(document)
    }


def _source_coordinates(design: dict[str, Any]) -> dict[str, Frame]:
    result: dict[str, Frame] = {}
    for slide in design["slides"]:
        for element in slide["elements"]:
            result[_key(slide["id"], element["id"])] = {
                field: element[field] for field in ("x", "y", "w", "h")
            }
    return result


def _element(
    document: dict[str, Any], slide_index: int, element_id: str
) -> dict[str, Any]:
    for element in document["slides"][slide_index]["elements"]:
        if element.get("id") == element_id:
            return element
    _fail(
        f"Browser document does not contain element {element_id!r} "
        f"on slide index {slide_index}."
    )


def _is_equation(element: dict[str, Any]) -> bool:
    source = element.get("html")
    return (
        element.get("type") == "text"
        and isinstance(source, str)
        and source.startswith("$$")
        and source.endswith("$$")
    )


def _targets(
    document: dict[str, Any], design: dict[str, Any] | None
) -> tuple[dict[str, ElementLocation], dict[str, int]]:
    targets: dict[str, ElementLocation] = {}
    counts: Counter[str] = Counter()
    if design is not None:
        for slide_index, slide in enumerate(design["slides"]):
            for element in slide["elements"]:
                kind = element["type"]
                counts[kind] += 1
                targets.setdefault(kind, (slide_index, slide["id"], element["id"]))
        return targets, dict(sorted(counts.items()))

    for slide_index, slide_id, element in _iter_elements(document):
        if _is_equation(element):
            kind = "latex"
        else:
            kind = element["type"]
        counts[kind] += 1
        targets.setdefault(kind, (slide_index, slide_id, element["id"]))
    return targets, dict(sorted(counts.items()))


def _movement(
    element: dict[str, Any], size: dict[str, float]
) -> tuple[str, float] | None:
    if element["x"] + element["w"] + 1 <= size["width"]:
        return "x", 1
    if element["x"] - 1 >= 0:
        return "x", -1
    if element["y"] + element["h"] + 1 <= size["height"]:
        return "y", 1
    if element["y"] - 1 >= 0:
        return "y", -1
    return None


def _default_screenshot_prefix(path: Path) -> str:
    name = path.name
    if name.endswith(".bento.html"):
        name = name[: -len(".bento.html")]
    else:
        name = path.stem
    return f"{name}-slide"


def run_browser_check(
    html_path: str | Path,
    *,
    design_path: str | Path | None = None,
    screenshots_dir: str | Path | None = None,
    screenshot_prefix: str | None = None,
    browser_executable: str | Path | None = None,
) -> BrowserCheckReport:
    """Verify UI startup, all slides/elements, API edits, save round-trip, and captures."""

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
    if design is not None:
        validate_design(design)
    executable = find_browser_executable(browser_executable)
    screenshots: list[str] = []
    prefix = screenshot_prefix or _default_screenshot_prefix(path)

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
            if design is not None:
                validate_conversion(design, original_document)
                expected_slide_ids = [slide["id"] for slide in design["slides"]]
                actual_slide_ids = [slide["id"] for slide in original_document["slides"]]
                _require(
                    actual_slide_ids == expected_slide_ids,
                    f"Browser slide order differs from GPT design: {actual_slide_ids!r}",
                )

            slide_count = len(original_document["slides"])
            element_count = sum(
                len(slide["elements"]) for slide in original_document["slides"]
            )
            _require(
                page.locator(".ed-thumb").count() == slide_count,
                "The editor thumbnail count does not match the document.",
            )

            coordinates = _coordinates(original_document)
            if design is not None:
                expected_coordinates = _source_coordinates(design)
                _require(
                    coordinates == expected_coordinates,
                    f"Browser coordinates differ from GPT design: actual={coordinates!r}; "
                    f"expected={expected_coordinates!r}",
                )
            targets, detected_types = _targets(original_document, design)

            rendered_slide_count = 0
            for slide_index, slide in enumerate(original_document["slides"]):
                page.locator(".ed-thumb").nth(slide_index).click()
                page.wait_for_timeout(150)
                active_slide = page.locator(".ed-stage .bento-slide")
                _require(active_slide.count() == 1, "The active Bento slide is missing.")
                _require(
                    active_slide.get_attribute("data-slide-id") == slide["id"],
                    f"Editor activated the wrong slide at index {slide_index}.",
                )
                rendered_ids = active_slide.locator("[data-el-id]").evaluate_all(
                    "elements => elements.map(element => element.dataset.elId)"
                )
                expected_ids = [element["id"] for element in slide["elements"]]
                _require(
                    sorted(rendered_ids) == sorted(expected_ids),
                    f"Rendered elements differ on slide {slide['id']!r}: "
                    f"actual={rendered_ids!r}; expected={expected_ids!r}",
                )
                for element in slide["elements"]:
                    locator = active_slide.locator("[data-el-id]").nth(
                        rendered_ids.index(element["id"])
                    )
                    if element["type"] == "shape":
                        _require(
                            locator.locator("svg").count() >= 1,
                            f"Shape {slide['id']}/{element['id']} was not rendered.",
                        )
                    if _is_equation(element):
                        _require(
                            locator.locator("math").count() >= 1,
                            f"Equation {slide['id']}/{element['id']} was not rendered.",
                        )
                rendered_slide_count += 1

            selection_target = (
                targets.get("text") or targets.get("shape") or targets.get("latex")
            )
            ui_selection: bool | None = None
            if selection_target is not None:
                slide_index, _, element_id = selection_target
                page.locator(".ed-thumb").nth(slide_index).click()
                page.wait_for_timeout(150)
                active_elements = page.locator(".ed-stage [data-el-id]")
                active_ids = active_elements.evaluate_all(
                    "elements => elements.map(element => element.dataset.elId)"
                )
                _require(
                    element_id in active_ids,
                    f"Element {element_id!r} is missing from the active slide.",
                )
                selectable = active_elements.nth(active_ids.index(element_id))
                _require(selectable.count() == 1, f"Element {element_id!r} is not selectable.")
                selectable.click(position={"x": 10, "y": 10})
                selection = page.evaluate("Array.from(window.bento.selection || [])")
                ui_selection = element_id in selection
                _require(ui_selection, f"UI selection failed: {selection!r}")

            edited_document = json.loads(json.dumps(original_document))
            text_marker = " [browser-check]"
            api_text_edit: bool | None = None
            api_shape_move: bool | None = None
            api_equation_edit: bool | None = None
            api_equation_rerender: bool | None = None
            equation_id_preserved: bool | None = None
            latex_source_preserved: bool | None = None
            latex_source_auto_synced: bool | None = None
            shape_movement: tuple[str, float] | None = None
            new_equation_html: str | None = None
            new_latex: str | None = None

            if "text" in targets:
                slide_index, _, element_id = targets["text"]
                _element(edited_document, slide_index, element_id)["html"] += text_marker
            if "shape" in targets:
                slide_index, _, element_id = targets["shape"]
                edited_shape = _element(edited_document, slide_index, element_id)
                shape_movement = _movement(edited_shape, edited_document["size"])
                if shape_movement is not None:
                    field, delta = shape_movement
                    edited_shape[field] += delta
            if "latex" in targets:
                slide_index, _, element_id = targets["latex"]
                edited_equation = _element(edited_document, slide_index, element_id)
                source = edited_equation["html"]
                new_latex = source[2:-2].strip() + r" + \mathrm{browsercheck}"
                new_equation_html = f"$${new_latex}$$"
                edited_equation["html"] = new_equation_html

            edit_result = page.evaluate(
                "json => window.bento.loadDoc(json)",
                json.dumps(edited_document, ensure_ascii=False),
            )
            _require(edit_result is True, "window.bento.loadDoc() rejected edited JSON.")
            page.wait_for_timeout(300)

            if "latex" in targets:
                slide_index, _, element_id = targets["latex"]
                page.locator(".ed-thumb").nth(slide_index).click()
                page.wait_for_timeout(300)
                active_elements = page.locator(".ed-stage [data-el-id]")
                active_ids = active_elements.evaluate_all(
                    "elements => elements.map(element => element.dataset.elId)"
                )
                _require(
                    element_id in active_ids,
                    f"Equation {element_id!r} is missing from the active slide.",
                )
                rendered_math = active_elements.nth(
                    active_ids.index(element_id)
                ).locator("math")
                api_equation_rerender = (
                    rendered_math.count() >= 1
                    and "browsercheck" in (rendered_math.first.text_content() or "")
                )
                _require(
                    api_equation_rerender,
                    f"Edited LaTeX for {element_id!r} did not re-render as MathML.",
                )

            serialized_html = page.evaluate("window.bento.serialize()")
            serialized_document = extract_bento_doc(serialized_html)
            validate_bento_doc(serialized_document)

            if "text" in targets:
                slide_index, _, element_id = targets["text"]
                after_text = _element(serialized_document, slide_index, element_id)
                api_text_edit = after_text["html"].endswith(text_marker)
                _require(api_text_edit, "Bento API text edit was lost during serialize().")
            if "shape" in targets and shape_movement is not None:
                slide_index, _, element_id = targets["shape"]
                before_shape = _element(original_document, slide_index, element_id)
                after_shape = _element(serialized_document, slide_index, element_id)
                field, delta = shape_movement
                api_shape_move = after_shape[field] == before_shape[field] + delta
                _require(api_shape_move, "Bento API shape move was lost during serialize().")
            if "latex" in targets:
                slide_index, _, element_id = targets["latex"]
                before_equation = _element(original_document, slide_index, element_id)
                after_equation = _element(serialized_document, slide_index, element_id)
                api_equation_edit = after_equation.get("html") == new_equation_html
                _require(
                    api_equation_edit,
                    "Bento API equation source edit was lost during serialize().",
                )
                if "equationId" in before_equation:
                    equation_id_preserved = (
                        after_equation.get("equationId")
                        == before_equation.get("equationId")
                    )
                    _require(
                        equation_id_preserved,
                        "equationId was removed during Bento serialize().",
                    )
                if "latexSource" in before_equation:
                    latex_source_preserved = (
                        after_equation.get("latexSource")
                        == before_equation.get("latexSource")
                    )
                    latex_source_auto_synced = (
                        after_equation.get("latexSource") == new_latex
                    )
                    _require(
                        latex_source_preserved,
                        "latexSource was removed during Bento serialize().",
                    )

            restored = page.evaluate(
                "json => window.bento.loadDoc(json)",
                json.dumps(original_document, ensure_ascii=False),
            )
            _require(restored is True, "Could not restore the original document for screenshots.")
            page.wait_for_timeout(200)

            if screenshots_dir is not None:
                output_dir = Path(screenshots_dir)
                output_dir.mkdir(parents=True, exist_ok=True)
                for index in range(slide_count):
                    page.locator(".ed-thumb").nth(index).click()
                    page.wait_for_timeout(200)
                    page.evaluate(
                        """
                        () => {
                          document.querySelector('#bento-check-screenshot')?.remove();
                          const source = document.querySelector('.ed-stage .bento-slide');
                          const clone = source.cloneNode(true);
                          clone.id = 'bento-check-screenshot';
                          Object.assign(clone.style, {
                            position: 'fixed', left: '0', top: '0', margin: '0',
                            transform: 'none', zIndex: '2147483647'
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
                    output = output_dir / f"{prefix}-{index + 1}.png"
                    slide.screenshot(path=str(output))
                    screenshots.append(str(output.resolve()))
                    page.evaluate(
                        "document.querySelector('#bento-check-screenshot')?.remove()"
                    )

            _require(not page_errors, f"JavaScript page errors: {page_errors}")
            _require(not console_errors, f"Browser console errors: {console_errors}")
            browser.close()
    except BrowserCheckError:
        raise
    except PlaywrightError as exc:
        raise BrowserCheckError(f"Playwright browser check failed: {exc}") from exc

    return BrowserCheckReport(
        browser=browser_name,
        slide_count=slide_count,
        rendered_slide_count=rendered_slide_count,
        element_count=element_count,
        checked_coordinates=coordinates,
        detected_types=detected_types,
        ui_selection=ui_selection,
        api_text_edit=api_text_edit,
        api_shape_move=api_shape_move,
        api_equation_edit=api_equation_edit,
        api_equation_rerender=api_equation_rerender,
        serialize_roundtrip=True,
        equation_id_preserved=equation_id_preserved,
        latex_source_preserved=latex_source_preserved,
        latex_source_auto_synced=latex_source_auto_synced,
        metadata_source_of_truth="html" if "latex" in targets else "not-applicable",
        screenshots=tuple(screenshots),
    )
