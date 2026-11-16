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
            raise ExtractionError("Office 原生文字超过输出上限，请拆分文档。")
        self.document.blocks.append(Block(kind="text", text=text, page=None if self.auxiliary else page or self.page, origin=origin))

    def image(self, data: bytes, *, origin: str, page: int | None = None, context: str = "", crop=None, rotation: float = 0, flip_h: bool = False, flip_v: bool = False) -> None:
        self.images += 1
        if self.images > self.options.max_images:
            raise ExtractionError("Office 图片数量超过上限，请拆分文档。")
        block = image_block(data, self.options, origin=origin, page=None if self.auxiliary else page or self.page, context=context, purpose="mixed")
        if crop or rotation or flip_h or flip_v:
            # Normalize/limit the image before allocating a cropped/rotated copy.
            with Image.open(io.BytesIO(block.image)) as source:
                picture = source.copy()
            if crop:
                left, top, right, bottom = crop
                if min(crop) < 0:
                    self.warn("图片含向外扩展的裁剪区域，仅保留图片本体；版式需复核。")
                box = (max(0, round(picture.width * left)), max(0, round(picture.height * top)),
                       min(picture.width, round(picture.width * (1 - right))),
                       min(picture.height, round(picture.height * (1 - bottom))))
                if box[0] >= box[2] or box[1] >= box[3]:
                    raise ExtractionError("Office 图片裁剪区域为空或无效。")
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
            raise ExtractionError("Office 图片总大小超过上限，请拆分文档。")
        self.document.blocks.append(block)

    def count_cells(self, count: int) -> None:
        self.cells += count
        if self.cells > self.options.max_table_cells:
            raise ExtractionError("Office 表格单元格数量超过上限，请拆分文档。")

    def next_page(self) -> None:
        if self.auxiliary:
            self.warn("DOCX 附属内容中的分页标记不计入正文页数；未恢复逐页排版。")
            return
        self.page += 1
        if self.page > self.options.max_pages:
            raise ExtractionError("Office 页数超过上限，请拆分文档。")


def parse_office(data: bytes, filename: str, options: Options) -> ParsedDocument:
    """Return ordered native text/image blocks for DOCX, PPTX, or XLSX."""
    if len(data) > options.max_file_bytes:
        raise ExtractionError("Office 文件超过大小上限，请拆分文档。")
    extension = PurePath(filename).suffix.lower().lstrip(".")
    if extension not in {"docx", "pptx", "xlsx"}:
        raise ExtractionError("仅支持 DOCX、PPTX、XLSX；旧版或含宏格式请先另存。")
    if options.max_pages < 1:
        raise ExtractionError("Office 页数上限必须大于零。")
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
        raise ExtractionError(f"无法解析 {extension.upper()}：文件损坏、加密或包含不受支持的结构。") from exc
    return output.document


def _drawing_tokens(element, part, output: _Output) -> Iterator[tuple[str, object]]:
    if any(_local(child) == "anchor" for child in element.iter()):
        output.warn("DOCX 浮动图片按锚点插入，环绕与多栏布局的阅读顺序需复核。")
    if any(_local(child) in {"chart", "relIds", "txbxContent"} for child in element.iter()):
        output.warn("DOCX 图表、SmartArt 或文本框未渲染；此类对象请另存为 PDF 后转换。")
    seen = set()
    for child in element.iter():
        if _local(child) not in {"blip", "imagedata"}:
            continue
        if _attr(child, "link"):
            output.warn("DOCX 含外链图片；未联网下载，请先嵌入图片。")
        relationship_id = _attr(child, "embed") or _attr(child, "id")
        if not relationship_id or relationship_id in seen:
            continue
        seen.add(relationship_id)
        relationship = part.rels.get(relationship_id)
        if relationship is None:
            output.warn("DOCX 图片关系缺失，无法读取该图片。")
            continue
        if relationship.is_external:
            output.warn("DOCX 含外链图片；未联网下载，请先嵌入图片。")
            continue
        image_part = relationship.target_part
        if any(word in image_part.content_type.lower() for word in ("svg", "emf", "wmf")):
            output.warn("DOCX 含 SVG/EMF/WMF 矢量图片，未栅格化；请另存为 PDF 后转换。")
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
        output.warn("DOCX 包含修订：提取当前插入内容，忽略已删除内容。")
        return
    if local in {"ins", "moveTo"}:
        output.warn("DOCX 包含修订：提取当前插入内容，忽略已删除内容。")
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
        output.warn("DOCX 内嵌 OLE 对象未执行或解析；请转换为普通文本、图片或 PDF。")
        return
    if local in {"footnoteReference", "endnoteReference"}:
        if output.docx_extras is not None:
            yield "text", output.docx_extras.reference(local, _attr(element, "id"))
        else:
            output.warn("DOCX 脚注或尾注引用无法解析。")
        return
    if local in {"oMath", "oMathPara"}:
        output.warn("DOCX 公式仅提取可见文本，数学排版未完整保留。")
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
        output.warn("DOCX 列表统一为项目符号，原编号与复杂缩进未完整保留。")
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
                        reference = f"表格图片 {len(pictures) + 1}"
                        pieces.append(f"[{reference}]")
                        pictures.append((payload, f"{origin}/row-{row_index}/col-{column_index}", reference, output.page))
                    elif kind == "page":
                        output.next_page()
                pieces.append("\n")
            if cell.tables:
                output.warn("DOCX 嵌套表格在所属外层表格之后输出，局部阅读顺序需复核。")
            values.append("".join(pieces).strip())
        rows.append(values)
    output.text(markdown_table(rows), origin=origin)
    if pictures:
        output.warn("DOCX 表格内图片以占位符定位，图片识别结果放在该表格之后。")
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
            output.warn("DOCX 附属内容包含未支持的对象或内容控件，请复核。")


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
        label = "脚注" if kind == "footnote" else "尾注"
        if not re.fullmatch(r"\d{1,10}", raw_id) or int(raw_id) > 2_147_483_647:
            self.output.warn("DOCX 脚注/尾注包含无效引用ID，未提取其正文。")
            return f"【{label}引用无效】"
        note_id = int(raw_id)
        key = (kind, note_id)
        if key not in self.references:
            # References themselves are a bounded collection, even if their
            # target notes contain no visible text.
            if len(self.references) >= self.output.options.max_table_cells:
                raise ExtractionError("DOCX 附注引用数量超过资源上限，请拆分文档。")
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
                self.output.warn("DOCX 附注为外部关系，未联网读取。")
                continue
            part = relationship.target_part
            if str(part.partname) in seen_parts:
                continue
            seen_parts.add(str(part.partname))
            root = parse_xml(part.blob)
            if _local(root) != kind + "s":
                self.output.warn("DOCX 附注关系的正文结构不匹配，未提取该部分。")
                continue
            for element in root:
                raw_id = _attr(element, "id")
                if (_local(element) != kind or _attr(element, "type", "normal") != "normal"
                        or not re.fullmatch(r"\d{1,10}", raw_id) or int(raw_id) > 2_147_483_647):
                    continue  # Separator records (including negative IDs) are not notes.
                self.note_records += 1
                if self.note_records > self.output.options.max_table_cells:
                    raise ExtractionError("DOCX 附注记录数量超过资源上限，请拆分文档。")
                note_id = int(raw_id)
                if note_id in notes:
                    notes[note_id] = None
                    self.output.warn("DOCX 附注存在重复ID，无法确定引用对应正文，请复核。")
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
                    self.output.warn("DOCX 含未知页眉/页脚类型，未提取该部分。")
                    continue
                previous[(kind, variant)] = _attr(reference, "id")
            for (kind, variant), relation_id in previous.items():
                if variant == "first" and not section.different_first_page_header_footer:
                    continue
                if variant == "even" and not even_enabled:
                    continue
                relationship = self.document.part.rels.get(relation_id)
                if relationship is None or not relationship.reltype.endswith("/" + kind):
                    self.output.warn("DOCX 页眉/页脚引用关系缺失或类型不匹配，请复核。")
                    continue
                if relationship.is_external:
                    self.output.warn("DOCX 页眉/页脚为外部关系，未联网读取。")
                    continue
                part = relationship.target_part
                key = (kind, str(part.partname))
                if key not in used:
                    used[key] = (part, [])
                scope = f"第{section_index}节/{ {'default': '普通', 'first': '首页', 'even': '偶数页'}[variant]}"
                used[key][1].append(scope)
        for (kind, part_name), (part, scopes) in used.items():
            root = parse_xml(part.blob)
            if _local(root) != {"header": "hdr", "footer": "ftr"}[kind]:
                self.output.warn("DOCX 页眉/页脚正文结构不匹配，未提取该部分。")
                continue
            label = "页眉" if kind == "header" else "页脚"
            self.output.warn("DOCX 页眉/页脚按适用节与类型集中列出，共享内容仅提取一次；未恢复逐页位置或动态域计算。")
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
                label = "脚注" if kind == "footnote" else "尾注"
                origin = f"docx/{kind}-{note_id}"
                self.output.warn("DOCX 脚注/尾注按文件引用ID标注，并按首次引用顺序集中列出；未恢复逐页排版或显示编号。")
                self.output.text(f"## {label} {note_id}", origin=origin)
                note = self._notes(kind).get(note_id)
                if note is None:
                    self.output.warn("DOCX 脚注/尾注引用缺少唯一可用正文，请复核。")
                    self.output.text(f"【{label} {note_id}：正文缺失或引用不唯一】", origin=origin)
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
    output.warn("DOCX 未执行版式渲染；页数仅按显式分页符估计，真实页数与多栏阅读顺序需复核。")
    for part in document.part.package.parts:
        part_name = str(part.partname)
        if "/comments" in part_name:
            output.warn("DOCX 批注未提取。")
    for index, element in enumerate(document.element.body, 1):
        origin = f"docx/body-{index}"
        if _local(element) == "p":
            _docx_paragraph(Paragraph(element, document), output, origin)
            if any(_local(item) == "sectPr" for item in element.iter()):
                output.warn("DOCX 分节分页未渲染，页数仅为显式分页估计。")
        elif _local(element) == "tbl":
            _docx_table(Table(element, document), output, origin)
        elif _local(element) != "sectPr":
            output.warn("DOCX 含未支持的正文对象或内容控件；请另存为普通 DOCX/PDF 后复核。")
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
                output.warn("PPTX 含 SVG/EMF/WMF 矢量图片，未栅格化；请另存为 PDF 后转换。")
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
        output.warn("PPTX 无文本矢量图形、SmartArt、连接线或其他对象未渲染；图解请另存为 PDF。")


def _parse_pptx(data: bytes, output: _Output) -> None:
    from pptx import Presentation

    presentation = Presentation(io.BytesIO(data))
    if len(presentation.slides) > output.options.max_pages:
        raise ExtractionError("PPTX 幻灯片数量超过上限，请拆分文档。")
    output.document.page_count = len(presentation.slides)
    output.warn("PPTX 按幻灯片内形状从上到下、从左到右排列；重叠、多栏和组合图形顺序需复核。")
    output.warn("PPTX 母版、背景、备注、动画及对象间关系未渲染。")
    for index, slide in enumerate(presentation.slides, 1):
        output.page = index
        output.text(f"## 幻灯片 {index}", origin=f"pptx/slide-{index}")
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
                raise ExtractionError("XLSX 工作表数量超过上限，请拆分文档。")
            with archive.open(name) as source:
                for _, element in ElementTree.iterparse(source, events=("end",)):
                    if _local(element) == "c":
                        count += 1
                        if count > options.max_table_cells:
                            raise ExtractionError("XLSX 单元格数量超过上限，请拆分文档。")
                    elif _local(element) == "mergeCell":
                        # openpyxl expands merge ranges into MergedCell objects,
                        # even when only the top-left cell is serialized.
                        left, top, right, bottom = range_boundaries(element.get("ref", ""))
                        if None in (left, top, right, bottom) or left > right or top > bottom:
                            raise ExtractionError("XLSX 合并单元格范围无效。")
                        merged_cells += (right - left + 1) * (bottom - top + 1)
                        if merged_cells > options.max_table_cells:
                            raise ExtractionError("XLSX 合并单元格展开范围超过上限，请拆分文档。")
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
            output.warn("XLSX 存在缺少缓存结果的公式，未执行计算；请用表格软件重算并保存后重试。")
            return "[未计算公式]"
        output.warn("XLSX 公式仅读取文件内缓存结果，缓存是否为最新值需复核。")
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
        output.warn("XLSX 自定义数字格式未完整模拟；标识符、货币和复杂格式请复核。")
    return str(value)


def _parse_xlsx(data: bytes, output: _Output) -> None:
    from openpyxl import load_workbook

    _xlsx_preflight(data, output.options)
    def open_workbook(*, data_only: bool):
        with warnings.catch_warnings(record=True) as notices:
            warnings.simplefilter("always")
            workbook = load_workbook(io.BytesIO(data), data_only=data_only, keep_links=False)
        if notices:
            output.warn("XLSX 读取器报告部分图片或扩展功能不受支持，可能已跳过；请另存为 PDF 后复核。")
        return workbook

    document = open_workbook(data_only=False)
    cached = None
    try:
        cached = open_workbook(data_only=True)
        if len(document.sheetnames) > output.options.max_pages:
            raise ExtractionError("XLSX 工作表数量超过上限，请拆分文档。")
        output.document.page_count = len(document.sheetnames)
        output.warn("XLSX 按工作表网格提取，隐藏行列也会包含；打印版式、条件格式、数据透视表、批注和图形未渲染。")
        # openpyxl can silently drop unsupported drawings, so disclose them.
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for name in archive.namelist():
                if name.startswith("xl/drawings/") and name.endswith(".xml"):
                    drawing = ElementTree.fromstring(archive.read(name))
                    if any(_local(element) in {"sp", "cxnSp", "grpSp", "graphicFrame"} for element in drawing.iter()):
                        output.warn("XLSX 图表、文本框或组合/矢量图形未渲染，请另存为 PDF 后转换。")
        for index, name in enumerate(document.sheetnames, 1):
            sheet = document[name]
            output.page = index
            origin = f"xlsx/sheet-{index}"
            output.text(f"## {sheet.title}", origin=origin)
            if not hasattr(sheet, "iter_rows"):
                output.warn("XLSX 含独立图表工作表，图表未渲染，请另存为 PDF 后转换。")
                continue
            if sheet.sheet_state != "visible":
                output.warn("XLSX 包含隐藏工作表，本次也提取其内容。")
            # Iterating a whole styled rectangle can manufacture millions of
            # empty cells. Only inspect already-loaded OOXML cells to locate
            # the final nonempty row/column, then bound the actual output grid.
            populated = (cell for cell in sheet._cells.values() if cell.value is not None and cell.value != "")
            max_row = max_column = 0
            for cell in populated:
                max_row = max(max_row, cell.row)
                max_column = max(max_column, cell.column)
            if max_row * max_column > output.options.max_table_cells - output.cells:
                raise ExtractionError("XLSX 表格范围超过单元格上限，请缩小使用区域或拆分文档。")
            output.count_cells(max_row * max_column)
            values = [[_xlsx_value(cell, cached[sheet.title].cell(cell.row, cell.column), output) for cell in row]
                      for row in sheet.iter_rows(min_row=1, max_row=max_row, min_col=1, max_col=max_column)] if max_row else []
            if any(any(value for value in row) for row in values):
                output.text(markdown_table(values), origin=origin)
            else:
                output.warn(f"XLSX 第 {index} 个工作表没有可提取的单元格值。")
            if sheet.merged_cells.ranges:
                output.warn("XLSX 合并单元格仅在左上角保留值，Markdown 不保留合并样式。")
            if sheet._charts:
                output.warn("XLSX 原生图表未渲染或识别，请另存为 PDF 或图片后转换。")
            for picture_index, picture in enumerate(sheet._images, 1):
                if (picture.format or "").lower() in {"wmf", "emf", "svg"}:
                    output.warn("XLSX 含不支持的矢量图片，未识别；请另存为 PDF。")
                    continue
                marker = getattr(picture.anchor, "_from", None)
                anchor = f"第 {marker.row + 1} 行、第 {marker.col + 1} 列" if marker is not None else "未提供单元格锚点"
                output.text(f"### 图片 {picture_index}（{anchor}）", origin=f"{origin}/image-{picture_index}")
                output.image(picture._data(), origin=f"{origin}/image-{picture_index}", context=f"工作表 {sheet.title}，{anchor}")
                output.warn("XLSX 嵌入图片在工作表表格之后输出，图片位置通过单元格锚点描述。")
    finally:
        document.close()
        if cached is not None:
            cached.close()
