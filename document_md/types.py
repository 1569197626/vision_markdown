from dataclasses import dataclass, field
from typing import Literal

class ExtractionError(ValueError):
    """An input cannot be processed safely or faithfully enough."""

@dataclass(frozen=True)
class Options:
    max_file_bytes: int = 20 * 1024 * 1024
    max_pages: int = 20
    max_images: int = 24
    max_image_side: int = 1800
    max_image_pixels: int = 20_000_000
    max_total_image_bytes: int = 32 * 1024 * 1024
    max_output_chars: int = 500_000
    max_table_cells: int = 20_000
    max_archive_entries: int = 5_000
    max_archive_bytes: int = 100 * 1024 * 1024
    max_archive_ratio: int = 200

@dataclass
class Block:
    kind: Literal["text", "image"]
    text: str = ""
    image: bytes = b""
    mime_type: str = "image/png"
    page: int | None = None
    origin: str = ""
    purpose: Literal["transcribe", "describe", "mixed"] = "mixed"
    context: str = ""
    bbox: tuple[float, float, float, float] | None = None

@dataclass
class ParsedDocument:
    filename: str
    format: str
    blocks: list[Block] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    page_count: int = 0

@dataclass
class VisionResult:
    markdown: str
    usage: dict = field(default_factory=dict)
    truncated: bool = False

@dataclass
class ConversionResult:
    markdown: str
    documents: list[dict]
    warnings: list[str]
    stats: dict

