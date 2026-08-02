"""Deterministic HTML-first and legacy JSON-first Bento Slides conversion."""

from .bento_validator import validate_bento_doc, validate_bento_html, validate_conversion
from .converter import ConversionResult, convert_design
from .design_loader import load_design
from .design_validator import validate_design
from .html_document import embed_bento_doc, extract_bento_doc
from .html_pipeline import HtmlBuildResult, build_from_html

__all__ = [
    "ConversionResult",
    "HtmlBuildResult",
    "build_from_html",
    "convert_design",
    "embed_bento_doc",
    "extract_bento_doc",
    "load_design",
    "validate_bento_doc",
    "validate_bento_html",
    "validate_conversion",
    "validate_design",
]
