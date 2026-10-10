from __future__ import annotations

import unittest

from bento_converter.native_compatibility import classify_native_compatibility, classify_slide_background


def item(**updates):
    value = {
        "type": "text", "tag": "div", "style": {
            "clipPath": "none", "maskImage": "none", "filter": "none", "backdropFilter": "none",
            "mixBlendMode": "normal", "writingMode": "horizontal-tb", "backgroundImage": "none",
            "display": "block", "paddingLeft": 0, "paddingRight": 0, "paddingTop": 0, "paddingBottom": 0,
            "letterSpacing": 0, "objectFit": "fill",
        },
        "transform": {"hasTransform": False, "hasSkew": False, "is3D": False},
        "pseudoElementDependent": False,
    }
    value.update(updates)
    return value


class NativeCompatibilityTests(unittest.TestCase):
    def test_flex_center_and_linear_gradient_need_native_adjustment(self) -> None:
        flex = item(style={**item()["style"], "display": "flex"})
        gradient = item(type="shape", style={**item()["style"], "backgroundImage": "linear-gradient(90deg, rgb(0,0,0), rgb(255,255,255))"})
        self.assertEqual(classify_native_compatibility(flex).classification, "native-with-adjustment")
        self.assertEqual(classify_native_compatibility(gradient).classification, "native-with-adjustment")

    def test_clip_filter_backdrop_and_skew_recommend_local_svg(self) -> None:
        for field, value in (
            ("clipPath", "polygon(0 0,100% 0,50% 100%)"),
            ("maskImage", "linear-gradient(black, transparent)"),
            ("filter", "blur(2px)"),
            ("backdropFilter", "blur(8px)"),
            ("mixBlendMode", "multiply"),
            ("writingMode", "vertical-rl"),
            ("backgroundImage", "url(one.png), linear-gradient(red, blue)"),
        ):
            candidate = item(style={**item()["style"], field: value})
            self.assertEqual(classify_native_compatibility(candidate).classification, "localized-svg-recommended")
        self.assertEqual(classify_native_compatibility(item(pseudoElementDependent=True)).classification, "localized-svg-recommended")
        self.assertEqual(classify_native_compatibility(item(transform={"hasTransform": True, "hasSkew": True, "is3D": False})).classification, "localized-svg-recommended")

    def test_simple_shadow_stays_native_safe(self) -> None:
        candidate = item(style={**item()["style"], "boxShadow": "0 4px 12px rgba(0,0,0,.2)"})
        self.assertEqual(classify_native_compatibility(candidate).classification, "native-safe")

    def test_complex_table_and_canvas_fallback(self) -> None:
        table = item(type="table", tag="table", table={"simpleTable": False, "complexityReasons": ["colspan"]})
        self.assertEqual(classify_native_compatibility(table).classification, "localized-svg-recommended")
        self.assertEqual(classify_native_compatibility(item(type="canvas", tag="canvas")).classification, "image-required")
        unsafe_svg = item(type="svg", tag="svg", outerHTML="<svg><foreignObject><canvas></canvas></foreignObject></svg>")
        self.assertEqual(classify_native_compatibility(unsafe_svg).classification, "image-required")

    def test_text_bearing_shape_and_annotated_group_remain_native_decompositions(self) -> None:
        shape = item(type="shape", text="Label")
        group = item(type="text", text="Card contents", hasExplicitDescendants=True)
        self.assertEqual(classify_native_compatibility(shape).classification, "native-with-adjustment")
        self.assertEqual(classify_native_compatibility(group).classification, "native-with-adjustment")

    def test_unsupported_text_container_requires_background_only_capture(self) -> None:
        candidate = item(
            type="shape", text="Editable", hasExplicitDescendants=True,
            style={**item()["style"], "filter": "blur(2px)"},
        )
        self.assertEqual(classify_native_compatibility(candidate).classification, "background-image-required")

    def test_unsupported_semantic_text_and_equation_keep_editable_overlays(self) -> None:
        for element_type in ("text", "equation"):
            candidate = item(
                type=element_type, text="Editable",
                style={**item()["style"], "filter": "blur(2px)"},
            )
            self.assertEqual(
                classify_native_compatibility(candidate).classification,
                "background-image-required",
            )

    def test_slide_gradient_is_background_adjustment(self) -> None:
        result = classify_slide_background({"backgroundStyle": {"backgroundImage": "linear-gradient(90deg, red, blue)"}})
        self.assertEqual(result.classification, "native-with-adjustment")

    def test_slide_image_and_complex_layers_are_distinguished(self) -> None:
        image = classify_slide_background({"backgroundStyle": {"backgroundImage": 'url("image.png")', "backgroundRepeat": "no-repeat"}})
        layers = classify_slide_background({"backgroundStyle": {"backgroundImage": "linear-gradient(red,blue),linear-gradient(white,black)", "backgroundRepeat": "no-repeat"}})
        self.assertEqual(image.classification, "native-with-adjustment")
        self.assertEqual(layers.classification, "localized-svg-recommended")


if __name__ == "__main__":
    unittest.main()
