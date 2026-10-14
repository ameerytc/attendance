"""
Logo upload validation. Never trusts the client's filename or content-type:
the bytes are opened with Pillow, verified, the real format is read from the
image itself, and the image is re-encoded (which also strips metadata).
"""

import io
import uuid

from PIL import Image, UnidentifiedImageError

ALLOWED_FORMATS = {"PNG": ("png", "image/png"), "JPEG": ("jpg", "image/jpeg"), "WEBP": ("webp", "image/webp")}
MAX_DIMENSION = 4000      # reject absurd source images (decompression bombs)
OUTPUT_MAX_SIDE = 600     # logos never need to be larger than this


class ImageRejected(ValueError):
    pass


def process_logo(file_storage, max_bytes: int, max_side: int = OUTPUT_MAX_SIDE):
    """Return (bytes, extension, content_type) of a safe, normalised image (logo or passport photo)."""
    raw = file_storage.read(max_bytes + 1)
    if not raw:
        raise ImageRejected("The selected file is empty.")
    if len(raw) > max_bytes:
        raise ImageRejected(f"The image is larger than {max_bytes // (1024 * 1024)} MB.")
    try:
        probe = Image.open(io.BytesIO(raw))
        fmt = probe.format
        probe.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError):
        raise ImageRejected("That file is not a valid image.")
    if fmt not in ALLOWED_FORMATS:
        raise ImageRejected("Only PNG, JPG/JPEG and WebP images are allowed.")
    img = Image.open(io.BytesIO(raw))   # verify() invalidates the first handle
    if max(img.size) > MAX_DIMENSION:
        raise ImageRejected(f"The image is too large (max {MAX_DIMENSION}px on its longest side).")
    img.load()
    img.thumbnail((max_side, max_side))
    ext, content_type = ALLOWED_FORMATS[fmt]
    out = io.BytesIO()
    if fmt == "JPEG":
        img.convert("RGB").save(out, "JPEG", quality=90, optimize=True)
    elif fmt == "PNG":
        img.save(out, "PNG", optimize=True)
    else:
        img.save(out, "WEBP", quality=90)
    return out.getvalue(), ext, content_type


def new_logo_key(school_id: int, ext: str) -> str:
    """Server-generated storage key; the client filename is never used."""
    return f"schools/{int(school_id)}/logo/{uuid.uuid4().hex}.{ext}"


def new_media_key(school_id: int, kind: str, ext: str) -> str:
    """Server-generated key for person photos, e.g. schools/3/teachers/<uuid>.jpg"""
    if kind not in ("teachers", "students"):
        raise ValueError("unknown media kind")
    return f"schools/{int(school_id)}/{kind}/{uuid.uuid4().hex}.{ext}"
