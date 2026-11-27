import hashlib
import io
from dataclasses import dataclass

import pjsk_background_gen_PIL as pjsk_bg
from PIL import Image, UnidentifiedImageError

from app.errors import ApiError, bad_request, too_large
from app.services import conversion

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def sha1(data: bytes) -> str:
    """Hash file contents the way stored files are named.

    Args:
        data: File contents.

    Returns:
        str: SHA-1 hex digest.
    """
    return hashlib.sha1(data).hexdigest()


def is_supported_image(data: bytes) -> bool:
    """Check image magic bytes accepted for jackets.

    Args:
        data: File contents.

    Returns:
        bool: Whether the file is PNG, JPEG, JPEG 2000, AVIF, ICO or ICNS.
    """
    head = data[:20]
    return (
        head[:8] == PNG_MAGIC
        or head[:3] == b"\xff\xd8\xff"
        or head[:8] == b"\x00\x00\x00\x0c\x6a\x50\x20\x20"
        or head[:2] == b"\xff\x4f"
        or head[4:12] in (b"ftypavif", b"ftypavis")
        or head[:4] == b"\x00\x00\x01\x00"
        or head[:4] == b"icns"
    )


def is_mp3(data: bytes) -> bool:
    """Check MP3 magic bytes.

    Args:
        data: File contents.

    Returns:
        bool: Whether the file starts with an ID3 tag or an MPEG frame header.
    """
    return data.startswith(b"ID3") or data[:2] == b"\xff\xfb"


def is_png(data: bytes) -> bool:
    """Check PNG magic bytes.

    Args:
        data: File contents.

    Returns:
        bool: Whether the file is a PNG.
    """
    return data[:8] == PNG_MAGIC


def inflate_limited(data: bytes, limit: int) -> bytes:
    """Decompress gzip or zlib data without ever exceeding a size limit.

    Args:
        data: Compressed data.
        limit: Largest decompressed size accepted.

    Returns:
        bytes: Decompressed data.
    """
    try:
        return conversion.inflate_limited(data, limit)
    except conversion.ConversionError as error:
        raise ApiError(error.status, error.code, error.params) from None


def check_image_size(data: bytes, max_pixels: int) -> Image.Image:
    """Open an image after checking its dimensions, before decoding pixels.

    Args:
        data: Image file.
        max_pixels: Largest accepted width times height.

    Returns:
        Image.Image: The opened, not yet decoded image.
    """
    try:
        image = Image.open(io.BytesIO(data))
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise bad_request("error.file.invalid_image") from None
    width, height = image.size
    if width * height > max_pixels:
        raise too_large("error.file.image_too_large", max_pixels=max_pixels)
    return image


@dataclass
class JacketImages:
    """Processed jacket images.

    Attributes:
        jacket: Resized jacket PNG.
        background_v1: Generated v1 background PNG.
        background_v3: Generated v3 background PNG.
    """

    jacket: bytes
    background_v1: bytes
    background_v3: bytes


def _png_bytes(image: Image.Image) -> bytes:
    """Encode an image as PNG.

    Args:
        image: Image to encode.

    Returns:
        bytes: PNG data.
    """
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def process_jacket(data: bytes, size: int, max_pixels: int) -> JacketImages:
    """Resize a jacket and render its generated backgrounds.

    Args:
        data: Uploaded jacket image.
        size: Side length of the stored jacket.
        max_pixels: Largest accepted width times height.

    Returns:
        JacketImages: The processed images.
    """
    image = check_image_size(data, max_pixels)
    try:
        jacket = image.resize((size, size)).convert("RGBA")
    except (OSError, ValueError, Image.DecompressionBombError):
        raise bad_request("error.file.invalid_image") from None
    return JacketImages(
        jacket=_png_bytes(jacket),
        background_v1=_png_bytes(pjsk_bg.render_v1(jacket)),
        background_v3=_png_bytes(pjsk_bg.render_v3(jacket)),
    )


@dataclass
class ProfileImage:
    """Processed avatar or banner.

    Attributes:
        png: PNG data.
        webp: WEBP data.
        file_hash: SHA-256 hex of the PNG, used as its name.
    """

    png: bytes
    webp: bytes
    file_hash: str


def process_profile_image(data: bytes, size: tuple[int, int], max_pixels: int) -> ProfileImage:
    """Resize an avatar or banner to PNG and WEBP.

    Args:
        data: Uploaded image.
        size: Target width and height.
        max_pixels: Largest accepted width times height.

    Returns:
        ProfileImage: The processed image.
    """
    image = check_image_size(data, max_pixels)
    try:
        resized = image.convert("RGB").resize(size, Image.Resampling.LANCZOS)
    except (OSError, ValueError, Image.DecompressionBombError):
        raise bad_request("error.file.invalid_image") from None
    png = _png_bytes(resized)
    webp_buffer = io.BytesIO()
    resized.save(webp_buffer, format="WEBP")
    return ProfileImage(
        png=png, webp=webp_buffer.getvalue(), file_hash=hashlib.sha256(png).hexdigest()
    )
