"""Actual DOCX supplemental stories; no network, renderer, or model needed."""
import io
import zipfile
from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree

import pytest
from docx import Document
from docx.enum.section import WD_SECTION_START
from docx.enum.text import WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches
from PIL import Image

from document_md.office import parse_office
from document_md.types import ExtractionError, Options

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_R = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT = "http://schemas.openxmlformats.org/package/2006/content-types"


def save(document):
    target = io.BytesIO()
    document.save(target)
    return target.getvalue()


def text(parsed):
    return "\n".join(block.text for block in parsed.blocks if block.kind == "text")


def png():
    buffer = io.BytesIO()
    Image.new("RGB", (20, 20), "red").save(buffer, "PNG")
    return buffer.getvalue()


def reference(paragraph, kind, note_id):
    element = OxmlElement("w:" + kind + "Reference")
    element.set(qn("w:id"), str(note_id))
    paragraph.add_run()._r.append(element)


def para(value):
    return "<w:p><w:r><w:t>" + value + "</w:t></w:r></w:p>"


def with_notes(data, notes, additions=None):
    """notes maps footnote/endnote to (ID, XML children, optional note type)."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        files = {entry.filename: archive.read(entry) for entry in archive.infolist()}
    rels = ElementTree.fromstring(files["word/_rels/document.xml.rels"])
    types = ElementTree.fromstring(files["[Content_Types].xml"])
    for kind, entries in notes.items():
        ElementTree.SubElement(rels, "{" + PKG_R + "}Relationship", {
            "Id": "rIdTest" + kind, "Type": R + "/" + kind + "s", "Target": kind + "s.xml"})
        ElementTree.SubElement(types, "{" + CONTENT + "}Override", {
            "PartName": "/word/" + kind + "s.xml",
            "ContentType": "application/vnd.openxmlformats-officedocument.wordprocessingml." + kind + "s+xml"})
        content = []
        for entry in entries:
            note_id, children, *types_ = entry
            type_attribute = f' w:type="{types_[0]}"' if types_ else ""
            content.append(f'<w:{kind} w:id="{note_id}"{type_attribute}>{children}</w:{kind}>')
        files["word/" + kind + "s.xml"] = (f'<w:{kind}s xmlns:w="{W}" xmlns:r="{R}">' + "".join(content) + f'</w:{kind}s>').encode()
    files["word/_rels/document.xml.rels"] = ElementTree.tostring(rels)
    files["[Content_Types].xml"] = ElementTree.tostring(types)
    files.update(additions or {})
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    return target.getvalue()


def test_shared_header_footer_output_once_with_all_section_scopes():
    document = Document()
    document.sections[0].header.paragraphs[0].text = "HEADER-CONTENT 中文页眉"
    document.sections[0].footer.paragraphs[0].text = "FOOTER-CONTENT English footer"
    document.add_paragraph("BODY-FIRST")
    document.add_section(WD_SECTION_START.NEW_PAGE)
    document.add_paragraph("BODY-LAST")
    data = save(document)
    parsed = parse_office(data, "sections.docx", Options())
    result = text(parsed)
    assert result.count("HEADER-CONTENT") == result.count("FOOTER-CONTENT") == 1
    assert "Section 1/default、Section 2/default" in result
    assert result.index("BODY-FIRST") < result.index("BODY-LAST") < result.index("HEADER-CONTENT")
    assert all(block.page is None for block in parsed.blocks if block.origin.startswith(("docx/header/", "docx/footer/")))
    assert any("shared content is extracted once" in warning for warning in parsed.warnings)


def test_only_enabled_first_and_even_stories_are_extracted():
    document = Document()
    section = document.sections[0]
    section.header.paragraphs[0].text = "DEFAULT-HEADER"
    section.first_page_header.paragraphs[0].text = "FIRST-HEADER"
    section.even_page_header.paragraphs[0].text = "EVEN-HEADER"
    document.add_paragraph("BODY")
    result = text(parse_office(save(document), "disabled.docx", Options()))
    assert "DEFAULT-HEADER" in result and "FIRST-HEADER" not in result and "EVEN-HEADER" not in result
    section.different_first_page_header_footer = True
    document.settings.odd_and_even_pages_header_footer = True
    result = text(parse_office(save(document), "enabled.docx", Options()))
    assert all(value in result for value in ["DEFAULT-HEADER", "FIRST-HEADER", "EVEN-HEADER"])
    assert "Section 1/first page" in result and "Section 1/even pages" in result


def test_later_section_can_enable_inherited_first_header_and_override_default():
    document = Document()
    document.sections[0].header.paragraphs[0].text = "FIRST-DEFAULT"
    document.sections[0].first_page_header.paragraphs[0].text = "INHERITED-FIRST"
    document.add_paragraph("ONE")
    section = document.add_section(WD_SECTION_START.NEW_PAGE)
    section.different_first_page_header_footer = True
    section.header.is_linked_to_previous = False
    section.header.paragraphs[0].text = "SECOND-DEFAULT"
    document.add_paragraph("TWO")
    parsed = parse_office(save(document), "inherit.docx", Options())
    result = text(parsed)
    assert all(result.count(value) == 1 for value in ["FIRST-DEFAULT", "SECOND-DEFAULT", "INHERITED-FIRST"])
    assert "Section 2/first page" in result and "Section 1/first page" not in result


def test_notes_keep_inline_references_first_reference_order_and_repeat_body_once():
    document = Document()
    paragraph = document.add_paragraph("BEFORE")
    reference(paragraph, "endnote", 7)
    paragraph.add_run("MIDDLE")
    reference(paragraph, "footnote", 2)
    paragraph.add_run("AFTER")
    reference(document.add_paragraph("AGAIN"), "footnote", 2)
    data = with_notes(save(document), {
        "footnote": [(-1, para("DO-NOT-READ-NEGATIVE"), "separator"), (0, para("DO-NOT-READ-SEPARATOR"), "continuationSeparator"), (2, para("FOOTNOTE-TEXT 中文")), (3, para("UNREFERENCED-TEXT"))],
        "endnote": [(7, para("ENDNOTE-TEXT English"))],
    })
    parsed = parse_office(data, "notes.docx", Options())
    result = text(parsed)
    assert "BEFORE【Endnote 7】MIDDLE【Footnote 2】AFTER" in result
    assert result.count("【Footnote 2】") == 2
    assert result.count("FOOTNOTE-TEXT") == result.count("ENDNOTE-TEXT") == 1
    assert result.index("ENDNOTE-TEXT") < result.index("FOOTNOTE-TEXT")
    assert all(value not in result for value in ["DO-NOT-READ", "UNREFERENCED-TEXT"])
    assert all(block.page is None for block in parsed.blocks if block.origin.startswith(("docx/footnote-", "docx/endnote-")))


def test_table_reference_and_note_table_are_preserved():
    document = Document()
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Serial 00123"
    reference(table.cell(0, 1).paragraphs[0], "footnote", 1)
    separate = Document()
    note_table = separate.add_table(rows=2, cols=2)
    for cell, value in zip([c for row in note_table.rows for c in row.cells], ["项目", "值", "Voltage", "48 V"]):
        cell.text = value
    data = with_notes(save(document), {"footnote": [(1, para("NOTE-LEAD") + note_table._tbl.xml)]})
    parsed = parse_office(data, "table-notes.docx", Options())
    result = text(parsed)
    assert "| Serial 00123 | 【Footnote 1】 |" in result
    assert "| Voltage | 48 V |" in result
    with pytest.raises(ExtractionError, match="cell"):
        parse_office(data, "table-notes.docx", replace(Options(), max_table_cells=5))


def test_headers_and_note_images_use_their_own_relationships_and_limits():
    document = Document()
    document.sections[0].header.paragraphs[0].add_run().add_picture(io.BytesIO(png()), width=Inches(.2))
    reference(document.add_paragraph("BODY"), "footnote", 1)
    drawing = f'<w:p><w:r><w:drawing xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:blip r:embed="rIdPic"/></w:drawing></w:r></w:p>'
    rels = f'<Relationships xmlns="{PKG_R}"><Relationship Id="rIdPic" Type="{R}/image" Target="media/image1.png"/></Relationships>'
    data = with_notes(save(document), {"footnote": [(1, para("NOTE-IMAGE") + drawing)]}, {"word/_rels/footnotes.xml.rels": rels.encode()})
    parsed = parse_office(data, "images.docx", Options())
    images = [block for block in parsed.blocks if block.kind == "image"]
    assert len(images) == 2
    assert images[0].origin.startswith("docx/header/") and images[1].origin.startswith("docx/footnote-1/")
    assert all(block.page is None for block in images)
    with pytest.raises(ExtractionError, match="image count"):
        parse_office(data, "images.docx", replace(Options(), max_images=1))


def test_missing_ambiguous_and_invalid_note_references_are_explicit():
    document = Document()
    for value in [5, 2, -1]:
        reference(document.add_paragraph("REF"), "footnote", value)
    data = with_notes(save(document), {"footnote": [(2, para("AMBIGUOUS-A")), (2, para("AMBIGUOUS-B"))]})
    parsed = parse_office(data, "missing.docx", Options())
    result = text(parsed)
    assert "【Footnote 5: body missing or reference not unique】" in result
    assert "【Footnote 2: body missing or reference not unique】" in result
    assert "【Footnote reference invalid】" in result
    assert "AMBIGUOUS-A" not in result and "AMBIGUOUS-B" not in result
    assert any("duplicate IDs" in warning for warning in parsed.warnings)
    assert any("invalid reference ID" in warning for warning in parsed.warnings)


def test_note_cycles_do_not_expand_forever_and_auxiliary_page_breaks_do_not_add_pages():
    document = Document()
    reference(document.add_paragraph("BODY"), "footnote", 1)
    children = para("CYCLE-NOTE") + '<w:p><w:r><w:br w:type="page"/><w:footnoteReference w:id="1"/></w:r></w:p>'
    data = with_notes(save(document), {"footnote": [(1, children)]})
    parsed = parse_office(data, "cycle.docx", replace(Options(), max_pages=1))
    assert text(parsed).count("CYCLE-NOTE") == 1 and parsed.page_count == 1
    assert any("excluded from the body page count" in warning for warning in parsed.warnings)


def test_supplemental_text_counts_toward_output_budget():
    document = Document()
    document.sections[0].header.paragraphs[0].text = "H" * 100
    document.add_paragraph("BODY")
    with pytest.raises(ExtractionError, match="native text"):
        parse_office(save(document), "limit.docx", replace(Options(), max_output_chars=90))
    document = Document()
    reference(document.add_paragraph("BODY"), "footnote", 1)
    data = with_notes(save(document), {"footnote": [(1, para("N" * 100))]})
    with pytest.raises(ExtractionError, match="native text"):
        parse_office(data, "note-limit.docx", replace(Options(), max_output_chars=90))


def test_no_headers_are_created_when_none_exist():
    document = Document()
    document.add_paragraph("BODY-ONLY")
    parsed = parse_office(save(document), "plain.docx", Options())
    assert [block.text for block in parsed.blocks] == ["BODY-ONLY"]
    assert not any("headers/footers are collected" in warning for warning in parsed.warnings)


def test_external_header_and_note_relationships_are_not_read():
    document = Document()
    document.sections[0].header.paragraphs[0].text = "LOCAL-HEADER"
    reference(document.add_paragraph("BODY"), "footnote", 1)
    data = with_notes(save(document), {"footnote": [(1, para("LOCAL-NOTE"))]})
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        files = {entry.filename: archive.read(entry) for entry in archive.infolist()}
    relationships = ElementTree.fromstring(files["word/_rels/document.xml.rels"])
    for relationship in relationships:
        if relationship.get("Type", "").endswith(("/header", "/footnotes")):
            relationship.set("TargetMode", "External")
            relationship.set("Target", "https://example.invalid/never-fetch.xml")
    files["word/_rels/document.xml.rels"] = ElementTree.tostring(relationships)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    parsed = parse_office(buffer.getvalue(), "external-stories.docx", Options())
    result = text(parsed)
    assert "LOCAL-HEADER" not in result and "LOCAL-NOTE" not in result
    assert "【Footnote 1: body missing or reference not unique】" in result
    assert any("header/footer has an external relationship" in w for w in parsed.warnings)
    assert any("note has an external relationship" in w for w in parsed.warnings)


def test_unique_note_reference_count_is_bounded_before_expanding_bodies():
    document = Document()
    for note_id in range(3):
        reference(document.add_paragraph("REF"), "footnote", note_id)
    with pytest.raises(ExtractionError, match="reference count"):
        parse_office(save(document), "note-limit.docx", replace(Options(), max_table_cells=2))


def test_note_cache_counts_unreferenced_records_and_combines_both_note_kinds():
    document = Document()
    reference(document.add_paragraph("BODY"), "footnote", 1)
    reference(document.add_paragraph("BODY-ENDNOTE"), "endnote", 1)
    data = with_notes(save(document), {
        "footnote": [(1, para("REFERENCED")), (2, para("UNREFERENCED"))],
        "endnote": [(1, para("REFERENCED-END")), (2, para("UNREFERENCED-END"))],
    })
    with pytest.raises(ExtractionError, match="note record count"):
        parse_office(data, "note-records.docx", replace(Options(), max_table_cells=3))


def test_long_note_reference_chain_is_processed_once_without_scanning_seen_keys(monkeypatch):
    from document_md import office

    class MembershipOnlyDict(dict):
        def __iter__(self):
            pytest.fail("Processing queued references must not rescan the growing seen-key dictionary")

    original_init = office._DocxExtras.__init__

    def init(context, document, output):
        original_init(context, document, output)
        context.references = MembershipOnlyDict()

    monkeypatch.setattr(office._DocxExtras, "__init__", init)
    document = Document()
    reference(document.add_paragraph("CHAIN"), "footnote", 1)
    notes = []
    for note_id in range(1, 101):
        next_id = note_id + 1 if note_id < 100 else 1
        children = para(f"NOTE-{note_id:03d}-CONTENT") + f'<w:p><w:r><w:footnoteReference w:id="{next_id}"/></w:r></w:p>'
        notes.append((note_id, children))
    parsed = parse_office(with_notes(save(document), {"footnote": notes}), "chain.docx", Options())
    result = text(parsed)
    positions = [result.index(f"NOTE-{note_id:03d}-CONTENT") for note_id in range(1, 101)]
    assert positions == sorted(positions)
    assert all(result.count(f"NOTE-{note_id:03d}-CONTENT") == 1 for note_id in range(1, 101))


def test_original_frozen_docx_fixture_recovers_header_and_real_footnote():
    project = Path(__file__).resolve().parent / "fixtures"
    fixture = project / "inputs/05_text_table_image.docx"
    parsed = parse_office(fixture.read_bytes(), fixture.name, Options())
    result = text(parsed)
    assert result.count("HEADER-05 维护资料 Maintenance reference") == 1
    assert result.count("FOOTNOTE-05 脚注内容 Footnote boundary check") == 1
    assert "脚注引用用于检查正文与页脚能力边界【Footnote 1】" in result
    assert result.count("REPEAT-05") == 2
    assert not any("headers and footers are not extracted" in w or "footnotes, endnotes, and comments are not extracted" in w for w in parsed.warnings)
