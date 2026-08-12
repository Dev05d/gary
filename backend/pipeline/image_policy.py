"""Deciding what to do with an image.

Two facts drive this module.

**1. Image embeddings do not read text.** CLIP encodes "a screenshot of a
messaging app", not the address in it. Screenshots and scanned documents are
*text* wearing an image's clothes, and embedding them loses the only thing that
mattered. They belong in OCR and the text index.

**2. Deciding which is which must be cheap.** Running OCR on every holiday
photo to find out it has no text is exactly the waste this avoids. So routing
uses metadata that is free or nearly free to obtain — EXIF tags, dimensions,
format, colour statistics — and never the image content itself.

The decision logic here is pure: it takes measurements and returns a route.
Extracting those measurements needs Pillow; testing the rules does not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Set, Tuple


class ImageRoute(str, Enum):
    EMBED = "embed"  # a photo — CLIP image embedding
    OCR = "ocr"  # text-dominant — read it, index the text
    BOTH = "both" # ambiguous, or valuable enough to warrant both
    SKIP = "skip"  # decorative, tiny, or not worth storing


class ImageSource(str, Enum):
    EMAIL_ATTACHMENT = "email_attachment"
    IMESSAGE_ATTACHMENT = "imessage_attachment"
    EMAIL_INLINE = "email_inline"  # not processed — see SKIP_INLINE


#: Inline email images are logos, banners, tracking pixels and signature
#: graphics almost without exception. Embedding them floods the index with
#: brand assets and returns them for every query.
SKIP_INLINE = True

#: Below this, an image is an icon, a spacer, or a tracking pixel.
MIN_DIMENSION = 64
MIN_PIXELS = 64 * 64

#: Common device screen sizes. A file matching one exactly is a screenshot with
#: near-certainty — no camera produces these.
_SCREENSHOT_SIZES: Set[Tuple[int, int]] = {
    # iPhone
    (750, 1334), (828, 1792), (1080, 1920), (1125, 2436), (1170, 2532),
    (1179, 2556), (1206, 2622), (1242, 2688), (1284, 2778), (1290, 2796),
    (1320, 2868),
    # iPad
    (1536, 2048), (1620, 2160), (1668, 2388), (1640, 2360), (2048, 2732),
    # Mac / external displays
    (1440, 900), (1680, 1050), (1920, 1080), (2560, 1440), (2560, 1600),
    (2880, 1800), (3024, 1964), (3456, 2234), (3840, 2160), (5120, 2880),
}


def _is_screenshot_size(width: int, height: int) -> bool:
    return (width, height) in _SCREENSHOT_SIZES or (height, width) in _SCREENSHOT_SIZES


@dataclass
class ImageMetrics:
    """Cheap measurements taken before any expensive processing."""

    width: int = 0
    height: int = 0
    fmt: str = ""  # "png" | "jpeg" | "heic" | "gif" | ...
    source: ImageSource = ImageSource.EMAIL_ATTACHMENT
    filename: str = ""

    #: Camera EXIF (Make/Model/FNumber/ExposureTime). Present on photographs,
    #: absent on screenshots and rendered documents.
    has_camera_exif: bool = False

    #: Distinct colours, sampled. Screenshots and documents use flat UI palettes;
    #: photographs are continuous-tone and run into the tens of thousands.
    distinct_colours: Optional[int] = None

    #: Fraction of pixels that are near-white. Scanned pages and documents are
    #: mostly paper.
    white_fraction: Optional[float] = None

    is_animated: bool = False
    bytes_size: int = 0

    @property
    def pixels(self) -> int:
        return max(0, self.width) * max(0, self.height)

    @property
    def aspect(self) -> float:
        return (self.width / self.height) if self.height else 0.0


@dataclass
class RoutingDecision:
    route: ImageRoute
    confidence: float
    reasons: List[str] = field(default_factory=list)

    @property
    def needs_ocr(self) -> bool:
        return self.route in (ImageRoute.OCR, ImageRoute.BOTH)

    @property
    def needs_embedding(self) -> bool:
        return self.route in (ImageRoute.EMBED, ImageRoute.BOTH)


#: Distinct-colour count below which an image is UI or a document rather than a
#: photograph. Continuous-tone photos are far above this even after sampling.
FLAT_PALETTE_THRESHOLD = 3000

#: Near-white coverage above which an image is very likely a scanned page.
DOCUMENT_WHITE_FRACTION = 0.55


def route_image(m: ImageMetrics) -> RoutingDecision:
    """Decide how to process one image."""
    reasons: List[str] = []

    if m.source is ImageSource.EMAIL_INLINE and SKIP_INLINE:
        return RoutingDecision(
            ImageRoute.SKIP, 1.0, ["inline email image — logo/banner/tracker"]
        )

    if m.width < MIN_DIMENSION or m.height < MIN_DIMENSION or m.pixels < MIN_PIXELS:
        return RoutingDecision(
            ImageRoute.SKIP,
            1.0,
            [f"too small ({m.width}x{m.height}) — icon, spacer or tracking pixel"],
        )

    # Camera EXIF is the strongest single signal, and it is definitive in one
    # direction only: a phone screenshot never carries an aperture value.
    if m.has_camera_exif:
        reasons.append("camera EXIF present — a photograph")
        # A photographed document is still mostly paper, and OCR beats CLIP on it.
        if m.white_fraction is not None and m.white_fraction >= DOCUMENT_WHITE_FRACTION:
            return RoutingDecision(
                ImageRoute.BOTH,
                0.7,
                reasons + [f"but {m.white_fraction:.0%} near-white — a photographed document"],
            )
        return RoutingDecision(ImageRoute.EMBED, 0.95, reasons)

    if _is_screenshot_size(m.width, m.height):
        return RoutingDecision(
            ImageRoute.OCR,
            0.95,
            [f"{m.width}x{m.height} is an exact device screen size — a screenshot"],
        )

    name = m.filename.lower()
    if any(token in name for token in ("screenshot", "screen shot", "capture", "scan")):
        return RoutingDecision(
            ImageRoute.OCR, 0.85, [f"filename says so: {m.filename!r}"]
        )

    if m.white_fraction is not None and m.white_fraction >= DOCUMENT_WHITE_FRACTION:
        return RoutingDecision(
            ImageRoute.OCR,
            0.8,
            [f"{m.white_fraction:.0%} near-white — a document or scanned page"],
        )

    if m.distinct_colours is not None and m.distinct_colours < FLAT_PALETTE_THRESHOLD:
        return RoutingDecision(
            ImageRoute.OCR,
            0.75,
            [f"only ~{m.distinct_colours} distinct colours — a flat UI palette, not a photo"],
        )

    # PNG is weak evidence of a rendered image rather than a camera capture,
    # but on its own it is not enough to skip embedding.
    if m.fmt.lower() == "png" and m.distinct_colours is None:
        return RoutingDecision(
            ImageRoute.BOTH,
            0.5,
            ["PNG with no colour statistics — could be either, so do both"],
        )

    return RoutingDecision(
        ImageRoute.EMBED, 0.7, reasons + ["no text-heavy indicators — treat as a photo"]
    )


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------

#: EXIF tags that leak location. Photos routinely carry precise GPS
#: coordinates, and a personal archive of them is a movement history.
GPS_EXIF_TAGS = {
    "GPSLatitude", "GPSLongitude", "GPSAltitude", "GPSTimeStamp",
    "GPSDateStamp", "GPSProcessingMethod", "GPSAreaInformation",
}


def strip_sensitive_exif(exif: dict, *, keep_location: bool = False) -> dict:
    """Remove location tags unless the user opted in.

    Kept off by default. Location makes "photos from Paris" possible, but it
    also turns the archive into a record of where the user has been — a much
    larger disclosure than the photos themselves, and not one anybody expects
    from an email client.
    """
    if keep_location:
        return dict(exif)
    return {k: v for k, v in exif.items() if k not in GPS_EXIF_TAGS}
