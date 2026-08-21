import hashlib
from io import BytesIO

import pytest
from PIL import Image

from zira_dashboard.feedback_image import ImageRejected, normalize_image


def image_bytes(fmt="PNG", size=(3000, 1000), color=(10, 20, 30, 120)):
    output = BytesIO()
    image = Image.new("RGBA", size, color)
    if fmt in {"JPEG", "BMP"}:
        image = image.convert("RGB")
    image.save(output, format=fmt)
    return output.getvalue()


def exif_oriented_jpeg_bytes():
    output = BytesIO()
    exif = Image.Exif()
    exif[274] = 6
    exif[315] = "Hidden camera name"
    Image.new("RGB", (9, 5), "blue").save(
        output,
        format="JPEG",
        quality=100,
        exif=exif,
    )
    return output.getvalue()


def test_normalize_image_strips_metadata_resizes_and_hashes_jpeg():
    normalized = normalize_image(image_bytes())
    reopened = Image.open(BytesIO(normalized.jpeg_bytes))
    assert reopened.format == "JPEG"
    assert reopened.mode == "RGB"
    assert reopened.size == (2048, 683)
    assert reopened.getexif() == {}
    assert normalized.byte_length == len(normalized.jpeg_bytes)
    assert len(normalized.sha256) == 64


def test_normalize_image_reports_exact_digest_length_and_dimensions():
    normalized = normalize_image(image_bytes(size=(37, 19)))
    reopened = Image.open(BytesIO(normalized.jpeg_bytes))

    assert normalized.sha256 == hashlib.sha256(normalized.jpeg_bytes).hexdigest()
    assert normalized.byte_length == len(normalized.jpeg_bytes)
    assert (normalized.width, normalized.height) == reopened.size == (37, 19)


def test_normalize_image_applies_exif_orientation_and_strips_metadata():
    raw = exif_oriented_jpeg_bytes()
    source = Image.open(BytesIO(raw))
    assert source.size == (9, 5)
    assert source.getexif()[274] == 6
    assert source.getexif()[315] == "Hidden camera name"

    normalized = normalize_image(raw)
    reopened = Image.open(BytesIO(normalized.jpeg_bytes))

    assert reopened.size == (5, 9)
    assert (normalized.width, normalized.height) == (5, 9)
    assert reopened.getexif() == {}


def test_normalize_image_flattens_transparent_pixels_to_white_rgb():
    normalized = normalize_image(image_bytes(size=(24, 24), color=(10, 20, 30, 0)))
    reopened = Image.open(BytesIO(normalized.jpeg_bytes))

    assert reopened.mode == "RGB"
    assert reopened.getpixel((12, 12)) == pytest.approx((255, 255, 255), abs=4)


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
def test_normalize_image_accepts_supported_input_formats(fmt):
    normalized = normalize_image(image_bytes(fmt=fmt, size=(16, 8), color=(1, 2, 3, 255)))

    assert Image.open(BytesIO(normalized.jpeg_bytes)).format == "JPEG"


def test_normalize_image_rejects_decodable_unsupported_format():
    with pytest.raises(ImageRejected, match="only JPEG, PNG, and WebP"):
        normalize_image(image_bytes(fmt="BMP", size=(12, 6)))


@pytest.mark.parametrize("raw", [b"", b"not an image", b"x" * (10 * 1024 * 1024 + 1)])
def test_normalize_image_rejects_empty_invalid_and_oversized_inputs(raw):
    with pytest.raises(ImageRejected):
        normalize_image(raw)


def test_normalize_image_rejects_excessive_dimensions(monkeypatch):
    monkeypatch.setattr("zira_dashboard.feedback_image.MAX_SIDE", 100)
    with pytest.raises(ImageRejected, match="dimensions"):
        normalize_image(image_bytes(size=(101, 10)))


def test_normalize_image_enforces_pixel_limit_independently_of_side_limit(monkeypatch):
    monkeypatch.setattr("zira_dashboard.feedback_image.MAX_PIXELS", 100)

    with pytest.raises(ImageRejected, match="dimensions"):
        normalize_image(image_bytes(size=(11, 10)))


def test_normalize_image_rejects_output_over_normalized_size_limit(monkeypatch):
    monkeypatch.setattr("zira_dashboard.feedback_image.MAX_OUTPUT_BYTES", 1)

    with pytest.raises(ImageRejected, match="normalized image exceeds 5 MiB"):
        normalize_image(image_bytes(size=(16, 8)))


def test_normalize_image_rejects_truncated_decode():
    raw = image_bytes(size=(32, 16))

    with pytest.raises(ImageRejected, match="decoded safely"):
        normalize_image(raw[: len(raw) // 2])
