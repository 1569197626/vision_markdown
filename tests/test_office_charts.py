"""Native chart cache contracts, using real OOXML but no model or network."""
from pathlib import Path

import pytest
from pptx import Presentation
from pptx.chart.data import CategoryChartData, XyChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches

from document_md.office import _Output
from document_md.office_charts import C, extract_pptx_chart
from document_md.types import ExtractionError, Options


def chart_fixture(kind=XL_CHART_TYPE.COLUMN_CLUSTERED, categories=("Jan", "Feb", "Mar"),
                  series=(("Count", (4, 7, 5)),)):
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    data = CategoryChartData()
    data.categories = categories
    for name, values in series:
        data.add_series(name, values)
    chart = slide.shapes.add_chart(kind, Inches(1), Inches(1), Inches(6), Inches(4), data).chart
    chart.has_title = True
    chart.chart_title.text_frame.text = "Monthly count 月度数量"
    return chart


def series_element(chart, number=0):
    return list(chart._chartSpace.iter(f"{C}ser"))[number]


def extract(chart, options=None):
    output = _Output("chart.pptx", "pptx", options or Options())
    extract_pptx_chart(chart, output, origin="pptx/slide-1/shape-2")
    return output, "\n\n".join(block.text for block in output.document.blocks)


@pytest.mark.parametrize("kind,type_text", [
    (XL_CHART_TYPE.COLUMN_CLUSTERED, "column/bar chart"),
    (XL_CHART_TYPE.LINE, "line chart"),
    (XL_CHART_TYPE.PIE, "pie chart"),
])
def test_real_category_charts_keep_title_series_and_associations(kind, type_text):
    output, text = extract(chart_fixture(kind))
    assert "Monthly count 月度数量" in text and "Series: Count" in text and type_text in text
    assert "| 1 | Jan | 4 |" in text
    assert "| 2 | Feb | 7 |" in text
    assert "| 3 | Mar | 5 |" in text
    assert output.cells == 12
    assert all(block.kind == "text" for block in output.document.blocks)
    assert any("data cache" in item and "not accessed or recalculated" in item for item in output.document.warnings)


def test_frozen_06_extracts_the_chart_itself_without_using_slide_body_text():
    source = Path(__file__).resolve().parent / "fixtures/inputs/06_slides_chart.pptx"
    presentation = Presentation(source)
    chart = next(shape.chart for slide in presentation.slides for shape in slide.shapes if shape.has_chart)
    output, text = extract(chart)
    assert "Monthly count" in text and "Series: Count" in text
    assert all(row in text for row in ("| 1 | Jan | 4 |", "| 2 | Feb | 7 |", "| 3 | Mar | 5 |"))
    assert "PPTX-06-" not in text  # no nearby slide paragraph can satisfy these checks
    assert all(block.origin.startswith("pptx/slide-1/shape-2/") for block in output.document.blocks)
    assert output.images == 0


def test_multiple_series_keep_lexical_values_formats_and_identifiers():
    chart = chart_fixture(categories=("00123", "00077"), series=(("Amount", (-12.5, 0)), ("Rate", (.125, .5))))
    amount, rate = series_element(chart, 0), series_element(chart, 1)
    amount.find(f"{C}val/{C}numRef/{C}numCache/{C}pt/{C}v").text = "-12.50"
    rate.find(f"{C}val/{C}numRef/{C}numCache/{C}formatCode").text = "0.00%"
    _, text = extract(chart)
    assert "Series: Amount" in text and "Series: Rate" in text
    assert "| 1 | 00123 | -12.50 |" in text
    assert "| 2 | 00077 | 0 |" in text
    assert "Value display format: 0.00%" in text and "| 1 | 00123 | 0.125 |" in text


def test_sparse_cache_indices_do_not_shift_categories_or_invent_zero():
    chart = chart_fixture()
    series = series_element(chart)
    values = series.find(f"{C}val/{C}numRef/{C}numCache")
    values.remove(values.findall(f"{C}pt")[1])
    output, text = extract(chart)
    assert "| 1 | Jan | 4 |" in text and "| 2 | Feb | [cache missing] |" in text and "| 3 | Mar | 5 |" in text
    assert any("missing" in item for item in output.document.warnings)


def test_missing_cache_never_opens_external_or_embedded_workbook():
    chart = chart_fixture()
    reference = series_element(chart).find(f"{C}val/{C}numRef")
    reference.remove(reference.find(f"{C}numCache"))
    reference.find(f"{C}f").text = "https://example.invalid/private.xlsx!B1:B3"

    class XmlOnlyChart:
        _chartSpace = chart._chartSpace

        @property
        def part(self):
            pytest.fail("chart extraction must not dereference a source workbook")

    output, text = extract(XmlOnlyChart())
    assert text.count("[cache missing]") == 3 and "example.invalid" not in text
    assert "| 1 | Jan | [cache missing] |" in text
    assert any("cache is missing" in item for item in output.document.warnings)


def test_empty_cached_value_is_distinct_from_missing_and_zero():
    chart = chart_fixture(series=(("Values", (0, 1, 2)),))
    values = series_element(chart).find(f"{C}val/{C}numRef/{C}numCache")
    values.findall(f"{C}pt")[1].find(f"{C}v").text = None
    values.remove(values.findall(f"{C}pt")[2])
    output, text = extract(chart)
    assert "| 1 | Jan | 0 |" in text
    assert "| 2 | Feb | [empty] |" in text
    assert "| 3 | Mar | [cache missing] |" in text
    assert any("empty values" in item for item in output.document.warnings)


def test_chart_reserves_grid_budget_before_expanding_missing_points():
    chart = chart_fixture()
    with pytest.raises(ExtractionError, match="cell"):
        extract(chart, Options(max_table_cells=11))
    series_element(chart).find(f"{C}val/{C}numRef/{C}numCache/{C}ptCount").set("val", "999999999")
    with pytest.raises(ExtractionError, match="data-point count"):
        extract(chart)


@pytest.mark.parametrize("bad_index", ["0", "-1", "abc", "99999999999999"])
def test_duplicate_invalid_or_unbounded_point_index_is_rejected(bad_index):
    chart = chart_fixture()
    points = series_element(chart).find(f"{C}val/{C}numRef/{C}numCache").findall(f"{C}pt")
    points[1].set("idx", bad_index)
    with pytest.raises(ExtractionError, match="PPTX chart"):
        extract(chart)


def test_chart_value_and_title_lengths_respect_output_budget():
    chart = chart_fixture()
    series_element(chart).find(f"{C}val/{C}numRef/{C}numCache/{C}pt/{C}v").text = "9" * 1000
    with pytest.raises(ExtractionError, match="output limit"):
        extract(chart, Options(max_output_chars=500))
    chart.chart_title.text_frame.text = "长" * 1000
    with pytest.raises(ExtractionError, match="output limit"):
        extract(chart, Options(max_output_chars=500))


def test_axis_titles_are_extracted_and_combination_plots_stay_separate():
    from copy import deepcopy
    chart = chart_fixture()
    chart.category_axis.has_title = True
    chart.category_axis.axis_title.text_frame.text = "月份 Month"
    chart.value_axis.has_title = True
    chart.value_axis.axis_title.text_frame.text = "数量 Count"
    area = chart._chartSpace.find(f"{C}chart/{C}plotArea")
    second = deepcopy(area.find(f"{C}barChart"))
    second.tag = f"{C}lineChart"
    second.find(f"{C}ser/{C}tx/{C}strRef/{C}strCache/{C}pt/{C}v").text = "Line series"
    area.append(second)
    output, text = extract(chart)
    assert "Category axis title: 月份 Month" in text and "Value axis title: 数量 Count" in text
    assert "Series: Count" in text and "Series: Line series" in text
    assert any("/plot-2/series-1" in block.origin for block in output.document.blocks)


def test_scatter_is_explicitly_unsupported_not_misrepresented_as_categories():
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    data = XyChartData()
    series = data.add_series("XY")
    series.add_data_point(10, 20)
    chart = slide.shapes.add_chart(XL_CHART_TYPE.XY_SCATTER, Inches(1), Inches(1), Inches(6), Inches(4), data).chart
    output, text = extract(chart)
    assert "scatterChart" in text and "not supported" in text
    assert "| Category |" not in text and "| 1 |" not in text
    assert any("scatterChart" in item and "not supported" in item for item in output.document.warnings)


def test_missing_all_caches_is_visible_and_does_not_claim_an_empty_chart():
    chart = chart_fixture()
    series = series_element(chart)
    series.remove(series.find(f"{C}cat"))
    series.remove(series.find(f"{C}val"))
    output, text = extract(chart)
    assert "[no usable chart data cache]" in text
    assert any("no usable data cache" in item for item in output.document.warnings)
