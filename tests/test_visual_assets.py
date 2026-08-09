from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from bento_converter.errors import BentoConverterError
from bento_converter.registry_document import content_digest, load_registry, validate_registry
from bento_converter.visual_assets import SourceReference, extract_pdf_figure, register_visual_asset
from bento_converter.visual_planning import validate_visual_plan


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class VisualAssetTests(unittest.TestCase):
    def repository(self, root: Path) -> Path:
        (root / "deck").mkdir()
        (root / "sources/private").mkdir(parents=True)
        registry = {
            "format": "bento/html-registry/v2", "unitId": "deck",
            "sources": {"paper": {"path": "sources/private/paper.pdf", "type": "pdf"}},
            "document": {}, "assets": {}, "fonts": {}, "equations": {}, "figures": {},
            "tables": {}, "charts": {},
            "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
        }
        (root / "deck/deck.registry.json").write_text(json.dumps(registry), encoding="utf-8")
        return root / "deck/deck.registry.json"

    def test_registers_source_and_generated_assets_in_separate_transactional_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry_path = self.repository(root)
            image = root / "input.png"
            image.write_bytes(PNG)
            source_result = register_visual_asset(
                repository=root, registry_path=registry_path, input_path=image,
                asset_id="paper-fig-3", kind="source-original", role="source-figure",
                source_references=[SourceReference("paper", "Fig. 3, p. 7")], caption="Method overview",
            )
            generated_result = register_visual_asset(
                repository=root, registry_path=registry_path, input_path=image,
                asset_id="concept", kind="generated", role="conceptual-illustration",
                description="Intuition only", generator={"name": "external-image-generator"},
            )
            registry = load_registry(registry_path)
            validate_registry(registry, allow_v1=False)
            self.assertEqual(source_result["path"], "deck/assets/source/paper-fig-3.png")
            self.assertEqual(generated_result["path"], "deck/assets/generated/concept.png")
            self.assertEqual(registry["assets"]["paper-fig-3"]["origin"]["locator"], "Fig. 3, p. 7")
            self.assertEqual(registry["assets"]["concept"]["origin"], {"kind": "generated"})
            self.assertEqual(registry["assets"]["paper-fig-3"]["contentDigest"], content_digest(PNG))
            self.assertEqual(source_result["contentDigest"], content_digest(PNG))
            self.assertNotIn("provenance", registry["assets"]["concept"])
            self.assertEqual((root / source_result["path"]).read_bytes(), PNG)

    def test_generated_asset_cannot_claim_source_or_quantitative_role(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry_path = self.repository(root)
            image = root / "input.png"
            image.write_bytes(PNG)
            with self.assertRaisesRegex(BentoConverterError, "must not claim source"):
                register_visual_asset(
                    repository=root, registry_path=registry_path, input_path=image,
                    asset_id="fake", kind="generated", role="concept",
                    source_references=[SourceReference("paper", "Fig. 1")],
                )
            with self.assertRaisesRegex(BentoConverterError, "cannot have role"):
                register_visual_asset(
                    repository=root, registry_path=registry_path, input_path=image,
                    asset_id="fake", kind="generated", role="benchmark",
                )

    def test_pdf_crop_records_page_figure_caption_and_region(self) -> None:
        import pymupdf

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry_path = self.repository(root)
            pdf_path = root / "sources/private/paper.pdf"
            document = pymupdf.open()
            page = document.new_page(width=200, height=200)
            page.draw_rect(pymupdf.Rect(20, 20, 180, 180), color=(0, 0, 1), fill=(0.8, 0.9, 1))
            document.save(pdf_path)
            document.close()
            result = extract_pdf_figure(
                repository=root, registry_path=registry_path, source_id="paper", page=1,
                crop=(20, 20, 180, 180), dpi=72, asset_id="paper-fig-1",
                locator="Fig. 1, p. 1", figure_number="Fig. 1", caption="Overview",
            )
            registry = load_registry(registry_path)
            extraction = registry["assets"]["paper-fig-1"]["origin"]["extraction"]
            self.assertEqual(extraction["page"], 1)
            self.assertEqual(extraction["figureNumber"], "Fig. 1")
            self.assertEqual(extraction["caption"], "Overview")
            self.assertTrue((root / result["path"]).read_bytes().startswith(b"\x89PNG"))
            self.assertEqual(result["libraryPath"], "images/extracted/paper-fig-1.png")
            self.assertEqual(
                (root / result["libraryPath"]).read_bytes(),
                (root / result["path"]).read_bytes(),
            )

    def test_visual_plan_contract(self) -> None:
        validate_visual_plan({
            "schemaVersion": 1,
            "slides": [{
                "id": "method-overview", "purpose": "Explain algorithm structure",
                "visual": {
                    "recommended": True, "type": "native-diagram",
                    "intent": "Show deterministic and randomized parts", "originKind": "source-derived",
                },
            }],
        })
        with self.assertRaisesRegex(BentoConverterError, "must use generated origin"):
            validate_visual_plan({
                "schemaVersion": 1,
                "slides": [{
                    "id": "cover", "purpose": "Orient the audience",
                    "visual": {"recommended": True, "type": "generated-image", "intent": "Metaphor", "originKind": "source-derived"},
                }],
            })


if __name__ == "__main__":
    unittest.main()
