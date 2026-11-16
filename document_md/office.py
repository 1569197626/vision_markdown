"""Native OOXML extraction. No office subprocesses, macros, or network access."""
from __future__ import annotations

import io
import re
import warnings
import zipfile
from collections import deque
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import PurePath
from typing import Iterator

from defusedxml import ElementTree
from PIL import Image

from .helpers import check_archive, image_block, markdown_table
from .types import Block, ExtractionError, Options, ParsedDocument


def _local(element) -> str:
    return element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else ""


def _attr(element, name: str, default: str = "") -> str:
    return next((value for key, value in element.attrib.items() if key.rsplit("}", 1)[-1] == name), default)


class _Output:
    def __init__(self, filename: str, format: str, options: Options):
        self.document = ParsedDocument(filename=filename, format=format)
        self.options = options
        self.cells = self.chars = self.images = self.image_bytes = 0
        self.page = 1
        self.auxiliary = False
        self.docx_extras = None

    def warn(self, text: str) -> None:
        if text not in self.document.warnings:
            self.document.warnings.append(text)

    def text(self, text: str, *, origin: str = "", page: int | None = None) -> None:
        if not text.strip():
            return
        self.chars += len(text)
        if self.chars > self.options.max_output_chars:
            raise ExtractionError("Office native text exceeds the output limit. Split the document.")
        self.document.blocks.append(Block(kind="text", text=text, page=None if self.auxiliary else page or self.page, origin=origin))

    def image(self, data: bytes, *, origin: str, page: int | None = None, context: str = "", crop=None, rotation: float = 0, flip_h: bool = False, flip_v: bool = False) -> None:
        self.images += 1
        if self.images > self.options.max_images:
            raise ExtractionError("The Office image count exceeds the limit. Split the document.")
        block = image_block(data, self.options, origin=origin, page=None if self.auxiliary else page or self.page, context=context, purpose="mixed")
        if crop or rotation or flip_h or flip_v:
            # Normalize/limit the image before allocating a cropped/rotated copy.
            with Image.open(io.BytesIO(block.image)) as source:
                picture = source.copy()
            if crop:
                left, top, right, bottom = crop
                if min(crop) < 0:
                    self.warn("An image crop extends beyond its bounds. Only the image itself was retained; review the layout.")
                box = (max(0, round(picture.width * left)), max(0, round(picture.height * top)),
                       min(picture.width, round(picture.width * (1 - right))),
                       min(picture.height, round(picture.height * (1 - bottom))))
                if box[0] >= box[2] or box[1] >= box[3]:
                    raise ExtractionError("The Office image crop region is empty or invalid.")
                picture = picture.crop(box)
            if flip_h:
                picture = picture.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            if flip_v:
                picture = picture.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            if rotation:
                picture = picture.rotate(-rotation, expand=True, fillcolor="white")
            stream = io.BytesIO()
            picture.save(stream, format="PNG")
            block = image_block(stream.getvalue(), self.options, origin=origin, page=None if self.auxiliary else page or self.page, context=context, purpose="mixed")
        self.image_bytes += len(block.image)
        if self.image_bytes > self.options.max_total_image_bytes:
            raise ExtractionError("The total Office image size exceeds the limit. Split the document.")
        self.document.blocks.append(block)

    def count_cells(self, count: int) -> None:
        self.cells += count
        if self.cells > self.options.max_table_cells:
            raise ExtractionError("The Office table cell count exceeds the limit. Split the document.")

    def next_page(self) -> None:
        if self.auxiliary:
            self.warn("Page breaks in DOCX extra content are excluded from the body page count; per-page layout was not reconstructed.")
            return
        self.page += 1
        if self.page > self.options.max_pages:
            raise ExtractionError("The Office page count exceeds the limit. Split the document.")


def parse_office(data: bytes, filename: str, options: Options) -> ParsedDocument:
    """Return ordered native text/image blocks for DOCX, PPTX, or XLSX."""
    if len(data) > options.max_file_bytes:
        raise ExtractionError("The Office file exceeds the size limit. Split the document.")
    extension = PurePath(filename).suffix.lower().lstrip(".")
    if extension not in {"docx", "pptx", "xlsx"}:
        raise ExtractionError("Only DOCX, PPTX, and XLSX are supported. Resave legacy or macro-enabled formats first.")
    if options.max_pages < 1:
        raise ExtractionError("The Office page limit must be greater than zero.")
    check_archive(data, options)
    output = _Output(filename, extension, options)
    try:
        if extension == "docx":
            _parse_docx(data, output)
        elif extension == "pptx":
            _parse_pptx(data, output)
        else:
            _parse_xlsx(data, output)
    except ExtractionError:
        raise
    except Exception as exc:
        # Do not expose source-document values or zip paths in an SDK traceback.
        raise ExtractionError(f"Could not parse {extension.upper()}: the file is damaged, encrypted, or contains unsupported structures.") from exc
    return output.document


def _drawing_tokens(element, part, output: _Output) -> Iterator[tuple[str, object]]:
    if any(_local(child) == "anchor" for child in element.iter()):
        output.warn("DOCX floating images are inserted at their anchors. Review reading order for wrapped and multi-column layouts.")
    if any(_local(child) in {"chart", "relIds", "txbxContent"} for child in element.iter()):
        output.warn("DOCX charts, SmartArt, and text boxes are not rendered. Save these objects as PDF before conversion.")
    seen = set()
    for child in element.iter():
        if _local(child) not in {"blip", "imagedata"}:
            continue
        if _attr(child, "link"):
            output.warn("DOCX contains externally linked images. They were not downloaded; embed them first.")
        relationship_id = _attr(child, "embed") or _attr(child, "id")
        if not relationship_id or relationship_id in seen:
            continue
        seen.add(relationship_id)
        relationship = part.rels.get(relationship_id)
        if relationship is None:
            output.warn("A DOCX image relationship is missing; the image could not be read.")
            continue
        if relationship.is_external:
            output.warn("DOCX contains externally linked images. They were not downloaded; embed them first.")
            continue
        image_part = relationship.target_part
        if any(word in image_part.content_type.lower() for word in ("svg", "emf", "wmf")):
            output.warn("DOCX contains SVG/EMF/WMF vector images that were not rasterized. Save as PDF before conversion.")
            continue
        crop = None
        for rectangle in element.iter():
            if _local(rectangle) == "srcRect":
                crop = tuple(int(_attr(rectangle, side, "0")) / 100000 for side in ("l", "t", "r", "b"))
                break
        yield "image", (image_part.blob, crop)


def _docx_tokens(element, part, output: _Output) -> Iterator[tuple[str, object]]:
    """Walk runs and hyperlinks in XML order, splitting at inline images."""
    local = _local(element)
    if local in {"del", "moveFrom"}:
        output.warn("DOCX contains tracked changes: current insertions are extracted and deleted content is ignored.")
        return
    if local in {"ins", "moveTo"}:
        output.warn("DOCX contains tracked changes: current insertions are extracted and deleted content is ignored.")
    if local == "AlternateContent":
        child = next((item for item in element if _local(item) == "Choice"), None)
        if child is None:
            child = next((item for item in element if _local(item) == "Fallback"), None)
        if child is not None:
            yield from _docx_tokens(child, part, output)
        return
    if local == "t":
        yield "text", element.text or ""
        return
    if local == "tab":
        yield "text", "\t"
        return
    if local in {"br", "cr"}:
        yield ("page", None) if _attr(element, "type") == "page" else ("text", "\n")
        return
    if local == "noBreakHyphen":
        yield "text", "‑"
        return
    if local in {"drawing", "pict"}:
        yield from _drawing_tokens(element, part, output)
        return
    if local == "object":
        output.warn("Embedded DOCX OLE objects are not executed or parsed. Convert them to plain text, images, or PDF.")
        return
    if local in {"footnoteReference", "endnoteReference"}:
        if output.docx_extras is not None:
            yield "text", output.docx_extras.reference(local, _attr(element, "id"))
        else:
            output.warn("A DOCX footnote or endnote reference could not be resolved.")
        return
    if local in {"oMath", "oMathPara"}:
        output.warn("Only visible DOCX equation text is extracted; mathematical layout is not fully preserved.")
    if local in {"pPr", "rPr", "sectPr", "instrText", "lastRenderedPageBreak"}:
        return
    for child in element:
        yield from _docx_tokens(child, part, output)


def _docx_is_list(paragraph) -> bool:
    """Resolve numId through paragraph/style inheritance; zero cancels a list."""
    properties = [paragraph._p.pPr]
    style = paragraph.style
    seen = set()
    while style is not None and style.style_id not in seen:
        seen.add(style.style_id)
        properties.append(style.element.pPr)
        style = style.base_style
    for ppr in properties:
        numpr = ppr.numPr if ppr is not None else None
        if numpr is not None and numpr.numId is not None:
            # An ilvl-only direct override still inherits the style's numId.
            return numpr.numId.val != 0
    return False


def _docx_paragraph(paragraph, output: _Output, origin: str) -> None:
    if paragraph.paragraph_format.page_break_before and output.document.blocks:
        output.next_page()
    style = paragraph.style.name if paragraph.style is not None else ""
    heading = re.fullmatch(r"Heading ([1-6])", style, re.IGNORECASE)
    prefix = "#" * int(heading.group(1)) + " " if heading else ("# " if style == "Title" else "")
    if _docx_is_list(paragraph):
        prefix = "- "
        output.warn("DOCX lists are normalized to bullets; original numbering and complex indentation are not fully preserved.")
    pending = ""
    for kind, payload in _docx_tokens(paragraph._p, paragraph.part, output):
        if kind == "text":
            pending += str(payload)
        else:
            if pending.strip():
                output.text(prefix + pending, origin=origin)
                prefix = ""
            pending = ""
            if kind == "page":
                output.next_page()
            elif kind == "image":
                raw, crop = payload
                output.image(raw, crop=crop, origin=origin, context=paragraph.text[:1000])
    if pending.strip():
        output.text(prefix + pending, origin=origin)


def _docx_table(table, output: _Output, origin: str) -> None:
    rows: list[list[str]] = []
    pictures = []
    seen = set()
    for row_index, row in enumerate(table.rows, 1):
        output.count_cells(len(row.cells))
        values = []
        for column_index, cell in enumerate(row.cells, 1):
            if cell._tc in seen:
                values.append("")
                continue
            seen.add(cell._tc)
            pieces = []
            for paragraph in cell.paragraphs:
                for kind, payload in _docx_tokens(paragraph._p, paragraph.part, output):
                    if kind == "text":
                        pieces.append(str(payload))
                    elif kind == "image":
                        reference = f"Table image {len(pictures) + 1}"
                        pieces.append(f"[{reference}]")
                        pictures.append((payload, f"{origin}/row-{row_index}/col-{column_index}", reference, output.page))
                    elif kind == "page":
                        output.next_page()
                pieces.append("\n")
            if cell.tables:
                output.warn("DOCX nested tables are emitted after their parent table. Review local reading order.")
            values.append("".join(pieces).strip())
        rows.append(values)
    output.text(markdown_table(rows), origin=origin)
    if pictures:
        output.warn("Images in DOCX tables are located by placeholders; recognized image content is emitted after the table.")
    for (raw, crop), picture_origin, context, page in pictures:
        output.text(f"### {context}", origin=picture_origin, page=page)
        output.image(raw, crop=crop, origin=picture_origin, context=context, page=page)
    nested_seen = set()
    for row in table.rows:
        for cell in row.cells:
            if cell._tc in nested_seen:
                continue
            nested_seen.add(cell._tc)
            for index, nested in enumerate(cell.tables, 1):
                _docx_table(nested, output, f"{origin}/nested-{index}")


class _DocxStoryPart:
    """Use the real story relationships and the document's shared style table.

    python-docx loads footnotes/endnotes as generic OPC parts, unlike headers.
    This small adapter lets the existing paragraph/table/image walker read them
    without mutating the package or adding an Office renderer.
    """
    def __init__(self, part, document_part):
        self.rels = part.rels
        self._document_part = document_part

    def get_style(self, style_id, style_type):
        return self._document_part.get_style(style_id, style_type)


class _DocxStory:
    def __init__(self, part, document_part):
        self.part = _DocxStoryPart(part, document_part)


def _docx_story(root, part, document_part, output: _Output, origin: str) -> None:
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    parent = _DocxStory(part, document_part)
    for index, element in enumerate(root, 1):
        child_origin = f"{origin}/item-{index}"
        if _local(element) == "p":
            _docx_paragraph(Paragraph(element, parent), output, child_origin)
        elif _local(element) == "tbl":
            _docx_table(Table(element, parent), output, child_origin)
        else:
            output.warn("DOCX extra content contains unsupported objects or content controls. Review the source.")


class _DocxExtras:
    """Read used headers/footers and referenced notes once, with visible labels."""
    def __init__(self, document, output: _Output):
        self.document = document
        self.output = output
        self.references: dict[tuple[str, int], None] = {}
        self.pending: deque[tuple[str, int]] = deque()
        self.note_parts: dict[str, dict[int, tuple[object, object] | None]] = {}
        self.note_records = 0

    def reference(self, tag: str, raw_id: str) -> str:
        kind = "footnote" if tag == "footnoteReference" else "endnote"
        label = "Footnote" if kind == "footnote" else "Endnote"
        if not re.fullmatch(r"\d{1,10}", raw_id) or int(raw_id) > 2_147_483_647:
            self.output.warn("DOCX footnotes/endnotes contain an invalid reference ID; the referenced text was not extracted.")
            return f"【{label} reference invalid】"
        note_id = int(raw_id)
        key = (kind, note_id)
        if key not in self.references:
            # References themselves are a bounded collection, even if their
            # target notes contain no visible text.
            if len(self.references) >= self.output.options.max_table_cells:
                raise ExtractionError("The DOCX note reference count exceeds the resource limit. Split the document.")
            self.references[key] = None
            self.pending.append(key)
        return f"【{label} {note_id}】"

    def _notes(self, kind: str):
        from docx.oxml import parse_xml

        if kind in self.note_parts:
            return self.note_parts[kind]
        notes: dict[int, tuple[object, object] | None] = {}
        self.note_parts[kind] = notes
        seen_parts = set()
        for relationship in self.document.part.rels.values():
            if not relationship.reltype.endswith("/" + kind + "s"):
                continue
            if relationship.is_external:
                self.output.warn("A DOCX note has an external relationship and was not fetched.")
                continue
            part = relationship.target_part
            if str(part.partname) in seen_parts:
                continue
            seen_parts.add(str(part.partname))
            root = parse_xml(part.blob)
            if _local(root) != kind + "s":
                self.output.warn("A DOCX note relationship has a mismatched body structure; that part was not extracted.")
                continue
            for element in root:
                raw_id = _attr(element, "id")
                if (_local(element) != kind or _attr(element, "type", "normal") != "normal"
                        or not re.fullmatch(r"\d{1,10}", raw_id) or int(raw_id) > 2_147_483_647):
                    continue  # Separator records (including negative IDs) are not notes.
                self.note_records += 1
                if self.note_records > self.output.options.max_table_cells:
                    raise ExtractionError("The DOCX note record count exceeds the resource limit. Split the document.")
                note_id = int(raw_id)
                if note_id in notes:
                    notes[note_id] = None
                    self.output.warn("DOCX notes contain duplicate IDs; referenced text is ambiguous. Review the source.")
                else:
                    notes[note_id] = (element, part)
        return notes

    def _headers_and_footers(self) -> None:
        from docx.oxml import parse_xml

        previous = {}
        used = {}
        even_enabled = self.document.settings.odd_and_even_pages_header_footer
        for section_index, section in enumerate(self.document.sections, 1):
            for reference in section._sectPr:
                local = _local(reference)
                if local not in {"headerReference", "footerReference"}:
                    continue
                kind = local.removesuffix("Reference")
                variant = _attr(reference, "type", "default")
                if variant not in {"default", "first", "even"}:
                    self.output.warn("DOCX contains an unknown header/footer type; that part was not extracted.")
                    continue
                previous[(kind, variant)] = _attr(reference, "id")
            for (kind, variant), relation_id in previous.items():
                if variant == "first" and not section.different_first_page_header_footer:
                    continue
                if variant == "even" and not even_enabled:
                    continue
                relationship = self.document.part.rels.get(relation_id)
                if relationship is None or not relationship.reltype.endswith("/" + kind):
                    self.output.warn("A DOCX header/footer relationship is missing or has a mismatched type. Review the source.")
                    continue
                if relationship.is_external:
                    self.output.warn("A DOCX header/footer has an external relationship and was not fetched.")
                    continue
                part = relationship.target_part
                key = (kind, str(part.partname))
                if key not in used:
                    used[key] = (part, [])
                scope = f"Section {section_index}/{ {'default': 'default', 'first': 'first page', 'even': 'even pages'}[variant]}"
                used[key][1].append(scope)
        for (kind, part_name), (part, scopes) in used.items():
            root = parse_xml(part.blob)
            if _local(root) != {"header": "hdr", "footer": "ftr"}[kind]:
                self.output.warn("A DOCX header/footer has a mismatched body structure; that part was not extracted.")
                continue
            label = "Header" if kind == "header" else "Footer"
            self.output.warn("DOCX headers/footers are collected by applicable section and type; shared content is extracted once. Per-page placement and dynamic fields are not reconstructed.")
            origin = f"docx/{kind}/{part_name.rsplit('/', 1)[-1]}"
            self.output.text(f"## {label}（{'、'.join(scopes)}）", origin=origin)
            _docx_story(root, part, self.document.part, self.output, origin)

    def extract(self) -> None:
        self.output.auxiliary = True
        try:
            self._headers_and_footers()
            # Notes may themselves contain references. Iterate only newly seen
            # keys so malformed cycles and repeated references cannot expand forever.
            while self.pending:
                kind, note_id = self.pending.popleft()
                label = "Footnote" if kind == "footnote" else "Endnote"
                origin = f"docx/{kind}-{note_id}"
                self.output.warn("DOCX footnotes/endnotes are labeled by file reference ID and collected in first-reference order; per-page layout and displayed numbering are not reconstructed.")
                self.output.text(f"## {label} {note_id}", origin=origin)
                note = self._notes(kind).get(note_id)
                if note is None:
                    self.output.warn("A DOCX footnote/endnote reference has no unique usable body. Review the source.")
                    self.output.text(f"【{label} {note_id}: body missing or reference not unique】", origin=origin)
                    continue
                element, part = note
                _docx_story(element, part, self.document.part, self.output, origin)
        finally:
            self.output.auxiliary = False


def _parse_docx(data: bytes, output: _Output) -> None:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(io.BytesIO(data))
    output.docx_extras = _DocxExtras(document, output)
    output.warn("DOCX layout is not rendered; the page count is estimated from explicit page breaks. Review actual pagination and multi-column reading order.")
    for part in document.part.package.parts:
        part_name = str(part.partname)
        if "/comments" in part_name:
            output.warn("DOCX comments are not extracted.")
    for index, element in enumerate(document.element.body, 1):
        origin = f"docx/body-{index}"
        if _local(element) == "p":
            _docx_paragraph(Paragraph(element, document), output, origin)
            if any(_local(item) == "sectPr" for item in element.iter()):
                output.warn("DOCX section pagination is not rendered; the page count is only an explicit-page-break estimate.")
        elif _local(element) == "tbl":
            _docx_table(Table(element, document), output, origin)
        elif _local(element) != "sectPr":
            output.warn("DOCX contains unsupported body objects or content controls. Resave as ordinary DOCX/PDF and review.")
    output.document.page_count = output.page
    output.docx_extras.extract()


def _pptx_shapes(shapes, output: _Output, origin: str) -> None:
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    for shape in sorted(shapes, key=lambda item: (item.top or 0, item.left or 0)):
        shape_origin = f"{origin}/shape-{shape.shape_id}"
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            _pptx_shapes(shape.shapes, output, shape_origin)
            continue
        if shape.has_table:
            table = shape.table
            output.count_cells(len(table.rows) * len(table.columns))
            output.text(markdown_table([["" if cell.is_spanned else cell.text for cell in row.cells] for row in table.rows]), origin=shape_origin)
            continue
        if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            if shape.image.ext.lower() in {"svg", "emf", "wmf"}:
                output.warn("PPTX contains SVG/EMF/WMF vector images that were not rasterized. Save as PDF before conversion.")
                continue
            transform = shape._element.find("{http://schemas.openxmlformats.org/presentationml/2006/main}spPr")
            xfrm = None if transform is None else transform.find("{http://schemas.openxmlformats.org/drawingml/2006/main}xfrm")
            output.image(shape.image.blob, origin=shape_origin,
                         crop=(shape.crop_left, shape.crop_top, shape.crop_right, shape.crop_bottom),
                         rotation=shape.rotation,
                         flip_h=xfrm is not None and _attr(xfrm, "flipH") in {"1", "true"},
                         flip_v=xfrm is not None and _attr(xfrm, "flipV") in {"1", "true"})
            continue
        if shape.has_chart:
            from .office_charts import extract_pptx_chart
            extract_pptx_chart(shape.chart, output, origin=shape_origin)
            continue
        if shape.has_text_frame:
            paragraphs = []
            for paragraph in shape.text_frame.paragraphs:
                if paragraph.text.strip():
                    paragraphs.append("  " * paragraph.level + ("- " if paragraph.level else "") + paragraph.text)
            output.text("\n".join(paragraphs), origin=shape_origin)
            if paragraphs:
                continue
        output.warn("PPTX textless vector shapes, SmartArt, connectors, and other objects are not rendered. Save diagrams as PDF.")


def _parse_pptx(data: bytes, output: _Output) -> None:
    from pptx import Presentation

    presentation = Presentation(io.BytesIO(data))
    if len(presentation.slides) > output.options.max_pages:
        raise ExtractionError("The PPTX slide count exceeds the limit. Split the document.")
    output.document.page_count = len(presentation.slides)
    output.warn("PPTX slide shapes are ordered top-to-bottom and left-to-right. Review order for overlaps, columns, and grouped shapes.")
    output.warn("PPTX masters, backgrounds, notes, animations, and relationships between objects are not rendered.")
    for index, slide in enumerate(presentation.slides, 1):
        output.page = index
        output.text(f"## Slide {index}", origin=f"pptx/slide-{index}")
        _pptx_shapes(slide.shapes, output, f"pptx/slide-{index}")


def _xlsx_preflight(data: bytes, options: Options) -> None:
    """Count serialized cells before openpyxl builds full worksheet objects."""
    from openpyxl.utils.cell import range_boundaries

    count = 0
    sheets = 0
    merged_cells = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in archive.namelist():
            if not re.fullmatch(r"xl/(?:worksheets|chartsheets)/[^/]+\.xml", name):
                continue
            sheets += 1
            if sheets > options.max_pages:
                raise ExtractionError("The XLSX worksheet count exceeds the limit. Split the document.")
            with archive.open(name) as source:
                for _, element in ElementTree.iterparse(source, events=("end",)):
                    if _local(element) == "c":
                        count += 1
                        if count > options.max_table_cells:
                            raise ExtractionError("The XLSX cell count exceeds the limit. Split the document.")
                    elif _local(element) == "mergeCell":
                        # openpyxl expands merge ranges into MergedCell objects,
                        # even when only the top-left cell is serialized.
                        left, top, right, bottom = range_boundaries(element.get("ref", ""))
                        if None in (left, top, right, bottom) or left > right or top > bottom:
                            raise ExtractionError("An XLSX merged-cell range is invalid.")
                        merged_cells += (right - left + 1) * (bottom - top + 1)
                        if merged_cells > options.max_table_cells:
                            raise ExtractionError("The expanded XLSX merged-cell range exceeds the limit. Split the document.")
                    element.clear()


def _xlsx_number(value: int | float, number_format: str) -> str | None:
    """Render a bounded subset of Excel formats, retaining currency and signs.

    Supported: fixed decimal/zero padding, comma grouping, percent, quoted or
    escaped currency/unit affixes, locale currency markers, and ordinary
    positive/negative/zero sections. Conditions, scaling commas, fractions,
    scientific notation and accounting padding deliberately fall back to a warning.
    """
    if number_format in {"General", "@"}:
        return str(value)
    if len(number_format) > 256:
        return None
    sections = []
    start = 0
    quoted = escaped = False
    for index, character in enumerate(number_format):
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == '"':
            quoted = not quoted
        elif character == ";" and not quoted:
            sections.append(number_format[start:index])
            start = index + 1
    if quoted or escaped:
        return None
    sections.append(number_format[start:])
    if len(sections) > 4:
        return None
    selected = sections[1] if value < 0 and len(sections) > 1 else sections[2] if value == 0 and len(sections) > 2 else sections[0]
    selected = re.sub(r"^\[(?:Black|Blue|Cyan|Green|Magenta|Red|White|Yellow)\]", "", selected, flags=re.IGNORECASE)
    literals = []

    def literal(match) -> str:
        quoted_text, escaped_char, currency = match.groups()
        text = quoted_text if quoted_text is not None else escaped_char
        if currency is not None:
            # [$€-407], [$$-409], [$USD-409]; locale controls are not displayed.
            text = re.sub(r"-[0-9A-Fa-f]+$", "", currency)
        literals.append(text or "")
        return chr(0xE000 + len(literals) - 1)

    pattern = re.sub(r'"([^"\n]*)"|\\(.)|\[\$([^\]]*)\]', literal, selected)
    if any(char not in "#0.,%()+-$€£¥￥₹₩ \t" and not 0xE000 <= ord(char) < 0xE000 + len(literals) for char in pattern):
        return None
    match = re.fullmatch(r"([^#0.,]*)(#,##0|0{1,32})(\.0{1,15})?([^#0.,]*)", pattern)
    if not match:
        return None
    prefix, integer, fraction, suffix = match.groups()
    if prefix.count("%") or suffix.count("%") > 1:
        return None
    if (prefix + suffix).count("(") != (prefix + suffix).count(")") or (prefix + suffix).count("(") > 1:
        return None
    precision = len(fraction or "") - 1 if fraction else 0
    # Inspect format syntax before expanding quoted/escaped text. A literal
    # unit such as "US-dollar" or "USD/worker-day" contains a hyphen, but it
    # does not represent the numeric value's negative sign.
    explicit_minus = "-" in prefix + suffix
    negative_parentheses = "(" in prefix and ")" in suffix

    def expand(part: str) -> str:
        return "".join(literals[ord(char) - 0xE000] if 0xE000 <= ord(char) < 0xE000 + len(literals) else char for char in part)

    prefix, suffix = expand(prefix), expand(suffix)
    try:
        amount = Decimal(str(value)).copy_abs()
        if "%" in pattern:
            amount *= 100
        amount = amount.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP)
        rendered = format(amount, f",.{precision}f" if "," in integer else f".{precision}f")
        if "," not in integer:
            whole, dot, decimal = rendered.partition(".")
            rendered = whole.zfill(len(integer)) + dot + decimal
    except (InvalidOperation, ValueError):
        return None
    result = prefix + rendered + suffix
    if value < 0 and not explicit_minus and not negative_parentheses:
        # A color-only negative section loses its meaning in Markdown; retain
        # a minus sign rather than silently turning the numeric fact positive.
        result = "-" + result
    return result


def _xlsx_value(cell, cached_cell, output: _Output) -> str:
    value = cell.value
    if cell.data_type == "f":
        value = cached_cell.value
        if value is None:
            output.warn("XLSX contains formulas without cached results. Formulas were not calculated; recalculate and save in spreadsheet software before trying again.")
            return "[formula not calculated]"
        output.warn("XLSX formulas use only results cached in the file. Check whether the cache is current.")
    if value is None:
        return ""
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        formatted = _xlsx_number(value, cell.number_format)
        if formatted is not None:
            return formatted
        output.warn("XLSX custom number formats are not fully simulated. Review identifiers, currency, and complex formats.")
    return str(value)


def _parse_xlsx(data: bytes, output: _Output) -> None:
    from openpyxl import load_workbook

    _xlsx_preflight(data, output.options)
    def open_workbook(*, data_only: bool):
        with warnings.catch_warnings(record=True) as notices:
            warnings.simplefilter("always")
            workbook = load_workbook(io.BytesIO(data), data_only=data_only, keep_links=False)
        if notices:
            output.warn("The XLSX reader reported unsupported images or extensions that may have been skipped. Save as PDF and review.")
        return workbook

    document = open_workbook(data_only=False)
    cached = None
    try:
        cached = open_workbook(data_only=True)
        if len(document.sheetnames) > output.options.max_pages:
            raise ExtractionError("The XLSX worksheet count exceeds the limit. Split the document.")
        output.document.page_count = len(document.sheetnames)
        output.warn("XLSX is extracted by worksheet grid, including hidden rows and columns. Print layout, conditional formats, pivot tables, comments, and graphics are not rendered.")
        # openpyxl can silently drop unsupported drawings, so disclose them.
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for name in archive.namelist():
                if name.startswith("xl/drawings/") and name.endswith(".xml"):
                    drawing = ElementTree.fromstring(archive.read(name))
                    if any(_local(element) in {"sp", "cxnSp", "grpSp", "graphicFrame"} for element in drawing.iter()):
                        output.warn("XLSX charts, text boxes, and grouped/vector graphics are not rendered. Save as PDF before conversion.")
        for index, name in enumerate(document.sheetnames, 1):
            sheet = document[name]
            output.page = index
            origin = f"xlsx/sheet-{index}"
            output.text(f"## {sheet.title}", origin=origin)
            if not hasattr(sheet, "iter_rows"):
                output.warn("XLSX contains a chart sheet whose chart is not rendered. Save as PDF before conversion.")
                continue
            if sheet.sheet_state != "visible":
                output.warn("XLSX contains a hidden worksheet; its content is also extracted.")
            # Iterating a whole styled rectangle can manufacture millions of
            # empty cells. Only inspect already-loaded OOXML cells to locate
            # the final nonempty row/column, then bound the actual output grid.
            populated = (cell for cell in sheet._cells.values() if cell.value is not None and cell.value != "")
            max_row = max_column = 0
            for cell in populated:
                max_row = max(max_row, cell.row)
                max_column = max(max_column, cell.column)
            if max_row * max_column > output.options.max_table_cells - output.cells:
                raise ExtractionError("The XLSX table range exceeds the cell limit. Reduce the used area or split the document.")
            output.count_cells(max_row * max_column)
            values = [[_xlsx_value(cell, cached[sheet.title].cell(cell.row, cell.column), output) for cell in row]
                      for row in sheet.iter_rows(min_row=1, max_row=max_row, min_col=1, max_col=max_column)] if max_row else []
            if any(any(value for value in row) for row in values):
                output.text(markdown_table(values), origin=origin)
            else:
                output.warn(f"XLSX worksheet {index} has no cell values to extract.")
            if sheet.merged_cells.ranges:
                output.warn("XLSX merged cells retain a value only in the top-left cell. Markdown does not preserve merged-cell styling.")
            if sheet._charts:
                output.warn("XLSX native charts are not rendered or recognized. Save as PDF or images before conversion.")
            for picture_index, picture in enumerate(sheet._images, 1):
                if (picture.format or "").lower() in {"wmf", "emf", "svg"}:
                    output.warn("XLSX contains an unsupported vector image that was not recognized. Save as PDF.")
                    continue
                marker = getattr(picture.anchor, "_from", None)
                anchor = f"row {marker.row + 1}, column {marker.col + 1}" if marker is not None else "cell anchor unavailable"
                output.text(f"### Image {picture_index} ({anchor})", origin=f"{origin}/image-{picture_index}")
                output.image(picture._data(), origin=f"{origin}/image-{picture_index}", context=f"Worksheet {sheet.title}, {anchor}")
                output.warn("XLSX embedded images are emitted after the worksheet table; cell anchors describe their positions.")
    finally:
        document.close()
        if cached is not None:
            cached.close()
