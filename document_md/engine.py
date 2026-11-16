"""Parse locally, then call the selected vision model only for image blocks."""
from __future__ import annotations

import csv
import io
from pathlib import PurePosixPath
from typing import Callable

from .helpers import image_block, markdown_table
from .types import Block, ConversionResult, ExtractionError, Options, ParsedDocument, VisionResult


SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".txt", ".md", ".png", ".jpg", ".jpeg", ".webp"}


def _decode(data: bytes) -> tuple[str, list[str]]:
    try:
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = data.decode("utf-16")
        else:
            text = data.decode("utf-8-sig")
        notes = []
    except UnicodeDecodeError:
        try:
            text = data.decode("gb18030")
            notes = ["Text was decoded as GB18030. If it appears garbled, resave the source as UTF-8."]
        except UnicodeDecodeError as exc:
            raise ExtractionError("The text encoding is unsupported. Resave the file as UTF-8.") from exc
    if "\x00" in text:
        raise ExtractionError("The text file contains binary content.")
    return text, notes


def _parse_text(data: bytes, filename: str, extension: str, options: Options) -> ParsedDocument:
    text, notes = _decode(data)
    if len(text) > options.max_output_chars:
        raise ExtractionError("The text is too long. Split it before conversion.")
    if extension == ".csv":
        try:
            try:
                dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            rows: list[list[str]] = []
            cells = 0
            for row in csv.reader(io.StringIO(text, newline=""), dialect, strict=True):
                cells += len(row)
                if cells > options.max_table_cells:
                    raise ExtractionError("The CSV cell count exceeds the limit. Split the file.")
                rows.append(row)
            if rows and max(map(len, rows)) * len(rows) > options.max_table_cells:
                raise ExtractionError("The expanded CSV table is too large. Reduce the column count or split the file.")
            text = markdown_table(rows)
        except csv.Error as exc:
            raise ExtractionError("The CSV format is invalid or a cell is too long. Export it again.") from exc
        notes.append("The first CSV row is used as the Markdown header. Formulas are retained as original text and are not executed.")
    return ParsedDocument(filename=filename, format=extension[1:], blocks=[Block(kind="text", text=text, origin="body")], warnings=notes)


def parse_document(data: bytes, filename: str, options: Options) -> ParsedDocument:
    if not isinstance(data, bytes) or not data:
        raise ExtractionError("The file is empty.")
    if len(data) > options.max_file_bytes:
        raise ExtractionError(f"The file exceeds {options.max_file_bytes // (1024 * 1024)} MiB. Split it before conversion.")
    safe_name = PurePosixPath(filename.replace("\\", "/")).name
    extension = PurePosixPath(safe_name).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise ExtractionError(f"Files with extension {extension or 'none'} are not supported. Resave legacy .doc/.ppt/.xls files as .docx/.pptx/.xlsx.")
    if extension == ".pdf":
        from .pdf import parse_pdf
        return parse_pdf(data, safe_name, options)
    if extension in {".docx", ".pptx", ".xlsx"}:
        from .office import parse_office
        return parse_office(data, safe_name, options)
    if extension in {".png", ".jpg", ".jpeg", ".webp"}:
        return ParsedDocument(filename=safe_name, format=extension[1:], blocks=[image_block(data, options, origin="image", page=1, purpose="mixed")], page_count=1)
    return _parse_text(data, safe_name, extension, options)


def convert_document(data: bytes, filename: str, vision: Callable[[Block], VisionResult] | None, options: Options) -> ConversionResult:
    parsed = parse_document(data, filename, options)
    image_blocks = [block for block in parsed.blocks if block.kind == "image"]
    native_chars = sum(len(block.text) for block in parsed.blocks if block.kind == "text")
    # Complete structural validation before any potentially billable model call.
    if len(image_blocks) > options.max_images:
        raise ExtractionError(f"The image count exceeds {options.max_images}. Split the document.")
    if sum(len(block.image) for block in image_blocks) > options.max_total_image_bytes:
        raise ExtractionError("The total document image size exceeds the limit. Split the document.")
    if parsed.page_count > options.max_pages:
        raise ExtractionError(f"The page count exceeds {options.max_pages}. Split the document.")
    if native_chars > options.max_output_chars:
        raise ExtractionError("The extracted text exceeds the output limit. Split the document.")
    if image_blocks and vision is None:
        raise ExtractionError("The document contains images or scanned pages. Select a configured Dify vision model.")

    parts: list[str] = []
    chars = 0
    calls = 0
    usages: list[dict] = []
    for block in parsed.blocks:
        if block.kind == "text":
            content = block.text.strip()
        else:
            try:
                result = vision(block)  # type: ignore[misc]
            except ExtractionError:
                raise
            except Exception as exc:
                # Keep model messages/credentials out of the end-user error.
                raise ExtractionError(f"Image recognition failed (location: {block.origin or block.page or 'unknown'}). No incomplete file was returned.") from exc
            calls += 1
            if not isinstance(result, VisionResult) or result.truncated:
                raise ExtractionError("The vision model output is truncated or invalid. Split the document or increase the model output limit.")
            if not isinstance(result.markdown, str) or not result.markdown.strip():
                raise ExtractionError("The vision model returned empty content. No incomplete file was returned.")
            content = result.markdown.strip()
            usages.append(result.usage)
        # Use the same newline convention for native text and vision output.
        # Preserve paragraph spacing and literal backslash sequences; only
        # translate actual CRLF/CR characters before sizing and emitting text.
        content = content.replace("\r\n", "\n").replace("\r", "\n")
        if content:
            chars += len(content) + (2 if parts else 0)
            if chars > options.max_output_chars:
                raise ExtractionError("Markdown exceeds the output limit. Split the document.")
            parts.append(content)
    markdown = "\n\n".join(parts)
    if not markdown:
        raise ExtractionError("No usable content could be extracted from the document.")
    notes = list(dict.fromkeys(parsed.warnings))
    if calls:
        notes.append("Image content was generated by the selected vision model. Spot-check text, numbers, formulas, and chart relationships.")
    metadata = {"filename": parsed.filename, "format": parsed.format, "page_count": parsed.page_count, "image_count": len(image_blocks), "native_chars": native_chars}
    # Same field shape as the official Dify Extractor's Document output.
    document = {"page_content": markdown, "metadata": metadata, "provider": "dify", "vector": None, "children": None}
    return ConversionResult(markdown=markdown, documents=[document], warnings=notes, stats={"file_bytes": len(data), "page_count": parsed.page_count, "image_count": len(image_blocks), "vision_calls": calls, "native_chars": native_chars, "output_chars": len(markdown), "model_usage": usages})
