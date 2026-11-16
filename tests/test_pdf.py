from dataclasses import replace
from io import BytesIO
from pathlib import Path

import pytest
import pypdfium2 as pdfium
import reportlab
from PIL import Image, ImageDraw
from reportlab.lib.pdfencrypt import StandardEncryption
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from document_md.pdf import PDFIUM_LOCK, parse_pdf
from document_md.types import ExtractionError, Options


def _pdf(draw, *, encrypt=None):
    stream = BytesIO()
    pdf = canvas.Canvas(stream, pagesize=(400, 600), encrypt=encrypt)
    draw(pdf)
    pdf.save()
    return stream.getvalue()


def _picture(color="red", size=(150, 80)):
    image = Image.new("RGB", size, color)
    ImageDraw.Draw(image).text((5, 5), "IMAGE TEXT", fill="black")
    return image


def test_chinese_native_text_image_and_scanned_page_are_not_ocr_twice(monkeypatch):
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))

    def draw(pdf):
        pdf.setFont("STSong-Light", 14)
        pdf.drawString(30, 550, "原生中文标题")
        pdf.drawImage(ImageReader(_picture()), 30, 350, 150, 80)
        pdf.drawString(30, 300, "图后文字")
        pdf.showPage()
        # Two scan tiles still generate just one vision page.
        pdf.drawImage(ImageReader(_picture("green", (400, 300))), 0, 300, 400, 300)
        pdf.drawImage(ImageReader(_picture("blue", (400, 300))), 0, 0, 400, 300)
        pdf.showPage()

    renders = []
    original_render = pdfium.PdfPage.render

    def record_render(page, *args, **kwargs):
        renders.append(kwargs.get("crop"))
        return original_render(page, *args, **kwargs)

    monkeypatch.setattr(pdfium.PdfPage, "render", record_render)
    result = parse_pdf(_pdf(draw), "mixed.pdf", Options())

    assert result.page_count == 2
    assert [block.kind for block in result.blocks] == ["text", "image", "text", "image"]
    text = "\n".join(block.text for block in result.blocks)
    assert text.count("原生中文标题") == 1
    assert text.count("图后文字") == 1
    first_image, scan_page = result.blocks[1], result.blocks[3]
    assert first_image.page == 1 and first_image.origin == "pdf-image-region"
    assert first_image.bbox == pytest.approx((30, 170, 180, 250))
    assert scan_page.page == 2 and scan_page.origin == "pdf-page"
    assert scan_page.bbox == (0, 0, 400, 600)
    assert len(renders) == 2
    assert renders[0] == pytest.approx((30, 350, 220, 170))
    assert renders[1] == (0, 0, 0, 0)


def test_native_whitespace_matches_pdfium_for_short_glyphs_and_cjk_digits():
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    expected = [
        "Docling verification: native text",
        "NATIVE-CHECK-173: The warranty is 24 months.",
        "原生文字验证：设备保修期为24个月。",
        "The picture below contains a separate test code:",
        "AFTER-IMAGE-395: This paragraph follows the picture.",
        "Keep  two spaces and digits 173 24.",
    ]

    def draw(pdf):
        for index, line in enumerate(expected):
            pdf.setFont("STSong-Light" if index == 2 else "Helvetica", 14 if index == 2 else 12)
            pdf.drawString(30, 550 - index * 30, line)
        pdf.showPage()

    data = _pdf(draw)
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(data)
        try:
            page = document[0]
            try:
                textpage = page.get_textpage()
                try:
                    reference = textpage.get_text_range(force_this=True).splitlines()
                finally:
                    textpage.close()
            finally:
                page.close()
        finally:
            document.close()
    assert reference[:-1] == expected[:-1]
    # PDFium itself normalizes consecutive space glyphs; the parser should
    # preserve its text stream, rather than invent spacing from glyph boxes.
    assert reference[-1] == "Keep two spaces and digits 173 24."
    result = parse_pdf(data, "native-spacing.pdf", Options())
    actual = "\n".join(block.text for block in result.blocks if block.kind == "text").splitlines()
    assert actual == reference


def test_punctuation_uses_text_baseline_instead_of_tight_ink_centers():
    font = Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"
    pdfmetrics.registerFont(TTFont("PunctuationRegression", str(font)))
    sentence = "Picture, temperature, and maintenance; amount -12.50, code 00123."

    def draw(pdf):
        pdf.setPageSize((600, 400))
        pdf.setFont("PunctuationRegression", 12)
        pdf.drawString(25, 300, sentence)
        pdf.drawString(25, 240, "Next paragraph stays separate.")
        pdf.showPage()

    result = parse_pdf(_pdf(draw), "punctuation.pdf", Options(max_images=0))
    assert [block.text for block in result.blocks] == [sentence, "Next paragraph stays separate."]


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_page_rotation_preserves_logical_text_image_text_order(rotation):
    def draw(pdf):
        pdf.drawString(30, 550, "BEFORE THE IMAGE")
        pdf.drawImage(ImageReader(_picture()), 30, 300, 150, 80)
        pdf.drawString(30, 200, "AFTER THE IMAGE")
        pdf.showPage()

    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(_pdf(draw))
        try:
            page = document[0]
            try:
                page.set_rotation(rotation)
            finally:
                page.close()
            output = BytesIO()
            document.save(output)
        finally:
            document.close()
    result = parse_pdf(output.getvalue(), "rotated-order.pdf", Options())
    assert [block.kind for block in result.blocks] == ["text", "image", "text"]
    assert result.blocks[0].text == "BEFORE THE IMAGE"
    assert result.blocks[-1].text == "AFTER THE IMAGE"


def test_supported_column_gutter_reads_left_column_then_right_with_spanning_title():
    def draw(pdf):
        pdf.setPageSize((600, 700))
        pdf.setFont("Helvetica", 12)
        pdf.drawString(25, 650, "A full width heading above two independently flowing columns")
        for index in range(3):
            y = 550 - index * 120
            # Source objects are deliberately interleaved row by row.
            pdf.drawString(25, y, f"LEFT-{index + 1}")
            pdf.drawString(325, y, f"RIGHT-{index + 1}")
            pdf.drawString(25, y - 30, f"Left paragraph number {index + 1}.")
            pdf.drawString(325, y - 30, f"Right paragraph number {index + 1}.")
        pdf.drawString(25, 50, "FULL PAGE FOOTER")
        pdf.showPage()

    result = parse_pdf(_pdf(draw), "columns.pdf", Options(max_images=0))
    text = "\n".join(block.text for block in result.blocks)
    expected_order = ["LEFT-1", "LEFT-2", "LEFT-3", "RIGHT-1", "RIGHT-2", "RIGHT-3", "FULL PAGE FOOTER"]
    assert [text.index(marker) for marker in expected_order] == sorted(text.index(marker) for marker in expected_order)
    assert text.startswith("A full width heading")
    assert any("阅读顺序" in warning for warning in result.warnings)


def test_short_aligned_record_rows_keep_key_value_association():
    rows = [("Name", "Alice Smith"), ("Department", "Finance"),
            ("Location", "Building C"), ("Manager", "Bob Jones")]

    def draw(pdf):
        pdf.setPageSize((600, 800))
        pdf.drawString(40, 750, "Employee record")
        for index, (key, value) in enumerate(rows):
            pdf.drawString(40, 680 - index * 40, key)
            pdf.drawString(300, 680 - index * 40, value)
        pdf.showPage()

    result = parse_pdf(_pdf(draw), "record.pdf", Options(max_images=0))
    text = "\n".join(block.text for block in result.blocks)
    expected = [item for row in rows for item in row]
    positions = [text.index(item) for item in expected]
    assert positions == sorted(positions)


def test_unequal_column_heights_do_not_move_longer_column_tail_after_right():
    def draw(pdf):
        pdf.setPageSize((600, 800))
        pdf.setFont("Helvetica", 10)
        pdf.drawString(40, 750, "Uneven newspaper columns")
        for index in range(6):
            pdf.drawString(40, 700 - index * 22,
                           f"L{index + 1}: This is the continuous left paragraph line.")
        for index in range(3):
            pdf.drawString(330, 700 - index * 22,
                           f"R{index + 1}: This is the separate right paragraph.")
        pdf.drawString(40, 80, "Separated page footer")
        pdf.showPage()

    result = parse_pdf(_pdf(draw), "unequal-columns.pdf", Options(max_images=0))
    text = "\n".join(block.text for block in result.blocks)
    expected = [f"L{i}:" for i in range(1, 7)] + [f"R{i}:" for i in range(1, 4)] + ["Separated page footer"]
    positions = [text.index(item) for item in expected]
    assert positions == sorted(positions)
    assert text.startswith("Uneven newspaper columns")


def test_native_grid_multiline_cells_follow_visual_not_paint_order():
    def draw(pdf):
        pdf.setPageSize((600, 800))
        for x in [50, 250, 450]:
            pdf.line(x, 400, x, 700)
        for y in [400, 550, 700]:
            pdf.line(50, y, 450, y)
        for x, baseline, label in [(60, 580, "H"), (260, 580, "V"),
                                   (60, 430, "A"), (260, 430, "B")]:
            pdf.drawString(x, baseline, f"{label} bottom")
            pdf.drawString(x, baseline + 40, f"{label} top")
        pdf.showPage()

    result = parse_pdf(_pdf(draw), "multiline-table.pdf", Options(max_images=0))
    assert len(result.blocks) == 1
    assert result.blocks[0].text == (
        "| H top<br>H bottom | V top<br>V bottom |\n| --- | --- |\n"
        "| A top<br>A bottom | B top<br>B bottom |")


def test_vertical_headers_do_not_rotate_horizontal_page_or_split_words():
    headers = ["RevenueGrowthRate", "OperatingExpenses", "CustomerRetention", "RegionalUnitSales"]

    def draw(pdf):
        pdf.setPageSize((600, 800))
        pdf.drawString(50, 750, "Metrics")
        for x in [50, 150, 250, 350, 450]:
            pdf.line(x, 400, x, 700)
        for y in [400, 450, 700]:
            pdf.line(50, y, 450, y)
        for index, (header, value) in enumerate(zip(headers, ["12", "34", "56", "78"])):
            x = 100 + index * 100
            pdf.saveState()
            pdf.translate(x, 480)
            pdf.rotate(90)
            pdf.drawString(0, 0, header)
            pdf.restoreState()
            pdf.drawString(x, 420, value)
        pdf.showPage()

    result = parse_pdf(_pdf(draw), "vertical-table.pdf", Options(max_images=0))
    assert result.blocks[0].text == "Metrics"
    assert result.blocks[1].origin == "pdf-native-table"
    assert result.blocks[1].text == (
        "| " + " | ".join(headers) + " |\n| --- | --- | --- | --- |\n| 12 | 34 | 56 | 78 |")
    assert any("多方向文字" in warning for warning in result.warnings)


@pytest.mark.parametrize("angle", [90, 180, 270])
def test_uniform_rotated_source_text_keeps_words_and_logical_order(angle):
    def draw(pdf):
        pdf.setPageSize((600, 600))
        pdf.translate(300, 300)
        pdf.rotate(angle)
        pdf.drawString(-100, 100, "FIRST UPPER LINE")
        pdf.drawString(-100, -100, "SECOND LOWER LINE")
        pdf.showPage()

    result = parse_pdf(_pdf(draw), "rotated-source.pdf", Options(max_images=0))
    assert [block.text for block in result.blocks] == ["FIRST UPPER LINE", "SECOND LOWER LINE"]


def _draw_native_grid(pdf, *, incomplete=False, overlay=False, annotation=False):
    pdf.setFont("Helvetica", 12)
    rows = [["Item", "Value"], ["Part A", "00123"], ["Part B", "-12.50 kg"]]
    xs, ys = [30, 180, 370], [500, 450, 400, 350]
    for row_index, row in enumerate(rows):
        for col_index, value in enumerate(row):
            pdf.drawString(xs[col_index] + 10, ys[row_index] - 30, value)
    for y in ys:
        pdf.line(xs[0], y, xs[-1], y)
    for column_index, x in enumerate(xs):
        pdf.line(x, ys[0], x, ys[1] if incomplete and column_index == 1 else ys[-1])
    if overlay:
        pdf.drawImage(ImageReader(_picture(size=(40, 20))), 200, 405, 40, 20)
    if annotation:
        pdf.textAnnotation("Cell comment", Rect=(210, 410, 240, 435))
    pdf.showPage()


def test_complete_native_grid_preserves_cells_and_uses_no_vision():
    result = parse_pdf(_pdf(_draw_native_grid), "native-table.pdf", Options(max_images=0))
    assert len(result.blocks) == 1
    assert result.blocks[0].origin == "pdf-native-table"
    assert result.blocks[0].text == "| Item | Value |\n| --- | --- |\n| Part A | 00123 |\n| Part B | -12.50 kg |"
    assert any("完整原生网格" in warning for warning in result.warnings)


@pytest.mark.parametrize("variant", ["incomplete", "overlay", "annotation"])
def test_ambiguous_or_illustrated_grid_does_not_invent_native_table(variant):
    result = parse_pdf(_pdf(lambda pdf: _draw_native_grid(pdf, **{variant: True})), "ambiguous-table.pdf", Options())
    assert not any(block.origin == "pdf-native-table" for block in result.blocks)
    assert any(block.kind == "image" for block in result.blocks)
    text = "\n".join(block.text for block in result.blocks if block.kind == "text")
    for expected in ["Item", "Value", "Part A", "00123", "Part B", "-12.50 kg"]:
        assert text.count(expected) == 1
    assert any("图表结构" in warning for warning in result.warnings)


def test_native_grid_respects_document_cell_budget_before_rendering():
    with pytest.raises(ExtractionError, match="单元格"):
        parse_pdf(_pdf(_draw_native_grid), "native-table.pdf", Options(max_table_cells=5))


def test_cropped_grid_is_not_mistaken_for_a_complete_table():
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(_pdf(_draw_native_grid))
        try:
            page = document[0]
            try:
                page.set_cropbox(50, 300, 400, 550)
            finally:
                page.close()
            output = BytesIO()
            document.save(output)
        finally:
            document.close()
    result = parse_pdf(output.getvalue(), "cropped-table.pdf", Options())
    assert not any(block.origin == "pdf-native-table" for block in result.blocks)
    assert any(block.kind == "image" for block in result.blocks)


def _ocr_layer_document(residual="diagram"):
    source_lines = [(30, 550, "NATIVE HEADING"), (30, 500, "NATIVE BEFORE FIGURE"),
                    (30, 100, "PAGE FOOTER")]

    def reference(pdf):
        if residual == "paper":
            pdf.setFillColorRGB(254 / 255, 254 / 255, 254 / 255)
            pdf.rect(0, 0, 400, 600, fill=1, stroke=0)
        pdf.setFillColorRGB(0, 0, 0)
        pdf.setFont("Helvetica", 16)
        for x, y, content in source_lines:
            pdf.drawString(x, y, content)
        if residual in {"diagram", "faint"}:
            pdf.setFillColorRGB(*((.1, .3, .7) if residual == "diagram" else (254 / 255,) * 3))
            pdf.rect(60, 250, 100, 100, fill=1, stroke=0)
        pdf.showPage()

    with PDFIUM_LOCK:
        doc = pdfium.PdfDocument(_pdf(reference))
        try:
            page = doc[0]
            try:
                bitmap = page.render(scale=2)
                try:
                    image = bitmap.to_pil().convert("RGB").copy()
                finally:
                    bitmap.close()
            finally:
                page.close()
        finally:
            doc.close()

    def draw(pdf):
        pdf.drawImage(ImageReader(image), 0, 0, 400, 600)
        for x, y, content in source_lines:
            text = pdf.beginText(x, y)
            text.setFont("Helvetica", 16)
            text.setTextRenderMode(3)
            text.textOut(content)
            pdf.drawText(text)
        pdf.showPage()

    try:
        return _pdf(draw)
    finally:
        image.close()


def test_ocr_layer_residual_diagram_is_inserted_at_its_remaining_position():
    result = parse_pdf(_ocr_layer_document(), "ocr-layer.pdf", Options())
    assert [block.kind for block in result.blocks] == ["text", "text", "image", "text"]
    assert [block.text for block in result.blocks if block.kind == "text"] == [
        "NATIVE HEADING", "NATIVE BEFORE FIGURE", "PAGE FOOTER"]
    image = next(block for block in result.blocks if block.kind == "image")
    assert image.bbox == pytest.approx((60, 250, 160, 350), abs=1.)
    assert any("纯白边缘" in warning for warning in result.warnings)


def test_ocr_layer_with_only_masked_text_does_not_call_vision_again():
    result = parse_pdf(_ocr_layer_document("blank"), "ocr-only.pdf", Options(max_images=0))
    assert all(block.kind == "text" for block in result.blocks)
    assert any("仅剩空白" in warning for warning in result.warnings)


def test_crop_never_thresholds_away_faint_residual_content():
    result = parse_pdf(_ocr_layer_document("faint"), "faint-diagram.pdf", Options())
    images = [block for block in result.blocks if block.kind == "image"]
    assert len(images) == 1
    with Image.open(BytesIO(images[0].image)) as image:
        assert min(image.convert("L").getextrema()) < 255
    assert images[0].bbox == pytest.approx((60, 250, 160, 350), abs=1.)


def test_nonwhite_scan_background_is_retained_with_position_warning():
    result = parse_pdf(_ocr_layer_document("paper"), "paper-scan.pdf", Options())
    image = next(block for block in result.blocks if block.kind == "image")
    assert image.bbox == (0., 0., 400., 600.)
    assert any("图文插入位置需人工复核" in warning for warning in result.warnings)


@pytest.mark.parametrize("tight_height", [0., .013977])
def test_degraded_cjk_tight_bounds_preserve_unicode_and_image_order(monkeypatch, tight_height):
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    original = pdfium.PdfTextPage.get_charbox
    loose_calls = []

    def linux_charbox(textpage, index, loose=False):
        box = original(textpage, index, loose=loose)
        if pdfium.raw.FPDFText_GetUnicode(textpage, index) >= 0x3000:
            if loose:
                loose_calls.append(index)
            else:
                baseline = round(box[1])
                return box[0], baseline, box[2], baseline + tight_height
        return box

    def draw(pdf):
        pdf.setFont("STSong-Light", 14)
        pdf.drawString(30, 550, "原生文字验证：设备保修期为24个月。")
        pdf.drawImage(ImageReader(_picture()), 30, 350, 150, 80)
        pdf.drawString(30, 300, "图后文字")
        pdf.showPage()

    monkeypatch.setattr(pdfium.PdfTextPage, "get_charbox", linux_charbox)
    result = parse_pdf(_pdf(draw), "linux-cjk.pdf", Options())
    assert [block.kind for block in result.blocks] == ["text", "image", "text"]
    assert result.blocks[0].text == "原生文字验证：设备保修期为24个月。"
    assert result.blocks[2].text == "图后文字"
    assert loose_calls
    assert all(block.bbox[3] > block.bbox[1] for block in result.blocks)
    assert any("字形边界退化" in warning for warning in result.warnings)


def test_unicode_with_unusable_tight_and_loose_bounds_fails_explicitly(monkeypatch):
    original = pdfium.PdfTextPage.get_charbox

    def invalid_bounds(textpage, index, loose=False):
        box = original(textpage, index, loose=loose)
        return box[0], box[1], box[2], box[1]

    monkeypatch.setattr(pdfium.PdfTextPage, "get_charbox", invalid_bounds)
    data = _pdf(lambda pdf: (pdf.drawString(30, 550, "Decodable Unicode"), pdf.showPage()))
    with pytest.raises(ExtractionError, match="字形与字体边界均无法定位"):
        parse_pdf(data, "bad-bounds.pdf", Options())


def test_valid_glyphs_outside_cropbox_do_not_trigger_loose_fallback(monkeypatch):
    original = pdfium.PdfTextPage.get_charbox

    def tight_only(textpage, index, loose=False):
        assert not loose, "A genuinely clipped glyph must not be recovered from font bounds"
        return original(textpage, index)

    def draw(pdf):
        pdf.drawString(30, 550, "OUTSIDE CROP")
        pdf.drawString(30, 300, "INSIDE CROP")
        pdf.showPage()

    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(_pdf(draw))
        try:
            page = document[0]
            try:
                page.set_cropbox(0, 0, 400, 400)
            finally:
                page.close()
            output = BytesIO()
            document.save(output)
        finally:
            document.close()
    monkeypatch.setattr(pdfium.PdfTextPage, "get_charbox", tight_only)
    result = parse_pdf(output.getvalue(), "crop.pdf", Options())
    assert [block.text for block in result.blocks] == ["INSIDE CROP"]
    assert not any("字形边界退化" in warning for warning in result.warnings)


@pytest.mark.parametrize("offset", [0., .17])
def test_image_crop_masks_native_overlay_before_vision(offset):
    def draw(pdf):
        pdf.setPageSize((400 + offset, 600 + offset))
        pdf.drawImage(ImageReader(Image.new("RGB", (200, 100), "red")),
                      30 + offset, 300 + offset, 200, 100)
        pdf.setFont("Helvetica", 20)
        pdf.drawString(50 + offset, 340 + offset, "OVERLAY")
        pdf.showPage()

    result = parse_pdf(_pdf(draw), "overlay.pdf", Options())
    assert sum(block.text.count("OVERLAY") for block in result.blocks) == 1
    raster = next(block for block in result.blocks if block.kind == "image")
    native = next(block for block in result.blocks if block.kind == "text")
    image = Image.open(BytesIO(raster.image))
    scale_x = image.width / (raster.bbox[2] - raster.bbox[0])
    scale_y = image.height / (raster.bbox[3] - raster.bbox[1])
    x = int(((native.bbox[0] + native.bbox[2]) / 2 - raster.bbox[0]) * scale_x)
    y = int(((native.bbox[1] + native.bbox[3]) / 2 - raster.bbox[1]) * scale_y)
    # Individual glyphs are masked, so assert a white pixel near the middle
    # (which may itself fall in an inter-character gap).
    assert any(image.getpixel((px, y)) == (255, 255, 255) for px in range(x - 10, x + 10))
    assert image.getpixel((image.width - 3, image.height - 3)) == (255, 0, 0)
    assert any("遮罩原生文字" in warning for warning in result.warnings)
    image.close()


@pytest.mark.parametrize("line_width", [0.1, 0.2])
def test_subpixel_vector_divider_has_nonempty_outward_aligned_crop(line_width):
    def draw(pdf):
        pdf.drawString(30, 550, "Text above divider")
        pdf.setLineWidth(line_width)
        pdf.line(30, 500, 300, 500)
        pdf.showPage()

    result = parse_pdf(_pdf(draw), "thin-line.pdf", Options())
    assert [block.kind for block in result.blocks] == ["text", "image"]
    assert result.blocks[0].text == "Text above divider"
    raster = result.blocks[1]
    assert raster.origin == "pdf-image-region"
    assert raster.bbox[1] <= 100 - line_width / 2
    assert raster.bbox[3] >= 100 + line_width / 2
    with Image.open(BytesIO(raster.image)) as picture:
        assert picture.height >= 2
        assert picture.width > 1
        assert picture.convert("L").getextrema()[0] < 255
        assert picture.height / (raster.bbox[3] - raster.bbox[1]) == pytest.approx(2)


def test_nested_form_image_is_cropped_in_page_coordinates():
    def draw(pdf):
        pdf.drawString(30, 550, "BEFORE")
        pdf.beginForm("figure", 0, 0, 100, 60)
        pdf.drawImage(ImageReader(_picture(size=(100, 60))), 0, 0, 100, 60)
        pdf.endForm()
        pdf.saveState()
        pdf.translate(60, 300)
        pdf.scale(2, 2)
        pdf.doForm("figure")
        pdf.restoreState()
        pdf.drawString(30, 200, "AFTER")
        pdf.showPage()

    result = parse_pdf(_pdf(draw), "form.pdf", Options())
    assert [block.kind for block in result.blocks] == ["text", "image", "text"]
    assert result.blocks[1].bbox == pytest.approx((60, 180, 260, 300))
    assert any("组合图形" in warning for warning in result.warnings)


def test_rotated_cropbox_keeps_image_and_glyph_coordinates_consistent():
    def draw(pdf):
        pdf.drawString(50, 400, "ROTATED")
        pdf.drawImage(ImageReader(_picture()), 60, 200, 150, 80)
        pdf.showPage()

    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(_pdf(draw))
        try:
            page = document[0]
            try:
                page.set_cropbox(20, 100, 380, 500)
                page.set_rotation(90)
            finally:
                page.close()
            output = BytesIO()
            document.save(output)
        finally:
            document.close()
    result = parse_pdf(output.getvalue(), "rotated.pdf", Options())
    assert sum(block.text.count("ROTATED") for block in result.blocks) == 1
    image = next(block for block in result.blocks if block.kind == "image")
    assert image.bbox == pytest.approx((100, 40, 180, 190))
    with Image.open(BytesIO(image.image)) as picture:
        assert picture.width == 160 and picture.height == 300


@pytest.mark.parametrize("field, limit", [("max_pages", 1), ("max_images", 1),
                                         ("max_output_chars", 3), ("max_total_image_bytes", 1)])
def test_limits_fail_whole_document_instead_of_returning_partial(field, limit):
    def draw(pdf):
        for number in range(2):
            pdf.drawString(30, 550, f"Page {number}")
            pdf.drawImage(ImageReader(_picture()), 30, 300, 150, 80)
            pdf.showPage()

    with pytest.raises(ExtractionError):
        parse_pdf(_pdf(draw), "limits.pdf", replace(Options(), **{field: limit}))


def test_embedded_image_pixel_limit_is_checked_before_render(monkeypatch):
    def draw(pdf):
        pdf.drawImage(ImageReader(_picture(size=(100, 100))), 0, 0, 100, 100)
        pdf.showPage()

    def should_not_render(*args, **kwargs):
        pytest.fail("Oversized embedded image reached PDFium rendering")

    monkeypatch.setattr(pdfium.PdfPage, "render", should_not_render)
    with pytest.raises(ExtractionError, match="像素"):
        parse_pdf(_pdf(draw), "large-image.pdf", replace(Options(), max_image_pixels=100))


def test_zero_vision_budget_allows_native_pdf_but_rejects_image():
    native = _pdf(lambda pdf: (pdf.drawString(30, 500, "NATIVE ONLY"), pdf.showPage()))
    result = parse_pdf(native, "native.pdf", replace(Options(), max_images=0))
    assert [block.text for block in result.blocks] == ["NATIVE ONLY"]

    def draw(pdf):
        pdf.drawImage(ImageReader(_picture()), 30, 300, 150, 80)
        pdf.showPage()

    with pytest.raises(ExtractionError, match="图片数量"):
        parse_pdf(_pdf(draw), "scan.pdf", replace(Options(), max_images=0))


def test_annotation_only_page_is_not_silently_discarded():
    def draw(pdf):
        pdf.textAnnotation("Important note", Rect=(40, 300, 100, 360))
        pdf.showPage()

    result = parse_pdf(_pdf(draw), "annotation.pdf", Options())
    assert result.page_count == 1
    assert len(result.blocks) == 1
    assert result.blocks[0].origin == "pdf-page"


@pytest.mark.parametrize("data", [b"", b"%PDF-1.7 corrupted"])
def test_invalid_pdf_raises(data):
    with pytest.raises(ExtractionError):
        parse_pdf(data, "bad.pdf", Options())


def test_blank_and_encrypted_pdf_raise():
    with pytest.raises(ExtractionError, match="没有可提取"):
        parse_pdf(_pdf(lambda pdf: pdf.showPage()), "blank.pdf", Options())
    encrypted = _pdf(lambda pdf: (pdf.drawString(30, 500, "secret"), pdf.showPage()),
                     encrypt=StandardEncryption("", ownerPassword="owner"))
    with pytest.raises(ExtractionError, match="加密"):
        parse_pdf(encrypted, "encrypted.pdf", Options())


def test_pdfium_handles_are_closed_when_page_extraction_fails(monkeypatch):
    data = _pdf(lambda pdf: (pdf.drawString(30, 500, "long text"), pdf.showPage()))
    closed = {"document": 0, "page": 0, "text": 0}
    for cls, key in [(pdfium.PdfDocument, "document"), (pdfium.PdfPage, "page"),
                     (pdfium.PdfTextPage, "text")]:
        original = cls.close

        def close(handle, *, _original=original, _key=key):
            assert PDFIUM_LOCK._is_owned()
            if handle.raw is not None:
                closed[_key] += 1
            return _original(handle)

        monkeypatch.setattr(cls, "close", close)
    with pytest.raises(ExtractionError):
        parse_pdf(data, "limit.pdf", replace(Options(), max_output_chars=1))
    assert closed == {"document": 1, "page": 1, "text": 1}
