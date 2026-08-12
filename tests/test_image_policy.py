"""Routing images to OCR or to CLIP."""

from __future__ import annotations

import pytest

from backend.pipeline.image_policy import (
    GPS_EXIF_TAGS,
    ImageMetrics,
    ImageRoute,
    ImageSource,
    route_image,
    strip_sensitive_exif,
)


def photo(**kw) -> ImageMetrics:
    base = dict(
        width=4032, height=3024, fmt="heic", has_camera_exif=True,
        distinct_colours=48000, white_fraction=0.05, filename="IMG_4821.HEIC",
        source=ImageSource.IMESSAGE_ATTACHMENT,
    )
    base.update(kw)
    return ImageMetrics(**base)


def screenshot(**kw) -> ImageMetrics:
    base = dict(
        width=1179, height=2556, fmt="png", has_camera_exif=False,
        distinct_colours=900, white_fraction=0.3, filename="IMG_0042.PNG",
        source=ImageSource.IMESSAGE_ATTACHMENT,
    )
    base.update(kw)
    return ImageMetrics(**base)


# ------------------------------------------------------------------- photos

def test_a_photograph_is_embedded():
    d = route_image(photo())
    assert d.route is ImageRoute.EMBED
    assert d.needs_embedding and not d.needs_ocr
    assert "camera EXIF" in d.reasons[0]


def test_camera_exif_beats_a_screenshot_sized_photo():
    """A photo cropped to 1920x1080 is still a photo."""
    d = route_image(photo(width=1920, height=1080))
    assert d.route is ImageRoute.EMBED


def test_photographed_document_gets_both():
    """A phone photo of a letter: EXIF says photo, whiteness says document."""
    d = route_image(photo(white_fraction=0.78))
    assert d.route is ImageRoute.BOTH
    assert d.needs_ocr and d.needs_embedding


# --------------------------------------------------------------- screenshots

@pytest.mark.parametrize(
    "w,h",
    [(1179, 2556), (1290, 2796), (1125, 2436), (2560, 1600), (3456, 2234)],
)
def test_exact_device_screen_sizes_route_to_ocr(w, h):
    d = route_image(screenshot(width=w, height=h))
    assert d.route is ImageRoute.OCR
    assert d.confidence >= 0.9


def test_rotated_screenshot_dimensions_also_match():
    """Landscape screenshot — width and height swapped."""
    assert route_image(screenshot(width=2556, height=1179)).route is ImageRoute.OCR


def test_filename_naming_a_screenshot_is_honoured():
    d = route_image(
        screenshot(width=1600, height=1200, filename="Screenshot 2026-08-20.png")
    )
    assert d.route is ImageRoute.OCR
    assert "filename" in d.reasons[0]


def test_scanned_page_routes_to_ocr():
    d = route_image(
        ImageMetrics(
            width=1700, height=2200, fmt="jpeg", has_camera_exif=False,
            white_fraction=0.82, distinct_colours=6000, filename="doc.jpg",
        )
    )
    assert d.route is ImageRoute.OCR
    assert "near-white" in d.reasons[0]


def test_flat_palette_routes_to_ocr():
    """UI chrome and charts use few colours; photos never do."""
    d = route_image(
        ImageMetrics(
            width=1400, height=900, fmt="png", has_camera_exif=False,
            distinct_colours=420, white_fraction=0.2, filename="chart.png",
        )
    )
    assert d.route is ImageRoute.OCR


def test_ambiguous_png_does_both_rather_than_guessing():
    d = route_image(
        ImageMetrics(
            width=1400, height=900, fmt="png", has_camera_exif=False,
            distinct_colours=None, white_fraction=None, filename="image.png",
        )
    )
    assert d.route is ImageRoute.BOTH
    assert d.confidence <= 0.6


# --------------------------------------------------------------------- skips

@pytest.mark.parametrize("w,h", [(1, 1), (16, 16), (63, 200), (200, 40)])
def test_tiny_images_are_skipped(w, h):
    """Tracking pixels, spacers and icons."""
    d = route_image(ImageMetrics(width=w, height=h, fmt="gif"))
    assert d.route is ImageRoute.SKIP
    assert not d.needs_ocr and not d.needs_embedding


def test_inline_email_images_are_skipped():
    """Logos, banners and signature graphics would flood the index."""
    d = route_image(
        ImageMetrics(
            width=600, height=200, fmt="png", source=ImageSource.EMAIL_INLINE,
            filename="logo.png",
        )
    )
    assert d.route is ImageRoute.SKIP


def test_zero_dimension_image_is_skipped_not_crashed():
    assert route_image(ImageMetrics(width=0, height=0)).route is ImageRoute.SKIP


# ----------------------------------------------------------------- decisions

def test_every_route_has_a_reason():
    for metrics in (photo(), screenshot(), ImageMetrics(width=1, height=1)):
        assert route_image(metrics).reasons, "a routing decision must explain itself"


def test_email_attachments_are_processed():
    d = route_image(photo(source=ImageSource.EMAIL_ATTACHMENT))
    assert d.route is not ImageRoute.SKIP


# ------------------------------------------------------------------- privacy

def test_gps_tags_are_stripped_by_default():
    exif = {"Make": "Apple", "Model": "iPhone", "GPSLatitude": 48.85, "GPSLongitude": 2.35}
    cleaned = strip_sensitive_exif(exif)
    assert "GPSLatitude" not in cleaned
    assert "GPSLongitude" not in cleaned
    assert cleaned["Make"] == "Apple"


def test_gps_can_be_kept_when_opted_in():
    exif = {"GPSLatitude": 48.85, "Make": "Apple"}
    assert "GPSLatitude" in strip_sensitive_exif(exif, keep_location=True)


def test_every_gps_tag_is_covered():
    exif = {tag: "x" for tag in GPS_EXIF_TAGS} | {"Model": "iPhone"}
    cleaned = strip_sensitive_exif(exif)
    assert set(cleaned) == {"Model"}


def test_stripping_does_not_mutate_the_original():
    exif = {"GPSLatitude": 1.0, "Make": "Apple"}
    strip_sensitive_exif(exif)
    assert "GPSLatitude" in exif
