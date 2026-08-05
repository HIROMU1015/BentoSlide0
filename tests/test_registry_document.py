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


if __name__ == "__main__":
    unittest.main()
