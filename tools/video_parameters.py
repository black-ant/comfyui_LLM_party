"""Resolve video sizing parameters on the Start Workflow node.

The node owns duration/fps/resolution/aspect_ratio/size/width/height inputs and
computes the derived width/height/frame_count outputs itself, so workflows can
wire those outputs straight into video nodes without backend calculations.
"""

import math
import re
from typing import Optional, Tuple

DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 640
DEFAULT_DURATION = 5.0
DEFAULT_FPS = 16.0
# Video models such as MiniMax H3 and Wan require dimensions that are
# multiples of 32 (MiniMax) / 16 (Wan); 32 satisfies both.
DIMENSION_STEP = 32

_RESOLUTION_SHORT_EDGES = {
    "sd": 480,
    "480p": 480,
    "hd": 720,
    "720p": 720,
    "fhd": 1080,
    "1080p": 1080,
    "2k": 1440,
    "1440p": 1440,
    "4k": 2160,
    "2160p": 2160,
}


def resolve_video_dimensions(
    width,
    height,
    resolution="",
    size="",
    aspect_ratio="",
) -> Tuple[int, int]:
    """Return the width/height that the Start Workflow node should emit.

    Explicit ``size``/``resolution`` dimensions win. Otherwise ``resolution``
    (short edge or megapixels) and ``aspect_ratio`` are combined. When none of
    those inputs are set the widget values are kept unchanged.
    """
    width = _positive_int(width) or DEFAULT_WIDTH
    height = _positive_int(height) or DEFAULT_HEIGHT

    for candidate in (size, resolution):
        dimensions = _parse_dimensions(candidate)
        if dimensions:
            return _align_dimension(dimensions[0]), _align_dimension(dimensions[1])

    ratio = _parse_aspect_ratio(aspect_ratio)
    short_edge = _parse_short_edge(resolution) or _parse_short_edge(size)
    megapixels = _parse_megapixels(resolution) or _parse_megapixels(size)

    if ratio is None and short_edge is None and megapixels is None:
        return width, height

    if ratio is None:
        ratio = width / height if height else 1.0

    if short_edge is None and megapixels is not None:
        total_pixels = megapixels * 1_000_000
        return (
            _align_dimension(math.sqrt(total_pixels * ratio)),
            _align_dimension(math.sqrt(total_pixels / ratio)),
        )

    if short_edge is None:
        short_edge = min(width, height)

    if ratio >= 1.0:
        height = short_edge
        width = height * ratio
    else:
        width = short_edge
        height = width / ratio
    return _align_dimension(width), _align_dimension(height)


def resolve_frame_count(duration, fps) -> int:
    """Return the frame count emitted by the Start Workflow node."""
    try:
        duration_value = max(0.0, float(duration))
    except (TypeError, ValueError):
        duration_value = DEFAULT_DURATION
    try:
        fps_value = max(1.0, float(fps))
    except (TypeError, ValueError):
        fps_value = DEFAULT_FPS
    return max(1, int(round(duration_value * fps_value)) + 1)


def _parse_dimensions(value) -> Optional[Tuple[int, int]]:
    if value is None:
        return None
    text = str(value).strip().lower().replace("×", "x")
    match = re.fullmatch(r"(\d+)\s*x\s*(\d+)", text)
    if not match:
        return None
    width = int(match.group(1))
    height = int(match.group(2))
    if width <= 0 or height <= 0:
        return None
    return width, height


def _parse_aspect_ratio(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        ratio = float(value)
        return ratio if ratio > 0 else None
    text = str(value).strip().lower().replace("／", "/")
    text = re.split(r"\s*\(", text, maxsplit=1)[0].strip()
    if not text:
        return None
    if ":" in text:
        left, right = text.split(":", 1)
    elif "/" in text:
        left, right = text.split("/", 1)
    else:
        try:
            ratio = float(text)
        except ValueError:
            return None
        return ratio if ratio > 0 else None
    try:
        left_value = float(left.strip())
        right_value = float(right.strip())
    except ValueError:
        return None
    if left_value <= 0 or right_value <= 0:
        return None
    return left_value / right_value


def _parse_short_edge(value) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in _RESOLUTION_SHORT_EDGES:
        return _RESOLUTION_SHORT_EDGES[text]
    match = re.fullmatch(r"(\d+)p", text)
    if match:
        edge = int(match.group(1))
        return edge if edge > 0 else None
    if re.fullmatch(r"\d+", text):
        edge = int(text)
        # Pure numbers above 100 are treated as pixel edges; smaller numbers
        # such as 0.9 are handled as megapixels instead.
        if 100 < edge <= 8192:
            return edge
    return None


def _parse_megapixels(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if 0 < parsed <= 100:
        return parsed
    return None


def _positive_int(value) -> Optional[int]:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _align_dimension(value: float) -> int:
    return max(DIMENSION_STEP, int(math.ceil(value / DIMENSION_STEP)) * DIMENSION_STEP)
