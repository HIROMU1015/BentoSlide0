from __future__ import annotations

import copy
import unittest

from bento_converter.errors import BentoConverterError
from bento_converter.registry_document import (
    REGISTRY_V2,
    normalize_registry,
    registry_revision,
    validate_registry,
)


class RegistryDocumentTests(unittest.TestCase):
    def v1(self) -> dict:
        return {
            "format": "bento/html-registry/v1",
            "document": {}, "assets": {}, "fonts": {}, "equations": {},
            "figures": {}, "tables": {}, "charts": {},
            "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
        }

    def test_v1_normalizes_to_v2_with_manifest_sources(self) -> None:
        normalized = normalize_registry(self.v1(), source_manifest={"items": [{
            "id": "results", "path": "sources/results.csv", "type": "dataset", "role": "evidence",
        }]})
        self.assertEqual(normalized["format"], REGISTRY_V2)
        self.assertEqual(normalized["unitId"], "deck")
        self.assertEqual(normalized["sources"]["results"]["path"], "sources/results.csv")
        validate_registry(normalized, allow_v1=False)

    def test_revision_is_canonical_and_unknown_format_is_rejected(self) -> None:
        value = normalize_registry(self.v1())
        reordered = {key: copy.deepcopy(value[key]) for key in reversed(list(value))}
        self.assertEqual(registry_revision(value), registry_revision(reordered))
        with self.assertRaisesRegex(BentoConverterError, "Unsupported"):
            validate_registry({"format": "future"})

    def test_v2_provenance_requires_a_registered_source(self) -> None:
        value = {
            "format": REGISTRY_V2, "unitId": "deck", "sources": {},
            "assets": {}, "fonts": {}, "equations": {}, "figures": {}, "tables": {},
            "charts": {"result": {"provenance": {"sourceId": "missing", "locator": "row 1"}}},
            "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
        }
        with self.assertRaisesRegex(BentoConverterError, "unknown sourceId"):
            validate_registry(value)
        value["sources"]["missing"] = {"path": "sources/results.csv", "type": "dataset"}
        validate_registry(value)

    def test_visual_origin_contract_distinguishes_source_and_generated(self) -> None:
        value = normalize_registry(self.v1())
        value["sources"]["paper"] = {"path": "sources/private/paper.pdf", "type": "pdf"}
        value["assets"]["original"] = {
            "path": "assets/source/original.png",
            "origin": {"kind": "source-original", "sourceId": "paper", "locator": "Fig. 3, p. 7"},
            "provenance": {"sourceId": "paper", "locator": "Fig. 3, p. 7"},
        }
        value["assets"]["derived"] = {
            "path": "assets/local/derived.svg",
            "origin": {"kind": "source-derived", "sources": [
                {"sourceId": "paper", "locator": "Sec. III and Fig. 2"},
            ]},
        }
        value["assets"]["concept"] = {
            "path": "assets/generated/concept.png", "role": "conceptual-illustration",
            "origin": {"kind": "generated"},
        }
        validate_registry(value, allow_v1=False)

        missing_locator = copy.deepcopy(value)
        del missing_locator["assets"]["original"]["origin"]["locator"]
        with self.assertRaisesRegex(BentoConverterError, "requires a non-empty locator"):
            validate_registry(missing_locator, allow_v1=False)
        generated_claim = copy.deepcopy(value)
        generated_claim["assets"]["concept"]["origin"]["sourceId"] = "paper"
        with self.assertRaisesRegex(BentoConverterError, "must not claim source provenance"):
            validate_registry(generated_claim, allow_v1=False)
        invalid_kind = copy.deepcopy(value)
        invalid_kind["assets"]["concept"]["origin"]["kind"] = "synthetic-evidence"
        with self.assertRaisesRegex(BentoConverterError, "kind must be one of"):
            validate_registry(invalid_kind, allow_v1=False)


if __name__ == "__main__":
    unittest.main()
