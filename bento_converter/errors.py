"""Error and validation result types used by the converter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class ValidationReport:
    warnings: tuple[str, ...] = ()


class BentoConverterError(Exception):
    """Base class for expected user-facing failures."""


class JsonLoadError(BentoConverterError):
    """The design or Bento JSON could not be loaded."""


class HtmlDocumentError(BentoConverterError):
    """The Bento document block is missing, duplicated, or malformed."""


class ConversionError(BentoConverterError):
    """A validated design cannot be represented by the supported converter."""


class BrowserCheckError(BentoConverterError):
    """The generated deck failed a browser-level check."""


class ValidationError(BentoConverterError):
    label = "Validation failed"

    def __init__(self, issues: Iterable[str]):
        self.issues = tuple(issues)
        super().__init__(self.__str__())

    def __str__(self) -> str:
        details = "\n".join(f"- {issue}" for issue in self.issues)
        return f"{self.label}:\n{details}"


class DesignValidationError(ValidationError):
    label = "GPT design validation failed"


class BentoValidationError(ValidationError):
    label = "Bento document validation failed"


def issue(
    *,
    slide_id: object = "<document>",
    element_id: object = "<none>",
    field: str,
    actual: object,
    fix: str,
) -> str:
    """Create a contextual error/warning message with a prescribed fix."""

    return (
        f"slideId={slide_id!r}; elementId={element_id!r}; field={field}; "
        f"actual={actual!r}; fix={fix}"
    )
