"""Newline compatibility without rewriting text or changing the chunker."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from dify_plugin.entities.tool import ToolRuntime

from document_md import engine
from document_md.types import Block, Options, ParsedDocument, VisionResult
from tools.convert import ConvertTool


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "gb18030"])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_text_encodings_keep_paragraphs_punctuation_and_literal_escapes(encoding, newline):
    expected = "标题 Title。\n\n第一段。金额-12.50。\n\n\n第二段。重复。重复。\n字面量 \\n 和 \\r\\n"
    source = expected.replace("\n", newline)
    result = engine.convert_document(source.encode(encoding), "sample.txt", None, Options())
    assert result.markdown == expected
    assert result.documents[0]["page_content"] == expected
    assert result.stats["output_chars"] == len(expected)
    assert result.stats["vision_calls"] == 0


def test_mixed_markdown_newlines_keep_blank_lines_code_and_hard_breaks():
    source = '# 标题\r\n\r\n段落。  \r下一行。\n\n\n```python\r\nprint("\\n")\r```'
    expected = '# 标题\n\n段落。  \n下一行。\n\n\n```python\nprint("\\n")\n```'
    result = engine.convert_document(source.encode(), "mixed.md", None, Options())
    assert result.markdown == expected


def test_native_and_vision_blocks_share_newline_convention(monkeypatch):
    blocks = [Block(kind="text", text="原生第一段。\r\n\r\n原生第二段。"),
              Block(kind="image", image=b"image", origin="fake"),
              Block(kind="text", text="后文。\r结尾。")]
    monkeypatch.setattr(engine, "parse_document", lambda *args: ParsedDocument("a.pdf", "pdf", blocks=blocks, page_count=1))
    result = engine.convert_document(b"source", "a.pdf", lambda _: VisionResult("图片第一段。\r\n\r图片第二段。"), Options())
    assert result.markdown == "原生第一段。\n\n原生第二段。\n\n图片第一段。\n\n图片第二段。\n\n后文。\n结尾。"
    assert result.stats["output_chars"] == len(result.markdown)
    assert result.stats["vision_calls"] == 1


def test_csv_quoted_crlf_keeps_one_table_row_and_cell_breaks():
    source = '键,内容\r\nA,"第一行。\r\n\r\n第二行。"\r\nB,"字面量 \\n"\r\n'
    result = engine.convert_document(source.encode(), "sample.csv", None, Options())
    assert result.markdown == "| 键 | 内容 |\n| --- | --- |\n| A | 第一行。<br><br>第二行。 |\n| B | 字面量 \\\\n |"
    assert "\r" not in result.markdown


def test_tool_text_file_markdown_and_document_outputs_are_identical():
    source = "标题。\r\n\r\n段落一。\r\n\r\n段落二。".encode()
    tool = ConvertTool(runtime=ToolRuntime(credentials={}, user_id=None, session_id=None), session=SimpleNamespace())
    messages = list(tool._invoke({"file": SimpleNamespace(filename="sample.txt", blob=source)}))
    values = {message.message.variable_name: message.message.variable_value for message in messages[2:]}
    expected = "标题。\n\n段落一。\n\n段落二。"
    assert messages[0].message.text == expected
    assert messages[1].message.blob == expected.encode("utf-8")
    assert values["markdown"] == values["documents"][0]["page_content"] == expected
    assert values["stats"]["output_chars"] == len(expected)


def test_frozen_gb18030_fixture_retains_all_twelve_paragraphs():
    root = Path(__file__).resolve().parent / "fixtures"
    expected = json.loads((root / "expected/09.json").read_text())
    result = engine.convert_document((root / "inputs" / expected["filename"]).read_bytes(), expected["filename"], None, Options())
    assert result.markdown == expected["source_text"].strip()
    assert [len(part) for part in result.markdown.split("\n\n")] == [95, 252, 227, 218, 225, 18, 209, 257, 232, 216, 18, 9]
    assert result.markdown.count("。") == 39
    assert result.markdown.count("重复条款：每次检验都要记录实际结果。") == 2
    assert "\r" not in result.markdown and result.markdown.count("\n\n") == 11
    assert result.stats["vision_calls"] == 0
