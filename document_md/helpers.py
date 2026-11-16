"""Bounded local parsing helpers; no URL fetching or external executables."""
from __future__ import annotations

import io
import warnings
import zipfile
from pathlib import PurePosixPath

from PIL import Image, ImageOps, UnidentifiedImageError

from .types import Block, ExtractionError, Options


def check_archive(data: bytes, options: Options) -> None:
    """Check Office archives before a library builds its in-memory object tree."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > options.max_archive_entries:
                raise ExtractionError("The Office archive contains too many entries. Split the document.")
            if sum(item.file_size for item in entries) > options.max_archive_bytes:
                raise ExtractionError("The expanded Office document is too large. Split the document.")
            names: set[str] = set()
            for item in entries:
                name = item.filename.replace("\\", "/")
                if name in names or name.startswith("/") or ".." in PurePosixPath(name).parts:
                    raise ExtractionError("The Office archive contains duplicate or unsafe paths.")
                names.add(name)
                if item.flag_bits & 1:
                    raise ExtractionError("Encrypted Office documents are not supported.")
                if item.file_size > max(item.compress_size, 1) * options.max_archive_ratio:
                    raise ExtractionError("The Office archive compression ratio exceeds the limit. Split or resave the document.")
                if name.lower().endswith((".xml", ".rels")):
                    xml = archive.read(item)
                    # OOXML normally uses UTF-8. Removing NULs also detects the
                    # declarations in UTF-16/32 without resolving any entities.
                    probe = xml.replace(b"\x00", b"").upper()
                    if b"<!DOCTYPE" in probe or b"<!ENTITY" in probe:
                        raise ExtractionError("Office XML contains DTD or entity declarations and cannot be parsed.")
    except ExtractionError:
        raise
    except (zipfile.BadZipFile, RuntimeError, OSError, NotImplementedError) as exc:
        raise ExtractionError("The Office file is not a complete, supported archive.") from exc


def image_block(data: bytes, options: Options, **kwargs) -> Block:
    """Normalize one raster to a bounded, metadata-free PNG for vision."""
    if not data:
        raise ExtractionError("An empty image was encountered.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as original:
                if original.width * original.height > options.max_image_pixels:
                    raise ExtractionError("The image pixel count exceeds the limit. Resize it and try again.")
                if getattr(original, "n_frames", 1) != 1:
                    raise ExtractionError("Multi-frame images are not supported. Split the pages into PNG/JPG files.")
                image = ImageOps.exif_transpose(original)
                image.thumbnail((options.max_image_side, options.max_image_side), Image.Resampling.LANCZOS)
                if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                    rgba = image.convert("RGBA")
                    rgb = Image.new("RGB", rgba.size, "white")
                    rgb.paste(rgba, mask=rgba.getchannel("A"))
                else:
                    rgb = image.convert("RGB")
                # Recreate the image so EXIF, XMP and other metadata cannot leak.
                clean = Image.frombytes("RGB", rgb.size, rgb.tobytes())
                result = io.BytesIO()
                clean.save(result, format="PNG")
        payload = result.getvalue()
        if len(payload) > options.max_total_image_bytes:
            raise ExtractionError("The converted image is too large. Resize it and try again.")
        return Block(kind="image", image=payload, mime_type="image/png", **kwargs)
    except ExtractionError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning, OSError, ValueError) as exc:
        raise ExtractionError("The image is damaged or its format is unsupported. Convert it to PNG/JPG.") from exc


def markdown_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max((len(row) for row in rows), default=0)
    if not width:
        return ""

    def escape(value: object) -> str:
        return str(value if value is not None else "").replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")

    rendered = ["| " + " | ".join(escape(cell) for cell in row + [""] * (width - len(row))) + " |" for row in rows]
    rendered.insert(1, "| " + " | ".join(["---"] * width) + " |")
    return "\n".join(rendered)
