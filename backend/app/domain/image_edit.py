"""C3-D3 — edit-mask and outpaint conventions (documented contract).

MASK CONVENTION
- Format: any payload accepted by ``image_safety.inspect_image`` —
  PNG/JPEG/WebP, ≤16 MiB, ≤8192 px per side, ≤64 MP.
- Dimensions: the mask MUST equal the source image's pixel dimensions
  exactly. Masks are never silently resized or cropped: a resized mask
  would change the intended edit region, so a mismatch is an error, not
  a transform.
- Editable region: pixels whose luminance ≥ ``MASK_EDIT_THRESHOLD``
  (128) are editable; darker pixels are protected. The payload is
  flattened to luminance — any alpha channel is ignored (an alpha=0
  white pixel is still editable; transparency carries no semantics).
- At least one editable pixel is required — an all-protected mask is a
  malformed request, not a no-op.

OUTPAINT CONVENTION
- ``out_width``/``out_height`` are explicit target canvas dimensions.
  Each must be ≥ the source dimension and at least one must be strictly
  greater; both are capped at ``MAX_OUT_DIM`` (4096 px).
- ``placement`` is one of ``PLACEMENTS`` — the anchor at which the
  original image region sits inside the new canvas. The original pixels
  are preserved verbatim; the delta is distributed as directional pad
  (``compute_pad``): edge anchors put all expansion on the opposite
  sides, "center" splits it evenly (extra pixel goes to bottom/right).
- Aspect ratio is intentionally unconstrained — canvas extension exists
  to change framing; bounds are the dimension limits only.
"""

from __future__ import annotations

import io
from typing import Any

from PIL import Image

from app.domain import image_safety

# Editable-region luminance cutoff (see module docstring).
MASK_EDIT_THRESHOLD = 128

# Maximum outpainted canvas dimension on either axis.
MAX_OUT_DIM = 4096

PLACEMENTS = ("center",
              "top", "bottom", "left", "right",
              "top_left", "top_right", "bottom_left", "bottom_right")


def inspect_edit_mask(data: bytes, *, src_w: int, src_h: int) -> dict[str, Any]:
    """Validate an edit mask against the source dimensions.

    Runs the shared safety gate first (payload limits, structural
    decode), then enforces the documented convention: exact dimension
    match and at least one editable pixel. Raises on any violation —
    callers never receive a partially valid mask.
    """
    info = image_safety.inspect_image(data)
    if info.width != src_w or info.height != src_h:
        raise ValueError(
            f"mask dimensions {info.width}x{info.height} do not match "
            f"source {src_w}x{src_h} — masks are never silently resized")
    with Image.open(io.BytesIO(data)) as im:
        lum = im.convert("L")
        # Mode "L" → one luminance byte per pixel.
        editable = sum(1 for p in lum.tobytes()
                       if p >= MASK_EDIT_THRESHOLD)
    if editable == 0:
        raise ValueError("mask has no editable region — no pixel reaches "
                         f"the luminance threshold {MASK_EDIT_THRESHOLD}")
    return {"width": info.width, "height": info.height,
            "format": info.format, "editable_pixels": editable,
            "editable_ratio": round(editable / info.pixels, 4)}


def validate_outpaint(src_w: int, src_h: int,
                      out_w: int, out_h: int) -> None:
    """Enforce the outpaint dimension contract (see module docstring)."""
    if not (1 <= out_w <= MAX_OUT_DIM and 1 <= out_h <= MAX_OUT_DIM):
        raise ValueError(
            f"outpaint dimensions must be 1–{MAX_OUT_DIM} px "
            f"(got {out_w}x{out_h})")
    if out_w < src_w or out_h < src_h:
        raise ValueError(
            f"outpaint canvas {out_w}x{out_h} is smaller than the "
            f"source {src_w}x{src_h} — expansion only, no crop")
    if out_w == src_w and out_h == src_h:
        raise ValueError("outpaint canvas equals the source — nothing "
                         "to expand")


def mask_to_provider_b64(data: bytes) -> str:
    """Translate the internal mask convention to the provider wire
    format. Both documented inpaint contracts implemented here —
    OpenAI Images edits and BFL FLUX Fill — expect an image whose
    transparent region is editable (the inverse of our luminance
    convention). This explicit boundary conversion is what the
    simulated submit body carries; the internal mask is never assumed
    to be provider-compatible as-is."""
    import base64
    info = image_safety.inspect_image(data)
    with Image.open(io.BytesIO(data)) as im:
        lum = im.convert("L").tobytes()
    w, h = info.width, info.height
    rgba = Image.new("RGBA", (w, h))
    px = rgba.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = ((0, 0, 0, 0) if lum[y * w + x] >= MASK_EDIT_THRESHOLD
                        else (0, 0, 0, 255))
    buf = io.BytesIO()
    rgba.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def compute_pad(src_w: int, src_h: int, out_w: int, out_h: int,
                placement: str) -> dict[str, int]:
    """Directional pad for a placement anchor (see module docstring).

    Edge anchors concentrate expansion on the opposite sides; "center"
    splits each axis evenly with the odd pixel on bottom/right.
    """
    if placement not in PLACEMENTS:
        raise ValueError(f"unknown placement '{placement}' — one of "
                         f"{', '.join(PLACEMENTS)}")
    dw, dh = out_w - src_w, out_h - src_h
    left = {"left": 0, "top_left": 0, "bottom_left": 0,
            "right": dw, "top_right": dw, "bottom_right": dw}
    top = {"top": 0, "top_left": 0, "top_right": 0,
           "bottom": dh, "bottom_left": dh, "bottom_right": dh}
    l = left.get(placement, dw // 2)
    t = top.get(placement, dh // 2)
    return {"left": l, "right": dw - l, "top": t, "bottom": dh - t}
