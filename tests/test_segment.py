from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from bento_converter.errors import BentoConverterError
from bento_converter.html_document import extract_bento_doc, load_html
from bento_converter.segment import merge_segment, slide_hashes


ROOT = Path(__file__).resolve().parents[1]


def registry() -> dict:
    return {
        "format": "bento/html-registry/v2", "unitId": "deck", "sources": {},
        "document": {}, "assets": {}, "fonts": {},
        "equations": {"hamiltonian_split": {"latex": "H = H_0 + \\alpha H_1"}},
        "figures": {}, "tables": {}, "charts": {},
        "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
    }


class SegmentMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = extract_bento_doc(load_html(ROOT / "demo.bento.html"))
        self.registry = registry()

    def new_slide(self, identifier: str = "segment-slide") -> dict:
        slide = copy.deepcopy(self.document["slides"][0])
        slide["id"] = identifier
        for index, element in enumerate(slide["elements"]):
            element["id"] = f"{identifier}-element-{index + 1}"
            element.pop("equationId", None)
            element.pop("latexSource", None)
        return slide

    def test_import_appends_only_new_slides_and_merges_registry(self) -> None:
        before = slide_hashes(self.document)
        incoming_registry = registry()
        incoming_registry["charts"]["new-chart"] = {"title": "Result"}
        merged, merged_registry, report = merge_segment(
            self.document, self.registry,
            {**copy.deepcopy(self.document), "slides": [self.new_slide()]}, incoming_registry,
            operation="import",
        )
        self.assertEqual([slide["id"] for slide in merged["slides"]][-1], "segment-slide")
        self.assertEqual({key: slide_hashes(merged)[key] for key in before}, before)
        self.assertIn("new-chart", merged_registry["charts"])
        self.assertEqual(report["registry"]["charts"]["added"], ["new-chart"])
        self.assertEqual(report["validation"], "pass")

    def test_import_rejects_slide_and_registry_id_conflicts(self) -> None:
        with self.assertRaisesRegex(BentoConverterError, "already exist"):
            merge_segment(
                self.document, self.registry,
                {**copy.deepcopy(self.document), "slides": [copy.deepcopy(self.document["slides"][0])]},
                registry(), operation="import",
            )
        conflicting = registry()
        conflicting["equations"]["hamiltonian_split"]["latex"] = "different"
        with self.assertRaisesRegex(BentoConverterError, "conflicts"):
            merge_segment(
                self.document, self.registry,
                {**copy.deepcopy(self.document), "slides": [self.new_slide()]},
                conflicting, operation="import",
            )

    def test_replace_preserves_order_and_every_non_target_hash(self) -> None:
        target = self.document["slides"][0]["id"]
        replacement = copy.deepcopy(self.document["slides"][0])
        replacement["elements"][0]["html"] = "Explicit replacement"
        before = slide_hashes(self.document)
        merged, _, report = merge_segment(
            self.document, self.registry,
            {**copy.deepcopy(self.document), "slides": [replacement]}, registry(),
            operation="replace", slide_id=target,
        )
        self.assertEqual([slide["id"] for slide in merged["slides"]], [slide["id"] for slide in self.document["slides"]])
        after = slide_hashes(merged)
        for identifier in set(before) - {target}:
            self.assertEqual(after[identifier], before[identifier])
        self.assertNotEqual(after[target], before[target])
        self.assertEqual(report["targetSlideId"], target)

    def test_replace_requires_exact_target_and_rejects_external_dangling_element(self) -> None:
        target = self.document["slides"][0]["id"]
        old_element = self.document["slides"][0]["elements"][0]["id"]
        self.document["slides"][1]["elements"][0]["targetElementId"] = old_element
        replacement = copy.deepcopy(self.document["slides"][0])
        replacement["elements"] = replacement["elements"][1:]
        with self.assertRaisesRegex(BentoConverterError, "referenced by other slides"):
            merge_segment(
                self.document, self.registry,
                {**copy.deepcopy(self.document), "slides": [replacement]}, registry(),
                operation="replace", slide_id=target,
            )
        with self.assertRaisesRegex(BentoConverterError, "explicit slide ID"):
            merge_segment(
                self.document, self.registry,
                {**copy.deepcopy(self.document), "slides": [replacement]}, registry(),
                operation="replace",
            )


if __name__ == "__main__":
    unittest.main()
