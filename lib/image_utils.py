"""Image utilities shared between the offline Pillbox preprocessing script
and the runtime api upload endpoint.

Both code paths run rembg on an input image and then call `crop_to_pill_bbox()`
on the resulting RGBA cutout so the pill fills the saved thumbnail rather
than floating inside a large transparent margin.
"""

from __future__ import annotations

from PIL import Image


def crop_to_pill_bbox(img: Image.Image, pad: int = 8) -> Image.Image:
    """Tighten a rembg-style RGBA cutout to the pill's bounding box.

    Steps:
      1. Find the bounding box of non-transparent pixels (the pill).
      2. Expand by `pad` pixels in each direction (clipped to image bounds)
         so the pill isn't flush against the thumbnail edge.

    Output aspect ratio matches the pill (no square padding). The frontend
    renders the image inside a fixed-size square thumbnail with
    `object-fit: contain`, so the card background fills any unused space —
    embedding transparent square padding in the PNG just adds bytes for
    the same visual result.

    If the image is fully transparent (rembg returned nothing), returns the
    input unchanged so the caller can decide how to handle that pathological
    case (most callers just save anyway).
    """
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    bbox = img.getbbox()
    if not bbox:
        return img

    left, top, right, bottom = bbox
    w, h = img.size
    left   = max(0, left - pad)
    top    = max(0, top - pad)
    right  = min(w, right + pad)
    bottom = min(h, bottom + pad)
    return img.crop((left, top, right, bottom))
