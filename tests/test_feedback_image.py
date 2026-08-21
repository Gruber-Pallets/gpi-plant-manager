from io import BytesIO

import pytest
from PIL import Image

from zira_dashboard.feedback_image import ImageRejected, normalize_image


def image_bytes(fmt="PNG", size=(3000, 1000), color=(10, 20, 30, 120)):
    output = BytesIO()
    Image.new("RGBA", size, color).save(output, format=fmt)
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


@pytest.mark.parametrize("raw", [b"", b"not an image", b"x" * (10 * 1024 * 1024 + 1)])
def test_normalize_image_rejects_empty_invalid_and_oversized_inputs(raw):
    with pytest.raises(ImageRejected):
        normalize_image(raw)


def test_normalize_image_rejects_excessive_dimensions(monkeypatch):
    monkeypatch.setattr("zira_dashboard.feedback_image.MAX_SIDE", 100)
    with pytest.raises(ImageRejected, match="dimensions"):
        normalize_image(image_bytes(size=(101, 10)))
