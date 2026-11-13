"""C3-A — image payload safety gate.

Every byte string a (simulated or live) image provider returns must be
inspected BEFORE it is written to CAS or exposed as an asset:

- bounded payload bytes (provider images are not text — the 2 MiB
  live-response cap for JSON never applied here; image payloads get
  their own, larger but still bounded limit),
- recognized container format (PNG / JPEG / WebP only),
- structural decode verification + pixel-count cap (decompression
  bombs are refused before a full decode happens),
- per-dimension limits,
- SHA-256 integrity digest,
- a safely decoded, re-encoded thumbnail.

Anything that fails is an ``ImageRejected`` — the caller decides the
financial posture (a 200 OK with an unprocessable payload is an
ambiguous billing outcome → the reservation is HELD, never released).
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageFile, UnidentifiedImageError

# Hard limits — a generated image is a bounded artifact, not a stream.
MAX_IMAGE_BYTES = 16 * 1024 * 1024      # 16 MiB compressed
MAX_IMAGE_PIXELS = 64_000_000           # 64 MP — Pillow bomb threshold too
MAX_DIMENSION = 8192                    # per side
SUPPORTED_FORMATS = ("PNG", "JPEG", "WEBP")
THUMBNAIL_SIZE = (256, 256)
THUMBNAIL_FORMAT = "PNG"

# Pillow's own decompression-bomb tripwire stays engaged: ImageFile
# refuses LOAD_TRUNCATED_IMAGES by default and Image.open honors
# Image.MAX_IMAGE_PIXELS — pin it to our cap explicitly.
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


class ImageRejected(ValueError):
    """The payload failed image safety inspection — never an asset."""


@dataclass(frozen=True)
class ImageInfo:
    format: str
    width: int
    height: int
    pixels: int
    sha256: str
    byte_size: int

    def as_media(self) -> dict[str, Any]:
        return {"format": self.format, "width": self.width,
                "height": self.height, "pixels": self.pixels,
                "sha256": self.sha256, "byte_size": self.byte_size}


def inspect_image(data: bytes) -> ImageInfo:
    """Validate + describe image bytes. Raises ImageRejected.

    Header decode → format allowlist → dimension/pixel caps → full
    verify. Rejected payloads never reach CAS.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise ImageRejected("image payload is not bytes")
    if not data:
        raise ImageRejected("empty image payload")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageRejected(
            f"image payload {len(data)} bytes exceeds "
            f"{MAX_IMAGE_BYTES} cap")
    try:
        probe = Image.open(io.BytesIO(data))
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageRejected(f"unreadable image payload: {exc}") from exc
    fmt = (probe.format or "").upper()
    if fmt not in SUPPORTED_FORMATS:
        raise ImageRejected(
            f"unsupported image format {fmt or 'unknown'!r} — allowed: "
            + ", ".join(SUPPORTED_FORMATS))
    w, h = probe.size
    if w <= 0 or h <= 0:
        raise ImageRejected(f"invalid image dimensions {w}x{h}")
    if w > MAX_DIMENSION or h > MAX_DIMENSION:
        raise ImageRejected(
            f"image dimension {w}x{h} exceeds {MAX_DIMENSION} limit")
    if w * h > MAX_IMAGE_PIXELS:
        raise ImageRejected(
            f"image {w * h} px exceeds {MAX_IMAGE_PIXELS} px cap")
    try:
        # Full structural verification (header decode already read the
        # container); DecompressionBombError surfaces here if a payload
        # slips absurd declared geometry past the explicit caps.
        probe.verify()
        Image.open(io.BytesIO(data)).convert("RGB").load()
    except Exception as exc:
        raise ImageRejected(f"corrupt image payload: {exc}") from exc
    return ImageInfo(format=fmt, width=w, height=h, pixels=w * h,
                     sha256=hashlib.sha256(bytes(data)).hexdigest(),
                     byte_size=len(data))


def make_thumbnail(data: bytes, *, size: tuple[int, int] = THUMBNAIL_SIZE
                   ) -> bytes:
    """Decode→thumbnail→re-encode as PNG. The output is OUR encoding —
    metadata, polyglot tricks and odd profiles are all stripped. Input
    must already have passed inspect_image."""
    img = Image.open(io.BytesIO(data))
    img = img.convert("RGB")
    img.thumbnail(size, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format=THUMBNAIL_FORMAT)
    return buf.getvalue()
