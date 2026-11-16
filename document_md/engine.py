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
            notes = ["文本按 GB18030 解码；若出现乱码，请将源文件另存为 UTF-8。"]
        except UnicodeDecodeError as exc:
            raise ExtractionError("文本编码不受支持，请另存为 UTF-8。") from exc
    if "\x00" in text:
        raise ExtractionError("文本文件包含二进制内容。")
    return text, notes


def _parse_text(data: bytes, filename: str, extension: str, options: Options) -> ParsedDocument:
    text, notes = _decode(data)
    if len(text) > options.max_output_chars:
        raise ExtractionError("文本过长，请拆分后转换。")
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
                    raise ExtractionError("CSV 单元格数量超出限制，请拆分文件。")
                rows.append(row)
            if rows and max(map(len, rows)) * len(rows) > options.max_table_cells:
                raise ExtractionError("CSV 表格展开后过大，请整理列数或拆分文件。")
            text = markdown_table(rows)
        except csv.Error as exc:
            raise ExtractionError("CSV 格式错误或单元格过长，请重新导出。") from exc
        notes.append("CSV 第一行用作 Markdown 表头；公式按原始文本保留，不执行。")
    return ParsedDocument(filename=filename, format=extension[1:], blocks=[Block(kind="text", text=text, origin="body")], warnings=notes)


def parse_document(data: bytes, filename: str, options: Options) -> ParsedDocument:
    if not isinstance(data, bytes) or not data:
        raise ExtractionError("文件为空。")
    if len(data) > options.max_file_bytes:
        raise ExtractionError(f"文件超过 {options.max_file_bytes // (1024 * 1024)} MiB，请拆分后转换。")
    safe_name = PurePosixPath(filename.replace("\\", "/")).name
    extension = PurePosixPath(safe_name).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise ExtractionError(f"不支持 {extension or '无扩展名'} 文件。旧版 .doc/.ppt/.xls 请先另存为 .docx/.pptx/.xlsx。")
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
        raise ExtractionError(f"图片数量超过 {options.max_images}，请拆分文档。")
    if sum(len(block.image) for block in image_blocks) > options.max_total_image_bytes:
        raise ExtractionError("文档内图片总量超出限制，请拆分文档。")
    if parsed.page_count > options.max_pages:
        raise ExtractionError(f"页数超过 {options.max_pages}，请拆分文档。")
    if native_chars > options.max_output_chars:
        raise ExtractionError("提取文字超出输出限制，请拆分文档。")
    if image_blocks and vision is None:
        raise ExtractionError("文档包含图片或扫描页，请选择已配置的 Dify 视觉模型。")

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
                raise ExtractionError(f"图片识别失败（位置：{block.origin or block.page or '未知'}），未输出不完整文件。") from exc
            calls += 1
            if not isinstance(result, VisionResult) or result.truncated:
                raise ExtractionError("视觉模型输出被截断或无效，请拆分文档或增加模型输出上限。")
            if not isinstance(result.markdown, str) or not result.markdown.strip():
                raise ExtractionError("视觉模型返回空内容，未输出不完整文件。")
            content = result.markdown.strip()
            usages.append(result.usage)
        # Use the same newline convention for native text and vision output.
        # Preserve paragraph spacing and literal backslash sequences; only
        # translate actual CRLF/CR characters before sizing and emitting text.
        content = content.replace("\r\n", "\n").replace("\r", "\n")
        if content:
            chars += len(content) + (2 if parts else 0)
            if chars > options.max_output_chars:
                raise ExtractionError("Markdown 超出输出限制，请拆分文档。")
            parts.append(content)
    markdown = "\n\n".join(parts)
    if not markdown:
        raise ExtractionError("文档未提取到可用内容。")
    notes = list(dict.fromkeys(parsed.warnings))
    if calls:
        notes.append("图片内容由所选视觉模型生成；文字、数字、公式和图表关系需要抽样核对。")
    metadata = {"filename": parsed.filename, "format": parsed.format, "page_count": parsed.page_count, "image_count": len(image_blocks), "native_chars": native_chars}
    # Same field shape as the official Dify Extractor's Document output.
    document = {"page_content": markdown, "metadata": metadata, "provider": "dify", "vector": None, "children": None}
    return ConversionResult(markdown=markdown, documents=[document], warnings=notes, stats={"file_bytes": len(data), "page_count": parsed.page_count, "image_count": len(image_blocks), "vision_calls": calls, "native_chars": native_chars, "output_chars": len(markdown), "model_usage": usages})
