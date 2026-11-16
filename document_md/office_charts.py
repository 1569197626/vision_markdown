"""Bounded PPTX category-chart cache extraction; never open source workbooks.

Chart values are kept as their stored lexical strings. This is data extraction,
not chart rendering or formula evaluation, and must not claim visual fidelity.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from .helpers import markdown_table
from .types import ExtractionError


C = "{http://schemas.openxmlformats.org/drawingml/2006/chart}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_CATEGORY_PLOTS = {
    "barChart": "column/bar chart", "bar3DChart": "3D column/bar chart",
    "lineChart": "line chart", "line3DChart": "3D line chart",
    "pieChart": "pie chart", "pie3DChart": "3D pie chart", "doughnutChart": "doughnut chart",
    "areaChart": "area chart", "area3DChart": "3D area chart", "radarChart": "radar chart",
}
_AXIS_NAMES = {"catAx": "Category axis", "valAx": "Value axis", "dateAx": "Date axis", "serAx": "Series axis"}
_GROUPINGS = {"clustered": "clustered", "stacked": "stacked", "percentStacked": "percent stacked", "standard": "standard"}


def _integer(value, limit: int) -> int:
    # Bound the digit length before int() or allocating a sparse expansion.
    if value is None or not re.fullmatch(r"[0-9]{1,10}", value):
        raise ExtractionError("A PPTX chart data-point index or count is invalid.")
    result = int(value)
    if result > limit:
        raise ExtractionError("The PPTX chart data-point count exceeds the table limit. Split the document.")
    return result


def _check_chars(size: int, output) -> None:
    if size > output.options.max_output_chars - output.chars:
        raise ExtractionError("PPTX chart text or cached data exceeds the output limit. Split the document.")


@dataclass
class _Cache:
    values: dict[int, str]
    count: int
    present: bool
    format_code: str = ""
    numeric: bool = False


def _cache(container, output) -> _Cache:
    """Read only one supported immediate cache/literal, preserving sparse IDs."""
    if container is None:
        return _Cache({}, 0, False)
    cache = None
    numeric = False
    for path, is_numeric in (
        (f"{C}strRef/{C}strCache", False), (f"{C}strLit", False),
        (f"{C}numRef/{C}numCache", True), (f"{C}numLit", True),
    ):
        candidate = container.find(path)
        if candidate is not None:
            if cache is not None:
                raise ExtractionError("The PPTX chart contains conflicting data caches.")
            cache, numeric = candidate, is_numeric
    if cache is None:
        if container.find(f"{C}multiLvlStrRef") is not None:
            output.warn("The PPTX chart has multi-level category labels that are not expanded. Category cells show missing-cache markers; review the original chart.")
        return _Cache({}, 0, False)
    limit = output.options.max_table_cells
    count_node = cache.find(f"{C}ptCount")
    count = _integer(count_node.get("val"), limit) if count_node is not None else None
    values = {}
    chars = 0
    for point in cache:
        if point.tag != f"{C}pt":
            continue
        index = _integer(point.get("idx"), max(limit - 1, 0))
        if index in values or (count is not None and index >= count):
            raise ExtractionError("A PPTX chart data-point index is duplicated or outside its declared range.")
        value = point.find(f"{C}v")
        # A missing <v> and an explicitly empty <v/> are both visibly blank;
        # neither is interpreted as the number zero.
        values[index] = value.text if value is not None and value.text is not None else ""
        chars += len(values[index])
        _check_chars(chars, output)
        if len(values) > limit:
            raise ExtractionError("The PPTX chart data-point count exceeds the table limit. Split the document.")
    if count is None:
        count = max(values, default=-1) + 1
    format_node = cache.find(f"{C}formatCode")
    format_code = (format_node.text or "") if format_node is not None else ""
    _check_chars(chars + len(format_code), output)
    return _Cache(values, count, True, format_code, numeric)


def _text(container, output) -> str:
    if container is None:
        return ""
    rich = container.find(f"{C}rich")
    if rich is not None:
        _check_chars(sum(len(node.text or "") for node in rich.iter(f"{A}t")), output)
        return "\n".join("".join(node.text or "" for node in p.iter(f"{A}t"))
                         for p in rich.findall(f"{A}p")).strip()
    direct = container.find(f"{C}v")
    if direct is not None:
        _check_chars(len(direct.text or ""), output)
        return direct.text or ""
    cache = _cache(container, output)
    # Chart titles and series-name references have a single cached text value.
    if cache.count > 1:
        output.warn("A PPTX chart title or series name has multiple cache entries, retained by index. Review the result.")
    return " / ".join(cache.values[index] for index in sorted(cache.values)).strip()


def _cell(cache: _Cache, index: int) -> str:
    if index not in cache.values:
        return "[cache missing]"
    return cache.values[index] if cache.values[index] != "" else "[empty]"


def extract_pptx_chart(chart, output, *, origin: str) -> None:
    """Emit a chart's native data into the existing Office _Output instance.

    Use from the shape.has_chart branch before continuing to the next shape.
    _Output owns cumulative limits and insertion order. No package relationships,
    embedded workbook content, external URLs, or model calls are accessed here.
    """
    root = chart._chartSpace
    body = root.find(f"{C}chart")
    if body is None:
        raise ExtractionError("The PPTX chart body is missing.")
    title = _text(body.find(f"{C}title/{C}tx"), output)
    heading = "### Native chart" + ("：" + title if title else "")
    output.text(heading, origin=origin + "/title")
    output.warn("PPTX native charts use saved data caches. Source workbooks are not accessed or recalculated; cached values may be outdated.")
    output.warn("PPTX chart appearance, data labels, trendlines, and error bars are not rendered. Tables retain raw cached values; display formats are only noted. Review the result.")
    plot_area = body.find(f"{C}plotArea")
    if plot_area is None:
        output.warn("The PPTX native chart has no plot area; chart data was not extracted.")
        return
    for axis in plot_area:
        kind = axis.tag.removeprefix(C) if isinstance(axis.tag, str) else ""
        if kind in _AXIS_NAMES:
            text = _text(axis.find(f"{C}title/{C}tx"), output)
            if text:
                output.text(f"{_AXIS_NAMES[kind]} title: {text}", origin=origin + "/axis")
    plots = [element for element in plot_area
             if isinstance(element.tag, str) and element.tag.startswith(C) and element.tag.endswith("Chart")]
    if not plots:
        output.warn("The PPTX native chart type is unsupported; chart data was not extracted. Review the original chart.")
    for plot_number, plot in enumerate(plots, 1):
        kind = plot.tag.removeprefix(C)
        plot_origin = f"{origin}/plot-{plot_number}"
        if kind not in _CATEGORY_PLOTS:
            output.text(f"Chart type: {kind} (native data extraction not supported)", origin=plot_origin)
            output.warn(f"Data extraction is not supported for PPTX chart type {kind}. Save as an image or PDF for recognition.")
            continue
        description = _CATEGORY_PLOTS[kind]
        direction = plot.find(f"{C}barDir")
        if direction is not None:
            description += "（" + {"col": "vertical", "bar": "horizontal"}.get(direction.get("val"), "unknown direction") + "）"
        grouping = plot.find(f"{C}grouping")
        if grouping is not None:
            description += "; grouping: " + _GROUPINGS.get(grouping.get("val"), grouping.get("val", "unspecified"))
        output.text(f"Chart type: {description}", origin=plot_origin)
        series_items = plot.findall(f"{C}ser")
        if not series_items:
            output.warn("The PPTX native chart has no data series to extract.")
        for series_number, series in enumerate(series_items, 1):
            series_origin = f"{plot_origin}/series-{series_number}"
            name = _text(series.find(f"{C}tx"), output)
            category = _cache(series.find(f"{C}cat"), output)
            values = _cache(series.find(f"{C}val"), output)
            rows = max(category.count, values.count)
            # Reserve the full rendered grid before range/list expansion.
            output.count_cells(3 * (rows + 1))
            info = ["#### Series: " + (name or f"Series {series_number} (name not saved)")]
            if category.format_code:
                info.append("Category display format: " + category.format_code)
            if values.format_code:
                info.append("Value display format: " + values.format_code)
            if (not category.present or not values.present
                    or category.count != values.count
                    or len(category.values) != rows or len(values.values) != rows):
                output.warn("The PPTX chart data cache is missing, incomplete, or has mismatched category/value counts. Missing points are marked individually; zeros are not fabricated.")
            if any(value == "" for value in (*category.values.values(), *values.values.values())):
                output.warn("The PPTX chart cache contains empty values, marked as [empty] rather than zero.")
            if rows == 0:
                info.append("[no usable chart data cache]")
                output.warn("The PPTX native chart has no usable data cache. The source workbook was not read or recalculated.")
            else:
                table = [["Data point", "Category", "Value (raw cache)"]]
                table.extend([[str(index + 1), _cell(category, index), _cell(values, index)] for index in range(rows)])
                info.append(markdown_table(table))
            output.text("\n\n".join(info), origin=series_origin)
