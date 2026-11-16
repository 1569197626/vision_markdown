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
    "barChart": "柱状图/条形图", "bar3DChart": "三维柱状图/条形图",
    "lineChart": "折线图", "line3DChart": "三维折线图",
    "pieChart": "饼图", "pie3DChart": "三维饼图", "doughnutChart": "环形图",
    "areaChart": "面积图", "area3DChart": "三维面积图", "radarChart": "雷达图",
}
_AXIS_NAMES = {"catAx": "类别轴", "valAx": "数值轴", "dateAx": "日期轴", "serAx": "系列轴"}
_GROUPINGS = {"clustered": "簇状", "stacked": "堆积", "percentStacked": "百分比堆积", "standard": "普通"}


def _integer(value, limit: int) -> int:
    # Bound the digit length before int() or allocating a sparse expansion.
    if value is None or not re.fullmatch(r"[0-9]{1,10}", value):
        raise ExtractionError("PPTX 图表数据点索引或数量无效。")
    result = int(value)
    if result > limit:
        raise ExtractionError("PPTX 图表数据点数量超过表格上限，请拆分文档。")
    return result


def _check_chars(size: int, output) -> None:
    if size > output.options.max_output_chars - output.chars:
        raise ExtractionError("PPTX 图表文字或缓存数据超过输出上限，请拆分文档。")


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
                raise ExtractionError("PPTX 图表包含冲突的数据缓存。")
            cache, numeric = candidate, is_numeric
    if cache is None:
        if container.find(f"{C}multiLvlStrRef") is not None:
            output.warn("PPTX 图表含多级类别标签，当前未展开；类别列以缓存缺失标记呈现，请核对原图。")
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
            raise ExtractionError("PPTX 图表数据点索引重复或超出声明范围。")
        value = point.find(f"{C}v")
        # A missing <v> and an explicitly empty <v/> are both visibly blank;
        # neither is interpreted as the number zero.
        values[index] = value.text if value is not None and value.text is not None else ""
        chars += len(values[index])
        _check_chars(chars, output)
        if len(values) > limit:
            raise ExtractionError("PPTX 图表数据点数量超过表格上限，请拆分文档。")
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
        output.warn("PPTX 图表标题或系列名称有多个缓存项，按索引保留，请核对。")
    return " / ".join(cache.values[index] for index in sorted(cache.values)).strip()


def _cell(cache: _Cache, index: int) -> str:
    if index not in cache.values:
        return "[缓存缺失]"
    return cache.values[index] if cache.values[index] != "" else "[空值]"


def extract_pptx_chart(chart, output, *, origin: str) -> None:
    """Emit a chart's native data into the existing Office _Output instance.

    Use from the shape.has_chart branch before continuing to the next shape.
    _Output owns cumulative limits and insertion order. No package relationships,
    embedded workbook content, external URLs, or model calls are accessed here.
    """
    root = chart._chartSpace
    body = root.find(f"{C}chart")
    if body is None:
        raise ExtractionError("PPTX 图表缺少主体。")
    title = _text(body.find(f"{C}title/{C}tx"), output)
    heading = "### 原生图表" + ("：" + title if title else "")
    output.text(heading, origin=origin + "/title")
    output.warn("PPTX 原生图表读取已保存的数据缓存，未重算或访问源工作簿；缓存可能不是最新值。")
    output.warn("PPTX 图表外观、数据标签、趋势线和误差线未渲染；数据表保留缓存原值，显示格式仅注明，请核对。")
    plot_area = body.find(f"{C}plotArea")
    if plot_area is None:
        output.warn("PPTX 原生图表缺少绘图区，未提取图表数据。")
        return
    for axis in plot_area:
        kind = axis.tag.removeprefix(C) if isinstance(axis.tag, str) else ""
        if kind in _AXIS_NAMES:
            text = _text(axis.find(f"{C}title/{C}tx"), output)
            if text:
                output.text(f"{_AXIS_NAMES[kind]}标题：{text}", origin=origin + "/axis")
    plots = [element for element in plot_area
             if isinstance(element.tag, str) and element.tag.startswith(C) and element.tag.endswith("Chart")]
    if not plots:
        output.warn("PPTX 原生图表类型不受支持，未提取图表数据，请核对原图。")
    for plot_number, plot in enumerate(plots, 1):
        kind = plot.tag.removeprefix(C)
        plot_origin = f"{origin}/plot-{plot_number}"
        if kind not in _CATEGORY_PLOTS:
            output.text(f"图表类型：{kind}（尚不支持原生数据提取）", origin=plot_origin)
            output.warn(f"PPTX 图表类型 {kind} 暂未提取数据，请另存为图片或 PDF 后识别。")
            continue
        description = _CATEGORY_PLOTS[kind]
        direction = plot.find(f"{C}barDir")
        if direction is not None:
            description += "（" + {"col": "竖向", "bar": "横向"}.get(direction.get("val"), "方向未识别") + "）"
        grouping = plot.find(f"{C}grouping")
        if grouping is not None:
            description += "；分组：" + _GROUPINGS.get(grouping.get("val"), grouping.get("val", "未指定"))
        output.text(f"图表类型：{description}", origin=plot_origin)
        series_items = plot.findall(f"{C}ser")
        if not series_items:
            output.warn("PPTX 原生图表没有可提取的数据系列。")
        for series_number, series in enumerate(series_items, 1):
            series_origin = f"{plot_origin}/series-{series_number}"
            name = _text(series.find(f"{C}tx"), output)
            category = _cache(series.find(f"{C}cat"), output)
            values = _cache(series.find(f"{C}val"), output)
            rows = max(category.count, values.count)
            # Reserve the full rendered grid before range/list expansion.
            output.count_cells(3 * (rows + 1))
            info = ["#### 系列：" + (name or f"系列 {series_number}（名称未保存）")]
            if category.format_code:
                info.append("类别显示格式：" + category.format_code)
            if values.format_code:
                info.append("数值显示格式：" + values.format_code)
            if (not category.present or not values.present
                    or category.count != values.count
                    or len(category.values) != rows or len(values.values) != rows):
                output.warn("PPTX 图表数据缓存缺失、不完整或类别与数值数量不一致，已逐点标记缺失；未编造零值。")
            if any(value == "" for value in (*category.values.values(), *values.values.values())):
                output.warn("PPTX 图表缓存包含空值，已用[空值]标记，不视为零。")
            if rows == 0:
                info.append("[无可用图表数据缓存]")
                output.warn("PPTX 原生图表没有可用的数据缓存，未读取或重算源工作簿。")
            else:
                table = [["数据点", "类别", "数值（缓存原文）"]]
                table.extend([[str(index + 1), _cell(category, index), _cell(values, index)] for index in range(rows)])
                info.append(markdown_table(table))
            output.text("\n\n".join(info), origin=series_origin)
