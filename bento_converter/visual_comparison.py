"""Perceptual screenshot comparison using Pillow only."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

try:
    from PIL import Image, ImageChops, ImageFilter, ImageStat
except ImportError as exc:  # pragma: no cover - exercised by CLI dependency failures
    raise RuntimeError("Pillow is required for screenshot comparison. Install requirements-browser.txt.") from exc

ANALYSIS_SIZE = (256, 144)
HASH_SIZE = 8
HASH_SOURCE_SIZE = 32
CROP_PADDING = 8
MIN_CROP_DIMENSION = 24

# Calibrated against tests/fixtures/html_first: native typography/table/chart
# rendering remains warning-level, while missing backgrounds or major blocks fail.
WARNING_THRESHOLDS = {
    "perceptualHashDistance": 10,
    "normalizedPixelDifference": 0.075,
    "colorDistributionDifference": 0.10,
    "edgeDifference": 0.065,
}
FAIL_THRESHOLDS = {
    "normalizedPixelDifference": 0.30,
    "colorDistributionDifference": 0.35,
    "edgeDifference": 0.25,
}
CROP_FAIL_THRESHOLDS = {
    "normalizedPixelDifference": 0.45,
    "colorDistributionDifference": 0.75,
    "edgeDifference": 0.35,
}


def _data(image: Image.Image):
    getter = getattr(image, "get_flattened_data", None)
    return getter() if getter else image.getdata()


def _normalized(image: Image.Image) -> Image.Image:
    return image.convert("RGB").resize(ANALYSIS_SIZE, Image.Resampling.LANCZOS)


def _mean_abs_difference(first: Image.Image, second: Image.Image) -> float:
    difference = ImageChops.difference(first, second)
    return round(sum(ImageStat.Stat(difference).mean) / (3 * 255), 6)


def _color_distribution_difference(first: Image.Image, second: Image.Image) -> float:
    # Eight bins per RGB channel are enough to detect missing/changed large areas
    # without making anti-aliasing and font rasterization dominate the result.
    def histogram(image: Image.Image) -> list[float]:
        counts = [0] * 512
        for red, green, blue in _data(image.resize((128, 72), Image.Resampling.BILINEAR)):
            counts[(red >> 5) * 64 + (green >> 5) * 8 + (blue >> 5)] += 1
        total = sum(counts) or 1
        return [count / total for count in counts]

    left, right = histogram(first), histogram(second)
    return round(sum(abs(a - b) for a, b in zip(left, right)) / 2, 6)


def _edge_difference(first: Image.Image, second: Image.Image) -> float:
    left = first.convert("L").filter(ImageFilter.FIND_EDGES)
    right = second.convert("L").filter(ImageFilter.FIND_EDGES)
    return round(ImageStat.Stat(ImageChops.difference(left, right)).mean[0] / 255, 6)


def _phash(image: Image.Image) -> int:
    pixels = list(_data(image.convert("L").resize((HASH_SOURCE_SIZE, HASH_SOURCE_SIZE), Image.Resampling.LANCZOS)))
    cosines = [
        [math.cos(math.pi * (2 * position + 1) * frequency / (2 * HASH_SOURCE_SIZE)) for position in range(HASH_SOURCE_SIZE)]
        for frequency in range(HASH_SIZE)
    ]
    coefficients: list[float] = []
    for vertical in range(HASH_SIZE):
        for horizontal in range(HASH_SIZE):
            value = 0.0
            for y in range(HASH_SOURCE_SIZE):
                row = y * HASH_SOURCE_SIZE
                vertical_factor = cosines[vertical][y]
                for x in range(HASH_SOURCE_SIZE):
                    value += pixels[row + x] * cosines[horizontal][x] * vertical_factor
            coefficients.append(value)
    median_source = sorted(coefficients[1:])
    median = median_source[len(median_source) // 2]
    result = 0
    for index, value in enumerate(coefficients):
        if value > median:
            result |= 1 << index
    return result


def _ssim_like(first: Image.Image, second: Image.Image) -> float:
    left = list(_data(first.convert("L").resize((128, 72), Image.Resampling.BILINEAR)))
    right = list(_data(second.convert("L").resize((128, 72), Image.Resampling.BILINEAR)))
    count = len(left) or 1
    mean_left, mean_right = sum(left) / count, sum(right) / count
    variance_left = sum((value - mean_left) ** 2 for value in left) / count
    variance_right = sum((value - mean_right) ** 2 for value in right) / count
    covariance = sum((a - mean_left) * (b - mean_right) for a, b in zip(left, right)) / count
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    value = ((2 * mean_left * mean_right + c1) * (2 * covariance + c2)) / (
        (mean_left**2 + mean_right**2 + c1) * (variance_left + variance_right + c2)
    )
    return round(max(-1.0, min(1.0, value)), 6)


def classify_metrics(metrics: dict[str, float | int]) -> tuple[str, list[str]]:
    pixel = float(metrics["normalizedPixelDifference"])
    colors = float(metrics["colorDistributionDifference"])
    edges = float(metrics["edgeDifference"])
    phash = int(metrics["perceptualHashDistance"])
    fail_reasons: list[str] = []
    if pixel >= FAIL_THRESHOLDS["normalizedPixelDifference"]:
        fail_reasons.append("large normalized pixel difference")
    if colors >= FAIL_THRESHOLDS["colorDistributionDifference"]:
        fail_reasons.append("large primary-color distribution difference")
    if edges >= FAIL_THRESHOLDS["edgeDifference"]:
        fail_reasons.append("large edge/structure difference")
    if pixel >= 0.16 and (edges >= 0.12 or phash >= 20):
        fail_reasons.append("combined placement/structure difference")
    if fail_reasons:
        return "fail", fail_reasons
    warnings = [
        label
        for field, label in (
            ("perceptualHashDistance", "perceptual structure differs"),
            ("normalizedPixelDifference", "pixel appearance differs"),
            ("colorDistributionDifference", "primary-color distribution differs"),
            ("edgeDifference", "edge structure differs"),
        )
        if float(metrics[field]) >= WARNING_THRESHOLDS[field]
    ]
    return ("warning", warnings) if warnings else ("pass", [])


def classify_crop_metrics(metrics: dict[str, float | int]) -> tuple[str, list[str]]:
    """Classify localized crops without over-penalizing renderer-specific detail."""
    pixel = float(metrics["normalizedPixelDifference"])
    colors = float(metrics["colorDistributionDifference"])
    edges = float(metrics["edgeDifference"])
    phash = int(metrics["perceptualHashDistance"])
    fail_reasons: list[str] = []
    if pixel >= CROP_FAIL_THRESHOLDS["normalizedPixelDifference"]:
        fail_reasons.append("large localized pixel difference")
    if colors >= CROP_FAIL_THRESHOLDS["colorDistributionDifference"]:
        fail_reasons.append("large localized color-distribution difference")
    if edges >= CROP_FAIL_THRESHOLDS["edgeDifference"]:
        fail_reasons.append("large localized edge/structure difference")
    if pixel >= 0.30 and (edges >= 0.16 or phash >= 32):
        fail_reasons.append("combined localized placement/structure difference")
    if fail_reasons:
        return "fail", fail_reasons
    whole_status, whole_warnings = classify_metrics(metrics)
    return (whole_status, whole_warnings) if whole_status != "fail" else (
        "warning", ["localized renderer difference exceeds whole-slide thresholds"]
    )


def compare_images(source: str | Path | Image.Image, bento: str | Path | Image.Image) -> dict[str, Any]:
    def open_image(value: str | Path | Image.Image) -> Image.Image:
        if isinstance(value, Image.Image):
            return value.copy()
        with Image.open(value) as image:
            return image.copy()

    first, second = _normalized(open_image(source)), _normalized(open_image(bento))
    metrics: dict[str, float | int] = {
        "perceptualHashDistance": (_phash(first) ^ _phash(second)).bit_count(),
        "normalizedPixelDifference": _mean_abs_difference(first, second),
        "colorDistributionDifference": _color_distribution_difference(first, second),
        "edgeDifference": _edge_difference(first, second),
        "ssimLike": _ssim_like(first, second),
    }
    status, warnings = classify_metrics(metrics)
    return {"status": status, **metrics, "warnings": warnings}


def compare_crops(
    source_path: str | Path,
    bento_path: str | Path,
    source_frame: dict[str, float],
    bento_frame: dict[str, float],
) -> dict[str, Any] | None:
    def box(frame: dict[str, float]) -> tuple[int, int, int, int] | None:
        center_x = float(frame["x"]) + float(frame["w"]) / 2
        center_y = float(frame["y"]) + float(frame["h"]) / 2
        width = max(float(frame["w"]) + CROP_PADDING * 2, MIN_CROP_DIMENSION)
        height = max(float(frame["h"]) + CROP_PADDING * 2, MIN_CROP_DIMENSION)
        left = max(0, int(round(center_x - width / 2)))
        top = max(0, int(round(center_y - height / 2)))
        right = min(1280, int(round(center_x + width / 2)))
        bottom = min(720, int(round(center_y + height / 2)))
        return (left, top, right, bottom) if right > left and bottom > top else None

    source_box, bento_box = box(source_frame), box(bento_frame)
    if source_box is None or bento_box is None:
        return None
    with Image.open(source_path) as source_image, Image.open(bento_path) as bento_image:
        comparison = compare_images(source_image.crop(source_box), bento_image.crop(bento_box))
        status, warnings = classify_crop_metrics(comparison)
        comparison.update({"status": status, "warnings": warnings})
        return comparison
