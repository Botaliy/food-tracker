"""Meal photos: shrunk before they go to the model. The full-size photo is never
stored; only a small thumbnail is kept for the meal card (see app.photos).

The browser already shrinks photos before upload; this is the safety net for
one that didn't (an old browser, a direct API call). Decoding with draft()
keeps a 12 MP photo from costing ~120 MB of RAM on a 1 GB server.
"""

import ctypes
import ctypes.util
import io

from PIL import Image, ImageOps, UnidentifiedImageError

# ~1.6k image tokens at this size; more pixels don't help to tell rice from buckwheat.
MAX_IMAGE_EDGE = 1280
# A menu or a watch screenshot in the coach chat is read, not recognized: small print needs the pixels.
TEXT_IMAGE_EDGE = 1568


def shrink(raw: bytes, max_edge: int = MAX_IMAGE_EDGE) -> bytes | None:
    """The photo as a JPEG no larger than max_edge; None if it isn't an image."""
    try:
        with Image.open(io.BytesIO(raw)) as img:
            scale = max_edge / max(img.size)
            if scale < 1:
                img.draft("RGB", (int(img.width * scale), int(img.height * scale)))
            img = ImageOps.exif_transpose(img)
            img.thumbnail((max_edge, max_edge))
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=85)
            return buf.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        return None


_libc = ctypes.CDLL(ctypes.util.find_library("c"))


def release_memory() -> None:
    """Give freed memory back to the OS (glibc only; a no-op elsewhere)."""
    trim = getattr(_libc, "malloc_trim", None)
    if trim is not None:
        trim(0)
