from __future__ import annotations

import hashlib
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_OUTPUT_BYTES = 5 * 1024 * 1024
MAX_SIDE = 8192
MAX_PIXELS = 25_000_000
OUTPUT_LONG_SIDE = 2048


class ImageRejected(ValueError):
    pass


@dataclass(frozen=True)
class NormalizedImage:
    jpeg_bytes: bytes
    sha256: str
    byte_length: int
    width: int
    height: int


def normalize_image(raw: bytes) -> NormalizedImage:
    if not raw or len(raw) > MAX_INPUT_BYTES:
        raise ImageRejected("image must be between 1 byte and 10 MiB")
    try:
        with Image.open(BytesIO(raw)) as source:
            if source.format not in {"JPEG", "PNG", "WEBP"}:
                raise ImageRejected("only JPEG, PNG, and WebP images are supported")
            width, height = source.size
            if width > MAX_SIDE or height > MAX_SIDE or width * height > MAX_PIXELS:
                raise ImageRejected("image dimensions exceed the safe limit")
            source.seek(0)
            frame = ImageOps.exif_transpose(source.copy())
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise ImageRejected("image could not be decoded safely") from error
    if frame.mode in {"RGBA", "LA"} or "transparency" in frame.info:
        rgba = frame.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        frame = background
    else:
        frame = frame.convert("RGB")
    frame.thumbnail((OUTPUT_LONG_SIDE, OUTPUT_LONG_SIDE), Image.Resampling.LANCZOS)
    output = BytesIO()
    frame.save(output, format="JPEG", quality=85, optimize=True)
    jpeg = output.getvalue()
    if len(jpeg) > MAX_OUTPUT_BYTES:
        raise ImageRejected("normalized image exceeds 5 MiB")
    return NormalizedImage(
        jpeg_bytes=jpeg,
        sha256=hashlib.sha256(jpeg).hexdigest(),
        byte_length=len(jpeg),
        width=frame.width,
        height=frame.height,
    )
