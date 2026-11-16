"""Exercise actual OOXML files; no model/network is needed for native text."""
from __future__ import annotations

import io
import zipfile
from dataclasses import replace
from xml.etree import ElementTree

import pytest
from docx import Document
from docx.enum.text import WD_BREAK
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches as DocxInches
from openpyxl import Workbook
from openpyxl.drawing.image import Image as SheetImage
from openpyxl.styles import PatternFill
from PIL import Image
from pptx import Presentation
from pptx.util import Inches

from document_md.office import parse_office
from document_md.types import ExtractionError, Options


def png() -> bytes:
    image = Image.new("RGB", (60, 40), "red")
    image.paste("blue", (30, 0, 60, 40))
    result = io.BytesIO()
    image.save(result, "PNG")
    return result.getvalue()


def saved(document) -> bytes:
    result = io.BytesIO()
    document.save(result)
    return result.getvalue()


def texts(document) -> str:
    return "\n".join(block.text for block in document.blocks if block.kind == "text")


def rewrite_zip(data: bytes, change) -> bytes:
    result = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as original, zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as edited:
        for entry in original.infolist():
            edited.writestr(entry.filename, change(entry.filename, original.read(entry.filename)))
    return result.getvalue()


def test_docx_keeps_inline_image_between_native_chinese_runs_and_table():
    document = Document()
    document.add_heading("检修说明", level=1)
    paragraph = document.add_paragraph("图片之前：先断电。")
    paragraph.add_run().add_picture(io.BytesIO(png()), width=DocxInches(1))
    paragraph.add_run("图片之后：再维护。")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "编号", "期限"
    table.cell(1, 0).text, table.cell(1, 1).text = "0007", "24个月"
    document.add_paragraph("正文结尾")

    parsed = parse_office(saved(document), "检修.docx", Options())

    assert [block.kind for block in parsed.blocks] == ["text", "text", "image", "text", "text", "text"]
    assert parsed.blocks[0].text == "# 检修说明"
    assert parsed.blocks[1].text == "图片之前：先断电。"
    assert parsed.blocks[3].text == "图片之后：再维护。"
    assert "| 0007 | 24个月 |" in parsed.blocks[4].text
    assert parsed.blocks[5].text == "正文结尾"
    assert len([block for block in parsed.blocks if block.kind == "image"]) == 1
    assert parsed.page_count == 1
    assert any("explicit page break" in item for item in parsed.warnings)


def test_docx_table_image_is_located_and_merged_text_not_duplicated():
    document = Document()
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).merge(table.cell(0, 1)).text = "合并标题"
    paragraph = table.cell(1, 0).paragraphs[0]
    paragraph.add_run("安装位置")
    paragraph.add_run().add_picture(io.BytesIO(png()), width=DocxInches(1))
    parsed = parse_office(saved(document), "table.docx", Options())

    assert texts(parsed).count("合并标题") == 1
    assert "[Table image 1]" in parsed.blocks[0].text
    assert parsed.blocks[1].text == "### Table image 1"
    assert parsed.blocks[2].kind == "image"
    assert parsed.blocks[2].origin.endswith("row-2/col-1")
    assert any("placeholders" in item for item in parsed.warnings)


def test_docx_explicit_page_breaks_and_image_limits_fail():
    document = Document()
    paragraph = document.add_paragraph("第一页")
    paragraph.add_run().add_break(WD_BREAK.PAGE)
    paragraph.add_run("第二页")
    data = saved(document)
    parsed = parse_office(data, "pages.docx", Options())
    assert parsed.page_count == 2
    assert [block.page for block in parsed.blocks] == [1, 2]
    with pytest.raises(ExtractionError, match="page count"):
        parse_office(data, "pages.docx", replace(Options(), max_pages=1))

    document = Document()
    document.add_picture(io.BytesIO(png()))
    document.add_picture(io.BytesIO(png()))
    with pytest.raises(ExtractionError, match="image count"):
        parse_office(saved(document), "images.docx", replace(Options(), max_images=1))


def test_docx_external_image_is_not_downloaded_and_is_reported():
    document = Document()
    document.add_paragraph("外链图片")
    document.add_picture(io.BytesIO(png()))

    def externalize(name, value):
        if name == "word/_rels/document.xml.rels":
            root = ElementTree.fromstring(value)
            for relation in root:
                if relation.get("Type", "").endswith("/image"):
                    relation.set("Target", "https://example.invalid/never-download.png")
                    relation.set("TargetMode", "External")
            return ElementTree.tostring(root)
        return value

    parsed = parse_office(rewrite_zip(saved(document), externalize), "external.docx", Options())
    assert not any(block.kind == "image" for block in parsed.blocks)
    assert any("not downloaded" in warning for warning in parsed.warnings)


def test_docx_native_text_and_table_cell_limits_fail():
    document = Document()
    document.add_paragraph("一二三四五六七八九十")
    with pytest.raises(ExtractionError, match="native text"):
        parse_office(saved(document), "long.docx", replace(Options(), max_output_chars=5))
    document = Document()
    document.add_table(rows=2, cols=3)
    with pytest.raises(ExtractionError, match="cell"):
        parse_office(saved(document), "table.docx", replace(Options(), max_table_cells=5))


def test_docx_list_style_inheritance_and_explicit_cancellation():
    document = Document()
    document.add_paragraph("STYLE-BULLET 保留项目符号", style="List Bullet")
    inherited = document.styles.add_style("Inherited Step", WD_STYLE_TYPE.PARAGRAPH)
    inherited.base_style = document.styles["List Number"]
    document.add_paragraph("INHERITED-NUMBER 保留列表结构", style=inherited)
    level_only = document.add_paragraph("LEVEL-ONLY 仍继承编号", style="List Bullet")
    level = OxmlElement("w:ilvl")
    level.set(qn("w:val"), "1")
    level_only._p.get_or_add_pPr().get_or_add_numPr().append(level)

    cancelled = document.add_paragraph("DIRECT-ZERO 普通正文", style="List Bullet")
    num_id = OxmlElement("w:numId")
    num_id.set(qn("w:val"), "0")
    cancelled._p.get_or_add_pPr().get_or_add_numPr().append(num_id)
    cancelled_style = document.styles.add_style("Cancelled Step", WD_STYLE_TYPE.PARAGRAPH)
    cancelled_style.base_style = document.styles["List Bullet"]
    style_num_id = OxmlElement("w:numId")
    style_num_id.set(qn("w:val"), "0")
    cancelled_style.element.get_or_add_pPr().get_or_add_numPr().append(style_num_id)
    document.add_paragraph("STYLE-ZERO 普通正文", style=cancelled_style)
    enabled = document.add_paragraph("DIRECT-OVERRIDE 直接编号优先", style=cancelled_style)
    enabled_id = OxmlElement("w:numId")
    enabled_id.set(qn("w:val"), "1")
    enabled._p.get_or_add_pPr().get_or_add_numPr().append(enabled_id)

    parsed = parse_office(saved(document), "lists.docx", Options())
    assert [block.text for block in parsed.blocks] == [
        "- STYLE-BULLET 保留项目符号", "- INHERITED-NUMBER 保留列表结构", "- LEVEL-ONLY 仍继承编号",
        "DIRECT-ZERO 普通正文", "STYLE-ZERO 普通正文", "- DIRECT-OVERRIDE 直接编号优先",
    ]
    assert sum("lists are normalized" in warning for warning in parsed.warnings) == 1


def test_pptx_sorts_shapes_recurses_groups_and_reads_table_and_cropped_image():
    document = Presentation()
    slide = document.slides.add_slide(document.slide_layouts[6])
    bottom = slide.shapes.add_textbox(Inches(1), Inches(5), Inches(5), Inches(1))
    bottom.text = "最后一段"
    top = slide.shapes.add_textbox(Inches(1), Inches(0.3), Inches(5), Inches(1))
    top.text = "最先一段"
    picture = slide.shapes.add_picture(io.BytesIO(png()), Inches(1), Inches(1.5))
    picture.crop_left = 0.5
    table = slide.shapes.add_table(2, 2, Inches(1), Inches(2.5), Inches(5), Inches(1)).table
    table.cell(0, 0).text, table.cell(0, 1).text = "项目", "数量"
    table.cell(1, 0).text, table.cell(1, 1).text = "电池", "2"
    group = slide.shapes.add_group_shape()
    child = group.shapes.add_textbox(Inches(1), Inches(4), Inches(5), Inches(0.5))
    child.text = "组合文本"

    parsed = parse_office(saved(document), "维修.pptx", Options())
    assert parsed.page_count == 1
    assert parsed.blocks[1].text == "最先一段"
    assert parsed.blocks[2].kind == "image"
    assert "| 电池 | 2 |" in parsed.blocks[3].text
    assert parsed.blocks[4].text == "组合文本"
    assert parsed.blocks[5].text == "最后一段"
    with Image.open(io.BytesIO(parsed.blocks[2].image)) as image:
        assert image.size == (30, 40)
        assert image.getpixel((10, 10)) == (0, 0, 255)
    assert sum(block.kind == "image" for block in parsed.blocks) == 1


def test_pptx_without_images_has_only_native_blocks_and_enforces_slides():
    document = Presentation()
    for value in ("第一张", "第二张"):
        slide = document.slides.add_slide(document.slide_layouts[6])
        slide.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(1)).text = value
    data = saved(document)
    parsed = parse_office(data, "native.pptx", Options())
    assert parsed.page_count == 2
    assert all(block.kind == "text" for block in parsed.blocks)
    assert "第一张" in texts(parsed) and "第二张" in texts(parsed)
    with pytest.raises(ExtractionError, match="slide count"):
        parse_office(data, "native.pptx", replace(Options(), max_pages=1))


def test_xlsx_keeps_identifiers_formula_status_and_embedded_image():
    document = Workbook()
    sheet = document.active
    sheet.title = "维修数据"
    sheet.append(["编号", "次数", "总数", "比例"])
    sheet.append([7, 2, "=SUM(B2:B2)", 0.25])
    sheet["A2"].number_format = "000000"
    sheet["D2"].number_format = "0.0%"
    sheet.append(["000008", 3, None, None])
    sheet.add_image(SheetImage(io.BytesIO(png())), "F4")

    parsed = parse_office(saved(document), "数据.xlsx", Options())
    output = texts(parsed)
    assert "000007" in output and "000008" in output
    assert "25.0%" in output
    assert "[formula not calculated]" in output
    assert any("without cached results" in warning for warning in parsed.warnings)
    assert sum(block.kind == "image" for block in parsed.blocks) == 1
    assert "row 4, column 6" in parsed.blocks[-1].context
    assert parsed.blocks[-2].text == "### Image 1 (row 4, column 6)"
    assert parsed.page_count == 1


def test_xlsx_formula_uses_saved_cache_without_recalculation():
    document = Workbook()
    document.active.append(["总数"])
    document.active.append(["=1+2"])

    def set_cache(name, value):
        if name == "xl/worksheets/sheet1.xml":
            return value.replace(b"<f>1+2</f><v></v>", b"<f>1+2</f><v>99</v>")
        return value

    parsed = parse_office(rewrite_zip(saved(document), set_cache), "cached.xlsx", Options())
    assert "| 99 |" in texts(parsed)
    assert "| 3 |" not in texts(parsed)
    assert any("whether the cache is current" in warning for warning in parsed.warnings)


def test_xlsx_cell_limits_cover_stored_cells_and_sparse_ranges():
    document = Workbook()
    document.active.append([1, 2, 3])
    with pytest.raises(ExtractionError, match="cell"):
        parse_office(saved(document), "many.xlsx", replace(Options(), max_table_cells=2))
    document = Workbook()
    document.active["Z100"] = "稀疏远端值"
    with pytest.raises(ExtractionError, match="table range"):
        parse_office(saved(document), "sparse.xlsx", replace(Options(), max_table_cells=50))


def test_xlsx_trims_style_only_tail_but_keeps_internal_blanks_and_image_anchor():
    document = Workbook()
    sheet = document.active
    sheet["A1"] = "开始"
    sheet["C3"] = "结尾"
    sheet["XFD1048576"].fill = PatternFill(fill_type="solid", fgColor="FFFFFF")
    sheet.add_image(SheetImage(io.BytesIO(png())), "L20")
    parsed = parse_office(saved(document), "styled-tail.xlsx", replace(Options(), max_table_cells=9))
    table = next(block.text for block in parsed.blocks if block.kind == "text" and block.text.startswith("|"))
    assert table == "| 开始 |  |  |\n| --- | --- | --- |\n|  |  |  |\n|  |  | 结尾 |"
    assert parsed.blocks[-2].text == "### Image 1 (row 20, column 12)"
    assert parsed.blocks[-1].kind == "image"


def test_xlsx_trim_keeps_false_zero_and_formula_and_stored_cell_limit():
    document = Workbook()
    sheet = document.active
    sheet.append(["A", "B", "C"])
    sheet.append([False, 0, "=1+1"])
    sheet["Z100"].fill = PatternFill(fill_type="solid", fgColor="FFFFFF")
    data = saved(document)
    parsed = parse_office(data, "values.xlsx", Options())
    assert "| FALSE | 0 | [formula not calculated] |" in texts(parsed)
    # The preflight still counts serialized style-only cells before loading.
    with pytest.raises(ExtractionError, match="cell"):
        parse_office(data, "values.xlsx", replace(Options(), max_table_cells=6))


def test_xlsx_style_trimming_does_not_bypass_merged_cell_allocation_limit(monkeypatch):
    import openpyxl
    document = Workbook()
    document.active["A1"] = "标题"
    document.active.merge_cells("A1:Z100")
    data = saved(document)

    def must_not_load(*args, **kwargs):
        pytest.fail("oversized merged ranges must fail before workbook allocation")

    monkeypatch.setattr(openpyxl, "load_workbook", must_not_load)
    with pytest.raises(ExtractionError, match="merged-cell"):
        parse_office(data, "merged.xlsx", replace(Options(), max_table_cells=50))


@pytest.mark.parametrize("value,number_format,display", [
    (1250.5, '"$"#,##0.00', "$1,250.50"),
    (-1250.5, '"$"#,##0.00', "-$1,250.50"),
    (-1250.5, '$#,##0.00;[Red]($#,##0.00)', "($1,250.50)"),
    (-1250.5, '"$"#,##0.00;[Red]"$"#,##0.00', "-$1,250.50"),
    (1250.5, '#,##0.00 "USD"', "1,250.50 USD"),
    (1250.5, '[$€-407]#,##0.00', "€1,250.50"),
    (1250.5, '[$¥-804]#,##0.00', "¥1,250.50"),
    (1250.5, r'\$#,##0.00', "$1,250.50"),
    (2.675, '$0.00', "$2.68"),
    (0, '$0.00;($0.00);$0.00', "$0.00"),
    (-12, '00000', "-00012"),
    (1234.567, '#,##0.00', "1,234.57"),
    (-12.3, '0.00 "US-dollar"', "-12.30 US-dollar"),
    (-12.3, '"US-dollar "0.00', "-US-dollar 12.30"),
    (-12.3, '0.00 "USD/worker-day"', "-12.30 USD/worker-day"),
    (-12.3, r'0.00 "USD/worker"\-"day"', "-12.30 USD/worker-day"),
])
def test_xlsx_common_currency_formats_keep_units_grouping_precision_and_sign(value, number_format, display):
    document = Workbook()
    cell = document.active["A1"]
    cell.value, cell.number_format = value, number_format
    parsed = parse_office(saved(document), "currency.xlsx", Options())
    assert f"| {display} |" in texts(parsed)
    assert not any("custom number formats" in warning for warning in parsed.warnings)


def test_xlsx_complex_number_format_keeps_raw_value_and_warns():
    document = Workbook()
    cell = document.active["A1"]
    cell.value = 1250.5
    cell.number_format = '_($* #,##0.00_);_($* (#,##0.00);_($* "-"??_)'
    parsed = parse_office(saved(document), "accounting.xlsx", Options())
    assert "| 1250.5 |" in texts(parsed)
    assert any("custom number formats" in warning for warning in parsed.warnings)


def test_xlsx_sheet_limit_and_hidden_sheet_are_explicit():
    document = Workbook()
    document.active["A1"] = "可见"
    hidden = document.create_sheet("隐藏")
    hidden["A1"] = "隐藏内容"
    hidden.sheet_state = "hidden"
    data = saved(document)
    parsed = parse_office(data, "hidden.xlsx", Options())
    assert "隐藏内容" in texts(parsed)
    assert any("hidden worksheet" in warning for warning in parsed.warnings)
    with pytest.raises(ExtractionError, match="worksheet count"):
        parse_office(data, "hidden.xlsx", replace(Options(), max_pages=1))


def test_xlsx_unsupported_image_drop_is_reported_to_caller():
    document = Workbook()
    document.active["A1"] = "保留的文字"
    document.active.add_image(SheetImage(io.BytesIO(png())), "A2")

    def corrupt_image(name, value):
        return b"invalid image" if name == "xl/media/image1.png" else value

    parsed = parse_office(rewrite_zip(saved(document), corrupt_image), "image.xlsx", Options())
    assert "保留的文字" in texts(parsed)
    assert any("may have been skipped" in warning for warning in parsed.warnings)


def test_actual_docx_conversion_calls_model_for_each_image_only_and_keeps_labels():
    from document_md.engine import convert_document
    from document_md.types import VisionResult

    document = Document()
    document.add_paragraph("原生中文无需视觉模型")
    table = document.add_table(rows=1, cols=1)
    table.cell(0, 0).paragraphs[0].add_run().add_picture(io.BytesIO(png()))
    calls = []

    def vision(block):
        calls.append(block)
        return VisionResult(markdown="图片内容：红蓝色块")

    result = convert_document(saved(document), "model.docx", vision, Options())
    assert len(calls) == result.stats["vision_calls"] == 1
    assert result.markdown.count("原生中文无需视觉模型") == 1
    assert "[Table image 1]" in result.markdown
    assert "### Table image 1\n\n图片内容：红蓝色块" in result.markdown


def test_rejects_non_ooxml_invalid_archives_and_oversize_file():
    with pytest.raises(ExtractionError, match="Only"):
        parse_office(b"", "old.doc", Options())
    with pytest.raises(ExtractionError, match="archive"):
        parse_office(b"not a zip", "bad.docx", Options())
    with pytest.raises(ExtractionError, match="file exceeds"):
        parse_office(b"1234", "large.xlsx", replace(Options(), max_file_bytes=3))
