"""Decode only supported snapshots and persist metadata-free JPEGs under generated names."""

import hashlib
import io
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import Settings


@dataclass
class PreparedImage:
    """Carry normalized JPEG bytes, original attachment identity and decoded dimensions."""

    data: bytes
    source_digest: str
    width: int
    height: int


def prepare_image(data: bytes, settings: Settings) -> PreparedImage:
    """Validate a bounded static snapshot and produce an oriented, metadata-free JPEG."""
    if len(data) > settings.max_image_bytes:
        raise HTTPException(413, "Image exceeds byte limit")
    try:
        with Image.open(io.BytesIO(data), formats=("JPEG", "PNG", "WEBP")) as image:
            if image.width * image.height > settings.max_image_pixels:
                raise ValueError("Too many pixels")
            if getattr(image, "is_animated", False):
                raise ValueError("Animated images are not supported")
            image.verify()

        # Verification invalidates the decoder; reopen before loading and normalizing pixels.
        with Image.open(io.BytesIO(data), formats=("JPEG", "PNG", "WEBP")) as image:
            image.load()
            oriented = ImageOps.exif_transpose(image)
            rgba = oriented.convert("RGBA")
            # A fresh image drops EXIF, text chunks, ICC profiles and other source metadata.
            normalized = Image.new("RGB", rgba.size, "white")
            normalized.paste(rgba, mask=rgba.getchannel("A"))
            output = io.BytesIO()
            normalized.save(output, format="JPEG", quality=85)
            result = output.getvalue()
            width, height = normalized.size

    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise HTTPException(422, "Invalid or unsupported snapshot") from None
    if len(result) > settings.max_image_bytes:
        raise HTTPException(413, "Normalized image exceeds byte limit")
    return PreparedImage(result, hashlib.sha256(data).hexdigest(), width, height)


def write_image(image: PreparedImage, data_dir: Path, received_at: str) -> tuple[str, str, Path]:
    """Atomically save a generated private image path; the caller owns rollback cleanup."""
    image_id = str(uuid4())
    disk_name = received_at[:10].replace("-", "/") + "/" + image_id + ".jpg"
    path = data_dir / "images" / disk_name
    write_private_file(path, image.data)
    return image_id, disk_name, path


def write_private_file(path: Path, data: bytes) -> None:
    """Atomically replace a private file, removing temporary data on write failures."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    # Keep the temporary file on the same filesystem so rename remains atomic.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".upload-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def thumbnail_path(source: Path, destination: Path) -> Path:
    """Lazily derive a bounded preview from a sanitized JPEG, including older events."""
    if not destination.is_file():
        with Image.open(source) as image:
            image.thumbnail((640, 480), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=75)
        write_private_file(destination, output.getvalue())
    return destination
