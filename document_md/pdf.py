"""Native-text-first PDF extraction; PDFium never runs outside PDFIUM_LOCK.

All block boxes use display coordinates (top-left origin, in PDF canvas units).
Reading order is a geometric heuristic, not tagged-PDF layout reconstruction.
Raster regions have native glyphs masked, including invisible OCR text layers.
This deliberately trades small masked portions of artwork for no duplicate OCR.
"""

from __future__ import annotations

import ctypes
import io
import math
import threading
from bisect import bisect_right
from dataclasses import dataclass

import pypdfium2 as pdfium
from PIL import Image, ImageChops, ImageDraw
from pypdfium2 import raw

from .helpers import image_block, markdown_table
from .types import Block, ExtractionError, Options, ParsedDocument


PDFIUM_LOCK = threading.RLock()
_MAX_OBJECTS = 10_000
_MAX_FORM_DEPTH = 16
_DISPLAY_PRECISION = 16
Box = tuple[float, float, float, float]


@dataclass(slots=True)
class _Glyph:
    text: str
    box: Box
    source_box: Box | None = None
    baseline: tuple[float, float] | None = None
    font_size: float = 0.
    angle: float = 0.
    index: int = 0
    line: int = 0


def _union(a: Box, b: Box) -> Box:
    return min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])


def _overlap(a: Box, b: Box, margin: float = 0) -> bool:
    return (a[0] < b[2] + margin and b[0] < a[2] + margin
            and a[1] < b[3] + margin and b[1] < a[3] + margin)


def _display_box(page, box: Box, width: float, height: float) -> Box:
    # PDFium handles page rotation and a non-zero CropBox origin. Use a 16x
    # virtual device for sub-unit accuracy without allocating a bitmap.
    device_w, device_h = math.ceil(width * _DISPLAY_PRECISION), math.ceil(height * _DISPLAY_PRECISION)
    points = []
    for x, y in ((box[0], box[1]), (box[0], box[3]),
                 (box[2], box[1]), (box[2], box[3])):
        dx, dy = ctypes.c_int(), ctypes.c_int()
        if not raw.FPDF_PageToDevice(page, 0, 0, device_w, device_h, 0,
                                     x, y, ctypes.byref(dx), ctypes.byref(dy)):
            raise ExtractionError("PDF page coordinate conversion failed.")
        points.append((dx.value * width / device_w, dy.value * height / device_h))
    result = (max(0., min(p[0] for p in points)), max(0., min(p[1] for p in points)),
              min(width, max(p[0] for p in points)), min(height, max(p[1] for p in points)))
    if not all(math.isfinite(v) for v in result):
        raise ExtractionError("PDF contains invalid page coordinates.")
    return result


def _resolvable_char_box(box: Box) -> bool:
    # Linux font substitution can return a 0.014pt-high tight CJK box even
    # when the Unicode text and the 14pt font bounds remain valid. Such a box
    # can disappear in the 1/16pt display grid just like a zero-area box.
    return (all(math.isfinite(value) for value in box)
            and box[2] - box[0] >= 1 / _DISPLAY_PRECISION
            and box[3] - box[1] >= 1 / _DISPLAY_PRECISION)


def _text_lines(page, page_number: int, width: float, height: float,
                char_budget: int) -> tuple[list[Block], list[_Glyph], int, int]:
    textpage = page.get_textpage()
    try:
        count = textpage.count_chars()
        if count > char_budget:
            raise ExtractionError("PDF native text exceeds the character limit. No truncated result was returned.")
        glyphs: list[_Glyph] = []
        lines: list[Block] = []
        current: list[_Glyph] = []
        fallback_count = 0

        def finish() -> None:
            if not current:
                return
            text = "".join(g.text for g in current).strip()
            if text:
                for glyph in current:
                    glyph.line = len(lines)
                box = current[0].box
                for g in current[1:]:
                    box = _union(box, g.box)
                lines.append(Block(kind="text", text=text, page=page_number,
                                   origin="pdf-native-text", bbox=box))
            current.clear()

        for index in range(count):
            codepoint = raw.FPDFText_GetUnicode(textpage, index)
            if not codepoint:
                raise ExtractionError("PDF contains text that cannot be mapped to Unicode for reliable extraction. Export it as images first.")
            if codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
                raise ExtractionError("PDF contains invalid Unicode text.")
            char = chr(codepoint)
            if char in "\r\n":
                finish()
                continue
            if char in "\x00\u200b\ufeff":
                continue
            if ord(char) < 32:
                char = " "
            origin_x, origin_y = ctypes.c_double(), ctypes.c_double()
            origin_ok = raw.FPDFText_GetCharOrigin(
                textpage, index, ctypes.byref(origin_x), ctypes.byref(origin_y))
            baseline = (origin_x.value, origin_y.value) if origin_ok else None
            font_size = raw.FPDFText_GetFontSize(textpage, index)
            angle = raw.FPDFText_GetCharAngle(textpage, index)
            if not math.isfinite(font_size) or font_size <= 0:
                font_size = current[-1].font_size if current else 1.
            if not math.isfinite(angle):
                angle = current[-1].angle if current else 0.
            source_box = textpage.get_charbox(index)
            used_loose_box = False
            if not char.isspace() and not _resolvable_char_box(source_box):
                # Inspect un-clipped PDF coordinates, not a display box that
                # may legitimately be empty because it lies outside CropBox.
                source_box = textpage.get_charbox(index, loose=True)
                if not _resolvable_char_box(source_box):
                    raise ExtractionError("PDF native text can be decoded, but neither glyph nor font bounds can be located. No result with missing text was returned.")
                used_loose_box = True
            box = _display_box(page, source_box, width, height)
            if box[2] <= box[0] or box[3] <= box[1]:
                if char.isspace() and current:
                    glyph = _Glyph(" ", current[-1].box, current[-1].source_box,
                                   baseline, font_size, angle, index)
                    current.append(glyph)
                    glyphs.append(glyph)
                continue  # Wholly clipped glyphs are not visible page content.
            fallback_count += int(used_loose_box)
            glyph = _Glyph(char, box, source_box, baseline, font_size, angle, index)
            if current:
                previous = current[-1]
                # Baselines and font em size, not tight ink bounds, identify
                # lines. A comma has a low, short box on the SAME baseline;
                # comparing its center with an e/r used to split sentences.
                em = max(previous.font_size, font_size, 1.)
                if baseline is not None and previous.baseline is not None:
                    dx = baseline[0] - previous.baseline[0]
                    dy = baseline[1] - previous.baseline[1]
                    # PDFium reports clockwise radians; PDF coordinates use
                    # +y upwards. A CCW 90-degree word therefore has angle
                    # 3*pi/2 and advances along +y, not against it.
                    along = dx * math.cos(angle) - dy * math.sin(angle)
                    across = dx * math.sin(angle) + dy * math.cos(angle)
                    before = previous.source_box or source_box
                    advance = max(before[2] - before[0], 0.) if abs(math.sin(angle)) < .5 else max(before[3] - before[1], 0.)
                    different_angle = math.cos(angle - previous.angle) < .995
                    if (abs(across) > em * .55 or along < -em * .5
                            or along - advance > em * 2.5 or different_angle):
                        finish()
                # Preserve PDFium's actual/generated whitespace. Tight glyph
                # boxes exclude side bearings, and short glyphs such as u/r
                # and digits can have large geometric gaps within one word.
                # Inventing spaces from those gaps corrupts native content.
            current.append(glyph)
            glyphs.append(glyph)
        finish()
        return lines, glyphs, count, fallback_count
    finally:
        textpage.close()


def _inspect_object(obj, page, options: Options, counter: list[int], depth: int = 0) -> bool:
    """Preflight nested images before rendering; return whether art is present."""
    counter[0] += 1
    if counter[0] > _MAX_OBJECTS or depth > _MAX_FORM_DEPTH:
        raise ExtractionError("PDF contains too many page objects or nesting levels.")
    kind = raw.FPDFPageObj_GetType(obj)
    if kind == raw.FPDF_PAGEOBJ_IMAGE:
        image = pdfium.PdfImage(obj, page=page)
        try:
            w, h = image.get_size()
            if w <= 0 or h <= 0 or w * h > options.max_image_pixels:
                raise ExtractionError("A PDF embedded image exceeds the pixel limit or has invalid dimensions.")
        finally:
            image.close()  # The page, not this non-owning wrapper, owns obj.
        return True
    if kind == raw.FPDF_PAGEOBJ_FORM:
        count = raw.FPDFFormObj_CountObjects(obj)
        if count < 0:
            raise ExtractionError("A PDF Form object could not be read.")
        has_art = False
        for index in range(count):
            child = raw.FPDFFormObj_GetObject(obj, index)
            if not child:
                raise ExtractionError("A PDF Form child object could not be read.")
            # Do not short-circuit: every nested image needs a pixel guard.
            has_art = _inspect_object(child, page, options, counter, depth + 1) or has_art
        return has_art
    return kind != raw.FPDF_PAGEOBJ_TEXT


def _regions(page, width: float, height: float, options: Options,
             excluded_paths: set[int] | None = None) -> tuple[list[Box], bool, int]:
    regions: list[Box] = []
    complex_art = False
    counter = [0]
    count = raw.FPDFPage_CountObjects(page)
    if count < 0 or count > _MAX_OBJECTS:
        raise ExtractionError("The PDF page object count is invalid or exceeds the limit.")
    for object_index, obj in enumerate(page.get_objects(max_depth=1)):
        try:
            if not _inspect_object(obj.raw, page, options, counter):
                continue
            if excluded_paths and object_index in excluded_paths:
                continue  # Consumed complete native table grid, not artwork.
            box = _display_box(page, obj.get_pos(), width, height)
            if box[2] <= box[0] or box[3] <= box[1]:
                continue
            # Keep even thin vector lines: adjacent chart/table elements are
            # merged below. We cannot infer that a small object is decorative.
            regions.append(box)
            complex_art |= obj.type != raw.FPDF_PAGEOBJ_IMAGE
        finally:
            obj.close()
    annotation_count = raw.FPDFPage_GetAnnotCount(page)
    if annotation_count < 0 or annotation_count + counter[0] > _MAX_OBJECTS:
        raise ExtractionError("The PDF annotation count is invalid or exceeds the limit.")
    for index in range(annotation_count):
        annotation = raw.FPDFPage_GetAnnot(page, index)
        if not annotation:
            raise ExtractionError("A PDF annotation could not be read.")
        try:
            # Links carry no document content. Widgets and visible annotation
            # appearances are rendered, since textpage excludes their text.
            if raw.FPDFAnnot_GetSubtype(annotation) == raw.FPDF_ANNOT_LINK:
                continue
            rectangle = raw.FS_RECTF()
            if not raw.FPDFAnnot_GetRect(annotation, ctypes.byref(rectangle)):
                raise ExtractionError("PDF annotation coordinates could not be read.")
            box = _display_box(page, (rectangle.left, rectangle.bottom,
                                      rectangle.right, rectangle.top), width, height)
            if box[2] > box[0] and box[3] > box[1]:
                regions.append(box)
                complex_art = True
        finally:
            raw.FPDFPage_CloseAnnot(annotation)
    # Merge overlapping/adjacent artwork to avoid recognizing the same area
    # twice (including duplicate images, charts and table borders).
    merged: list[Box] = []
    for box in regions:
        index = 0
        while index < len(merged):
            if _overlap(box, merged[index], margin=3.):
                box = _union(box, merged.pop(index))
                index = 0
            else:
                index += 1
        merged.append(box)
    return merged, complex_art, count + annotation_count


def _render_region(page, box: Box, width: float, height: float,
                   glyphs: list[_Glyph], options: Options) -> tuple[bytes | None, Box, bool]:
    region_w, region_h = box[2] - box[0], box[3] - box[1]
    scale = min(2., options.max_image_side / max(region_w, region_h),
                math.sqrt(options.max_image_pixels / (region_w * region_h)))
    if scale <= 0 or not math.isfinite(scale):
        raise ExtractionError("PDF rendering dimensions are invalid.")

    # PDFium rounds the full page and each crop margin separately using ceil.
    # A subpixel vector line can otherwise lose its entire width/height.
    # Choose the crop in the full-page device grid, expanding outwards to at
    # least two pixels (one when the configured limits permit only one).
    minimum = 2 if options.max_image_side >= 2 and options.max_image_pixels >= 4 else 1
    while True:
        source_w, source_h = math.ceil(width * scale), math.ceil(height * scale)
        scale_x, scale_y = source_w / width, source_h / height
        left, top = max(0, math.floor(box[0] * scale_x)), max(0, math.floor(box[1] * scale_y))
        right = min(source_w, math.ceil(box[2] * scale_x))
        bottom = min(source_h, math.ceil(box[3] * scale_y))
        if right - left < minimum:
            left = max(0, left - (minimum - (right - left) + 1) // 2)
            right = min(source_w, left + minimum)
            left = max(0, right - minimum)
        if bottom - top < minimum:
            top = max(0, top - (minimum - (bottom - top) + 1) // 2)
            bottom = min(source_h, top + minimum)
            top = max(0, bottom - minimum)
        render_w, render_h = right - left, bottom - top
        if render_w * render_h <= options.max_image_pixels and max(render_w, render_h) <= options.max_image_side:
            break
        scale *= .99
    rendered_box = (left / scale_x, top / scale_y, right / scale_x, bottom / scale_y)

    def margin(pixels: int) -> float:
        # A tiny downward bias prevents floating division/multiplication from
        # making ceil(N + epsilon) consume an extra pixel of the crop.
        return max(0., (pixels - 1e-7) / scale)

    bitmap = page.render(scale=scale,
                         crop=(margin(left), margin(source_h - bottom),
                               margin(source_w - right), margin(top)),
                         rev_byteorder=True, limit_image_cache=True, draw_annots=True)
    try:
        # Copy while bitmap is alive; PIL may otherwise reference its buffer.
        image = bitmap.to_pil().convert("RGB")
        try:
            if image.width * image.height > options.max_image_pixels:
                raise ExtractionError("The rendered PDF image exceeds the pixel limit.")
            draw = ImageDraw.Draw(image)
            masked = False
            for glyph in glyphs:
                if glyph.text.isspace():
                    continue
                if _overlap(glyph.box, rendered_box):
                    masked = True
                    # PDFium crop rounds in pixel units; 2px padding covers
                    # rounding and antialiasing, including invisible OCR text.
                    x0 = glyph.box[0] * scale_x - left
                    y0 = glyph.box[1] * scale_y - top
                    x1 = glyph.box[2] * scale_x - left
                    y1 = glyph.box[3] * scale_y - top
                    draw.rectangle((math.floor(x0) - 2, math.floor(y0) - 2,
                                    math.ceil(x1) + 2, math.ceil(y1) + 2), fill="white")
            trimmed = False
            if masked:
                # A scan with an OCR layer can cover the whole page even
                # though only a small diagram remains after text masking.
                # Crop exact white margins only: thresholds could silently
                # discard light text or faint chart details. Noisy/paper-tone
                # backgrounds retain their full extent and get a warning.
                white = Image.new("RGB", image.size, "white")
                difference = ImageChops.difference(image, white)
                try:
                    foreground = difference.getbbox()
                finally:
                    difference.close()
                    white.close()
                if foreground is None:
                    return None, rendered_box, True
                if foreground != (0, 0, image.width, image.height):
                    cropped = image.crop(foreground)
                    image.close()
                    image = cropped
                    rendered_box = ((left + foreground[0]) / scale_x,
                                    (top + foreground[1]) / scale_y,
                                    (left + foreground[2]) / scale_x,
                                    (top + foreground[3]) / scale_y)
                    trimmed = True
            output = io.BytesIO()
            image.save(output, format="PNG")
            return output.getvalue(), rendered_box, trimmed
        finally:
            image.close()
    finally:
        bitmap.close()


def _layout_box(box: Box, rotation: int, width: float, height: float) -> Box:
    """Undo a consistently oriented page's display rotation for ordering."""
    x0, y0, x1, y1 = box
    if rotation == 90:
        return y0, width - x1, y1, width - x0
    if rotation == 180:
        return width - x1, height - y1, width - x0, height - y0
    if rotation == 270:
        return height - y1, x0, height - y0, x1
    return box


def _inside(inner: Box, outer: Box, tolerance=1.) -> bool:
    return (inner[0] >= outer[0] - tolerance and inner[1] >= outer[1] - tolerance
            and inner[2] <= outer[2] + tolerance and inner[3] <= outer[3] + tolerance)


def _cluster_positions(values: list[float], tolerance=1.) -> list[float]:
    groups: list[list[float]] = []
    for value in sorted(values):
        if groups and value - groups[-1][-1] <= tolerance:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [sum(group) / len(group) for group in groups]


def _native_tables(page, native: list[Block], glyphs: list[_Glyph], rotation: int,
                   width: float, height: float, cell_budget: int) -> tuple[list[Block], set[int], int]:
    """Recover only complete, unfilled rectilinear grids with unambiguous text.

    Filled/curved artwork, images, merged/incomplete grids and text crossing a
    cell boundary keep the original path. No cells or text are inferred from
    proximity alone. All original visible glyphs are retained exactly once.
    """
    if not native or raw.FPDFPage_CountObjects(page) > _MAX_OBJECTS:
        return native, set(), 0
    paths: dict[int, tuple[Box, list[tuple[float, float, float, float]]]] = {}
    other_art: list[Box] = []
    segment_count = 0
    for object_index, obj in enumerate(page.get_objects(max_depth=1)):
        try:
            if obj.type == raw.FPDF_PAGEOBJ_TEXT:
                continue
            display = _display_box(page, obj.get_pos(), width, height)
            layout = _layout_box(display, rotation, width, height)
            if obj.type != raw.FPDF_PAGEOBJ_PATH:
                other_art.append(layout)
                continue
            fill, stroke = ctypes.c_int(), ctypes.c_int()
            if (not raw.FPDFPath_GetDrawMode(obj, ctypes.byref(fill), ctypes.byref(stroke))
                    or fill.value != 0 or not stroke.value):
                other_art.append(layout)
                continue
            count = raw.FPDFPath_CountSegments(obj)
            segment_count += max(count, 0)
            if count < 0 or segment_count > 2048:
                return native, set(), 0  # Bound optional grid recognition work.
            matrix = obj.get_matrix()
            segments = []
            previous = start = None
            valid = True
            for index in range(count):
                segment = raw.FPDFPath_GetPathSegment(obj, index)
                px, py = ctypes.c_float(), ctypes.c_float()
                if not segment or not raw.FPDFPathSegment_GetPoint(segment, ctypes.byref(px), ctypes.byref(py)):
                    valid = False
                    break
                x = px.value * matrix.a + py.value * matrix.c + matrix.e
                y = px.value * matrix.b + py.value * matrix.d + matrix.f
                point_box = _layout_box(_display_box(page, (x, y, x, y), width, height),
                                        rotation, width, height)
                point = point_box[:2]
                kind = raw.FPDFPathSegment_GetType(segment)
                if kind == raw.FPDF_SEGMENT_MOVETO:
                    previous = start = point
                elif kind == raw.FPDF_SEGMENT_LINETO and previous is not None:
                    segments.append((*previous, *point))
                    previous = point
                else:
                    valid = False
                    break
                if raw.FPDFPathSegment_GetClose(segment) and previous is not None and start is not None:
                    segments.append((*previous, *start))
                    previous = start
            if valid and segments and all(abs(a - c) <= 1. or abs(b - d) <= 1. for a, b, c, d in segments):
                paths[object_index] = (display, segments)
            else:
                other_art.append(layout)
        finally:
            obj.close()

    annotations = raw.FPDFPage_GetAnnotCount(page)
    if annotations < 0 or annotations > _MAX_OBJECTS:
        return native, set(), 0
    for index in range(annotations):
        annotation = raw.FPDFPage_GetAnnot(page, index)
        if not annotation:
            return native, set(), 0
        try:
            if raw.FPDFAnnot_GetSubtype(annotation) == raw.FPDF_ANNOT_LINK:
                continue
            rect = raw.FS_RECTF()
            if not raw.FPDFAnnot_GetRect(annotation, ctypes.byref(rect)):
                return native, set(), 0
            display = _display_box(page, (rect.left, rect.bottom, rect.right, rect.top), width, height)
            other_art.append(_layout_box(display, rotation, width, height))
        finally:
            raw.FPDFPage_CloseAnnot(annotation)

    horizontal: list[tuple[float, float, float]] = []
    vertical: list[tuple[float, float, float]] = []
    for _, segments in paths.values():
        for x0, y0, x1, y1 in segments:
            if abs(y1 - y0) <= 1. and abs(x1 - x0) > 4.:
                horizontal.append(((y0 + y1) / 2, min(x0, x1), max(x0, x1)))
            elif abs(x1 - x0) <= 1. and abs(y1 - y0) > 4.:
                vertical.append(((x0 + x1) / 2, min(y0, y1), max(y0, y1)))
    spans = {(round(y0), round(y1)) for _, y0, y1 in vertical}
    if len(spans) > 128:
        return native, set(), 0
    consumed_paths: set[int] = set()
    consumed_text: set[int] = set()
    tables: list[Block] = []
    cells_used = 0
    for top, bottom in sorted(spans):
        xs = _cluster_positions([x for x, a, b in vertical if abs(a - top) <= 1. and abs(b - bottom) <= 1.])
        if len(xs) < 3:
            continue
        ys = _cluster_positions([y for y, a, b in horizontal
                                 if abs(a - xs[0]) <= 1. and abs(b - xs[-1]) <= 1.
                                 and top - 1. <= y <= bottom + 1.])
        if len(ys) < 3 or abs(ys[0] - top) > 1. or abs(ys[-1] - bottom) > 1.:
            continue
        rect = (xs[0], ys[0], xs[-1], ys[-1])
        layout_width, layout_height = (height, width) if rotation in {90, 270} else (width, height)
        # Clamping cropped path endpoints could make a partial table look
        # closed at the page boundary. Keep such edge tables on the old path.
        if rect[0] <= 1. or rect[1] <= 1. or rect[2] >= layout_width - 1. or rect[3] >= layout_height - 1.:
            continue
        if min(b - a for a, b in zip(xs, xs[1:])) < 8. or min(b - a for a, b in zip(ys, ys[1:])) < 5.:
            continue
        if any(_overlap(box, rect) for box in other_art):
            continue
        grid_paths = set()
        for object_index, (display, segments) in paths.items():
            def on_grid(segment):
                a, b, c, d = segment
                segment_box = (min(a, c), min(b, d), max(a, c), max(b, d))
                return _inside(segment_box, rect) and (
                    (abs(b - d) <= 1. and any(abs(b - y) <= 1. for y in ys))
                    or (abs(a - c) <= 1. and any(abs(a - x) <= 1. for x in xs)))
            if all(on_grid(segment) for segment in segments):
                grid_paths.add(object_index)
            elif _overlap(_layout_box(display, rotation, width, height), rect):
                grid_paths = set()
                break
        if not grid_paths or grid_paths & consumed_paths:
            continue
        relevant = [block for block in native
                    if _overlap(_layout_box(block.bbox, rotation, width, height), rect)]
        if not relevant or any(not _inside(_layout_box(block.bbox, rotation, width, height), rect)
                               or id(block) in consumed_text for block in relevant):
            continue
        nrows, ncols = len(ys) - 1, len(xs) - 1
        if nrows * ncols > cell_budget - cells_used:
            raise ExtractionError("The PDF native table cell count exceeds the limit. Split the document.")
        cells: list[list[list[_Glyph]]] = [[[] for _ in range(ncols)] for _ in range(nrows)]
        valid = True
        visible = 0
        for glyph in glyphs:
            box = _layout_box(glyph.box, rotation, width, height)
            if not _overlap(box, rect):
                continue
            col = bisect_right(xs, (box[0] + box[2]) / 2) - 1
            row = bisect_right(ys, (box[1] + box[3]) / 2) - 1
            if not (0 <= row < nrows and 0 <= col < ncols) or not _inside(
                    box, (xs[col], ys[row], xs[col + 1], ys[row + 1])):
                if glyph.text.isspace():
                    continue
                valid = False
                break
            cells[row][col].append(glyph)
            visible += not glyph.text.isspace()
        if not valid or not visible:
            continue
        rows = []
        occupied_columns: set[int] = set()
        for cell_row in cells:
            row = []
            for column_index, cell in enumerate(cell_row):
                groups: dict[int, list[_Glyph]] = {}
                for glyph in cell:
                    groups.setdefault(glyph.line, []).append(glyph)
                def line_position(group):
                    box = group[0].box
                    for glyph in group[1:]:
                        box = _union(box, glyph.box)
                    box = _layout_box(box, rotation, width, height)
                    return round(box[1] / 3), box[0]

                # PDF content streams may paint a lower line before an upper
                # one. Keep character order within each line, but position
                # whole lines geometrically within the cell.
                lines = ["".join(g.text for g in sorted(group, key=lambda g: g.index)).strip()
                         for group in sorted(groups.values(), key=line_position)]
                value = "\n".join(line for line in lines if line)
                if value:
                    occupied_columns.add(column_index)
                row.append(value)
            rows.append(row)
        if len(occupied_columns) < 2 or any(not any(row) for row in rows):
            continue
        display_box = paths[next(iter(grid_paths))][0]
        for object_index in grid_paths:
            display_box = _union(display_box, paths[object_index][0])
        tables.append(Block(kind="text", text=markdown_table(rows), page=relevant[0].page,
                            origin="pdf-native-table", bbox=display_box))
        consumed_paths.update(grid_paths)
        consumed_text.update(id(block) for block in relevant)
        cells_used += nrows * ncols
    return [block for block in native if id(block) not in consumed_text] + tables, consumed_paths, cells_used


def _column_order(blocks: list[Block], boxes: dict[int, Box], depth=0) -> tuple[list[Block], bool]:
    """Use a supported vertical gutter, preserving full-width top/bottom bands.

    Ambiguous spanning content in a column body is left in geometric order;
    this intentionally does not guess at arbitrary magazines or sidebars.
    """
    ordered = sorted(blocks, key=lambda block: (round(boxes[id(block)][1] / 3), boxes[id(block)][0]))
    if depth >= 4 or len(blocks) < 6:
        return ordered, False
    text = [block for block in ordered if block.kind == "text" and block.origin != "pdf-native-table"]
    candidates: set[float] = set()
    for index, first in enumerate(text):
        a = boxes[id(first)]
        for second in text[index + 1:index + 17]:
            b = boxes[id(second)]
            line_height = max(a[3] - a[1], b[3] - b[1], 1.)
            if b[1] - a[1] > line_height * 1.2:
                break
            if abs(a[1] - b[1]) <= line_height * .6 and b[0] - a[2] > line_height * 1.5:
                candidates.add(round((a[2] + b[0]) / 2, 1))
        if len(candidates) >= 128:
            break
    best = None
    for cut in sorted(candidates):
        left = [b for b in text if boxes[id(b)][2] < cut]
        right = [b for b in text if boxes[id(b)][0] > cut]
        if min(len(left), len(right)) < 3:
            continue
        top = max(min(boxes[id(b)][1] for b in left), min(boxes[id(b)][1] for b in right))
        bottom = min(max(boxes[id(b)][3] for b in left), max(boxes[id(b)][3] for b in right))
        body = [b for b in blocks if top <= (boxes[id(b)][1] + boxes[id(b)][3]) / 2 <= bottom]
        body_left = [b for b in body if boxes[id(b)][2] < cut]
        body_right = [b for b in body if boxes[id(b)][0] > cut]
        if (min(len(body_left), len(body_right)) < 3
                or len(body_left) + len(body_right) != len(body)):
            continue
        # At least three baselines per side avoids treating a one-row label
        # and value as independent newspaper columns.
        if min(len({round(boxes[id(b)][1] / 8) for b in side})
               for side in (body_left, body_right)) < 3:
            continue
        # Several aligned short labels/values are a borderless record, not
        # independent reading columns. Require paragraph-width evidence on
        # both sides before overriding conservative row-wise order.
        if any(sum(boxes[id(b)][2] - boxes[id(b)][0]
                   >= 8 * max(boxes[id(b)][3] - boxes[id(b)][1], 1.)
                   for b in side if b.kind == "text") < 2
               for side in (body_left, body_right)):
            continue

        # The overlap establishes the gutter, but the longer column may
        # continue below it. Extend through normal line/paragraph spacing,
        # stopping at a spanning object or a separated footer band.
        for side, population in ((body_left, left), (body_right, right)):
            starts = sorted({boxes[id(b)][1] for b in side})
            line_height = max(boxes[id(b)][3] - boxes[id(b)][1] for b in side)
            gap_limit = max(3 * line_height,
                            1.5 * max((b - a for a, b in zip(starts, starts[1:])), default=0.))
            last = max(boxes[id(b)][3] for b in side)
            for block in sorted(population, key=lambda b: boxes[id(b)][1]):
                box = boxes[id(block)]
                if (box[1] + box[3]) / 2 <= bottom:
                    continue
                if box[1] - last > gap_limit:
                    break
                if any(boxes[id(b)][0] <= cut <= boxes[id(b)][2]
                       and boxes[id(b)][3] > last and boxes[id(b)][1] < box[3]
                       for b in blocks):
                    break
                side.append(block)
                last = box[3]
        extended_bottom = max(boxes[id(b)][3] for b in body_left + body_right)
        extended = [b for b in blocks
                    if top <= (boxes[id(b)][1] + boxes[id(b)][3]) / 2 <= extended_bottom]
        # Never absorb a band while dropping another block in the same band.
        if {id(b) for b in extended} != {id(b) for b in body_left + body_right}:
            continue
        bottom = extended_bottom
        score = min(len(body_left), len(body_right)) * max(bottom - top, 0.)
        if best is None or score > best[0]:
            best = (score, top, bottom, body_left, body_right)
    if best is None:
        return ordered, bool(candidates)
    _, top, bottom, left, right = best
    before = [b for b in ordered if (boxes[id(b)][1] + boxes[id(b)][3]) / 2 < top]
    after = [b for b in ordered if (boxes[id(b)][1] + boxes[id(b)][3]) / 2 > bottom]
    left_order, _ = _column_order(left, boxes, depth + 1)
    right_order, _ = _column_order(right, boxes, depth + 1)
    return before + left_order + right_order + after, True


def _reading_order(blocks: list[Block], rotation=0, width=0., height=0.) -> tuple[list[Block], bool]:
    boxes = {id(block): _layout_box(block.bbox, rotation, width, height) for block in blocks}
    blocks, complex_layout = _column_order(blocks, boxes)
    # Merge consecutive lines only when there is no image or other column
    # between them. Keep line breaks instead of inventing sentence flow.
    result: list[Block] = []
    for block in blocks:
        previous = result[-1] if result else None
        if (previous and previous.kind == block.kind == "text"
                and previous.origin == block.origin == "pdf-native-text"):
            a, b = boxes[id(previous)], boxes[id(block)]
            line_height = max(b[3] - b[1], 1.)
            if 0 <= b[1] - a[3] <= line_height * .8 and abs(a[0] - b[0]) <= line_height:
                previous.text += "\n" + block.text
                previous.bbox = _union(previous.bbox, block.bbox)
                boxes[id(previous)] = _union(a, b)
                continue
        result.append(block)
    return result, complex_layout


def parse_pdf(data: bytes, filename: str, options: Options) -> ParsedDocument:
    """Parse every page or fail explicitly; never return a truncated PDF."""
    if not data or len(data) > options.max_file_bytes:
        raise ExtractionError("The PDF is empty or exceeds the file size limit.")
    if options.max_images < 0 or min(options.max_pages, options.max_image_side,
           options.max_image_pixels, options.max_output_chars, options.max_total_image_bytes) <= 0:
        raise ExtractionError("PDF processing limits must be positive.")
    # PDFium has process-global state. The lock covers loading, all helpers,
    # raw calls and explicit destruction, including exceptional exits.
    with PDFIUM_LOCK:
        document = None
        try:
            document = pdfium.PdfDocument(data)
            if raw.FPDF_GetSecurityHandlerRevision(document) >= 0:
                raise ExtractionError("Encrypted PDFs are not supported. Provide an unencrypted copy.")
            document.init_forms()
            page_count = len(document)
            if not page_count or page_count > options.max_pages:
                raise ExtractionError("The PDF has no pages or exceeds the page limit. No partial pages were returned.")
            result = ParsedDocument(filename=filename, format="pdf", page_count=page_count)
            total_chars = total_images = total_bytes = total_cells = 0
            for page_index in range(page_count):
                page = document[page_index]
                number = page_index + 1
                try:
                    width, height = page.get_size()
                    if not all(math.isfinite(v) and 0 < v <= 1_000_000 for v in (width, height)):
                        raise ExtractionError(f"PDF page {number} has invalid dimensions.")
                    native, glyphs, count, fallback_count = _text_lines(
                        page, number, width, height, options.max_output_chars - total_chars)
                    total_chars += count
                    if fallback_count:
                        result.warnings.append(f"PDF page {number} has {fallback_count} degenerate glyph bounds. Font bounds were used to preserve native text; review text/image placement and masking regions.")
                    orientations = {round(g.angle / (math.pi / 2)) % 4
                                    for g in glyphs if not g.text.isspace()}
                    # A long vertical table header must not rotate a whole
                    # page merely because it has more characters than its
                    # horizontal title/data. Normalize source text rotation
                    # only when all visible text has one orientation.
                    text_rotation = next(iter(orientations)) * 90 if len(orientations) == 1 else 0
                    order_rotation = (page.get_rotation() + text_rotation) % 360
                    if len(orientations) > 1:
                        result.warnings.append(f"PDF page {number} contains text in multiple directions, ordered by page and cell positions. Review complex reading directions.")
                    native, table_paths, table_cells = _native_tables(
                        page, native, glyphs, order_rotation, width, height,
                        options.max_table_cells - total_cells)
                    total_cells += table_cells
                    if table_cells:
                        result.warnings.append(f"PDF page {number} tables were reconstructed from a complete native grid. The first row is used as the Markdown header.")
                    regions, complex_art, object_count = _regions(page, width, height, options, table_paths)
                    if raw.FPDFPage_GetAnnotCount(page):
                        result.warnings.append(f"PDF page {number} annotations/forms are processed by visible appearance; collapsed annotation text and interactive content are excluded.")
                    if not native and not object_count:
                        result.warnings.append(f"PDF page {number} is blank.")
                        continue
                    if not native:
                        # A scanned/vector page is rendered ONCE, regardless
                        # of the number of embedded images/tiles it contains.
                        regions = [(0., 0., width, height)]
                    blocks = list(native)
                    for region in regions:
                        original_region = region
                        overlapping = [g for g in glyphs if not g.text.isspace() and _overlap(g.box, region)]
                        content, region, trimmed = _render_region(page, region, width, height, glyphs, options)
                        if content is None:
                            result.warnings.append(f"An image region on PDF page {number} is blank after removing extracted text. Duplicate vision recognition was skipped.")
                            continue
                        if total_images + 1 > options.max_images:
                            raise ExtractionError("The PDF image count to recognize exceeds the limit. No partial content was returned.")
                        block = image_block(content, options, page=number, bbox=region,
                                            origin="pdf-image-region" if native else "pdf-page",
                                            purpose="mixed",
                                            context=("Native text has been extracted separately and its image pixels masked white. Extract only remaining content; "
                                                     "do not reconstruct masked text." if native else "Transcribe as Markdown in page reading order."))
                        total_images += 1
                        total_bytes += len(block.image)
                        if total_bytes > options.max_total_image_bytes:
                            raise ExtractionError("The total PDF image size exceeds the limit. No partial content was returned.")
                        blocks.append(block)
                        if overlapping:
                            result.warnings.append(f"Native text in image regions on PDF page {number} was masked to avoid duplicates; graphic details covered by text may be affected.")
                            if trimmed:
                                result.warnings.append(f"Pure-white edges were cropped after masking on PDF page {number}; recognition results are inserted at the remaining graphic position.")
                            elif ((original_region[2] - original_region[0]) * (original_region[3] - original_region[1])
                                  > width * height * .5):
                                result.warnings.append(f"The image background or remaining content on PDF page {number} covers a large area. Review text/image insertion positions manually.")
                    ordered, complex_layout = _reading_order(blocks, order_rotation, width, height)
                    if sum(len(block.text) for block in result.blocks + ordered) > options.max_output_chars:
                        raise ExtractionError("PDF native text exceeds the character limit. No truncated result was returned.")
                    if complex_layout or complex_art:
                        result.warnings.append(f"PDF page {number} contains multiple columns or vector/grouped graphics. Reading order and chart structure use coordinate heuristics; review the result.")
                    result.blocks.extend(ordered)
                finally:
                    page.close()
            if not result.blocks:
                raise ExtractionError("The PDF has no content to extract.")
            result.warnings = list(dict.fromkeys(result.warnings))
            return result
        except ExtractionError:
            raise
        except Exception as exc:
            raise ExtractionError("The PDF is damaged, encrypted, or could not be fully parsed.") from exc
        finally:
            if document is not None:
                document.close()
