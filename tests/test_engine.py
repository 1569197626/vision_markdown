import io
import zipfile

import pytest
from PIL import Image

from document_md import engine
from document_md.helpers import check_archive, image_block, markdown_table
from document_md.types import Block, ExtractionError, Options, ParsedDocument, VisionResult


def png(size=(10, 10)):
    data = io.BytesIO()
    Image.new("RGBA", size, (255, 0, 0, 128)).save(data, "PNG")
    return data.getvalue()


def archive(entries):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in entries:
            z.writestr(name, content)
    return data.getvalue()


def test_native_chinese_text_does_not_call_model():
    def forbidden(_):
        pytest.fail("native text must not call a model")
    result = engine.convert_document("# 标题\n\n重复\n重复".encode(), "说明.md", forbidden, Options())
    assert result.markdown == "# 标题\n\n重复\n重复"
    assert result.stats["vision_calls"] == 0
    assert result.documents[0]["metadata"]["filename"] == "说明.md"
    assert result.documents[0]["page_content"] == result.markdown


def test_csv_preserves_quoted_multiline_and_literal_formula():
    result = engine.convert_document('列一,列二\n"a|b","第二行\n续行"\n=SUM(A1:A2),2'.encode(), "a.csv", None, Options())
    assert "a\\|b" in result.markdown and "第二行<br>续行" in result.markdown
    assert "=SUM(A1:A2)" in result.markdown
    assert result.stats["vision_calls"] == 0


def test_mixed_content_keeps_order_and_repeated_text(monkeypatch):
    blocks = [Block(kind="text", text="before"), image_block(png(), Options(), origin="image-1"), Block(kind="text", text="after"), Block(kind="text", text="before")]
    monkeypatch.setattr(engine, "parse_document", lambda *a: ParsedDocument("a.pdf", "pdf", blocks=blocks, page_count=1))
    seen = []
    def vision(block):
        seen.append(block.origin)
        return VisionResult("recognized", {"total_tokens": 12})
    result = engine.convert_document(b"placeholder", "a.pdf", vision, Options())
    assert result.markdown == "before\n\nrecognized\n\nafter\n\nbefore"
    assert seen == ["image-1"] and result.stats["vision_calls"] == 1
    assert result.stats["model_usage"] == [{"total_tokens": 12}]


@pytest.mark.parametrize("option", [Options(max_images=0), Options(max_total_image_bytes=1)])
def test_limits_fail_before_any_billable_call(option):
    def forbidden(_):
        pytest.fail("validation must finish before model calls")
    with pytest.raises(ExtractionError):
        engine.convert_document(png(), "a.png", forbidden, option)


@pytest.mark.parametrize("result", [VisionResult("partial", truncated=True), VisionResult(" "), "not a VisionResult"])
def test_model_failures_never_return_success(result):
    with pytest.raises(ExtractionError):
        engine.convert_document(png(), "a.png", lambda _: result, Options())


def test_model_errors_do_not_echo_credentials():
    def fail(_):
        raise ValueError("secret-token-xyz")
    with pytest.raises(ExtractionError) as error:
        engine.convert_document(png(), "a.png", fail, Options())
    assert "secret-token" not in str(error.value)


def test_vision_required_only_for_images():
    with pytest.raises(ExtractionError, match="vision model"):
        engine.convert_document(png(), "a.png", None, Options())


def test_output_limit_is_not_silent_truncation():
    with pytest.raises(ExtractionError, match="output limit"):
        engine.convert_document(png(), "a.png", lambda _: VisionResult("x" * 11), Options(max_output_chars=10))


@pytest.mark.parametrize("data,name", [(b"", "a.txt"), (b"abc", "a.doc"), (b"abc", "a.html"), (b"hello\x00", "a.txt")])
def test_invalid_input(data, name):
    with pytest.raises(ExtractionError):
        engine.convert_document(data, name, None, Options())


def test_file_and_csv_cell_limits():
    with pytest.raises(ExtractionError):
        engine.convert_document(b"abcdef", "a.txt", None, Options(max_file_bytes=3))
    with pytest.raises(ExtractionError, match="CSV"):
        engine.convert_document(b"a,b\nc,d", "a.csv", None, Options(max_table_cells=3))


def test_image_normalization_resize_and_transparency():
    block = image_block(png((100, 50)), Options(max_image_side=40), origin="test")
    image = Image.open(io.BytesIO(block.image))
    assert image.size == (40, 20) and image.mode == "RGB"
    assert image.getpixel((0, 0)) == (255, 127, 127)
    assert image.getexif() == {} and block.origin == "test"


def test_pixel_and_frame_limits():
    with pytest.raises(ExtractionError, match="pixel"):
        image_block(png((11, 11)), Options(max_image_pixels=100))
    data = io.BytesIO()
    Image.new("RGB", (5, 5), "red").save(data, "GIF", save_all=True, append_images=[Image.new("RGB", (5, 5), "blue")])
    with pytest.raises(ExtractionError, match="Multi-frame"):
        image_block(data.getvalue(), Options())


@pytest.mark.parametrize("entries", [[("../evil.xml", "x")], [("a.xml", '<!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]><a/>')], [("a.xml", ("<!DOCTYPE a><a/>").encode("utf-16"))]])
def test_archive_rejects_unsafe_xml_and_paths(entries):
    with pytest.raises(ExtractionError):
        check_archive(archive(entries), Options())


def test_archive_limits_and_normal_archive():
    normal = archive([("word/document.xml", "<document>hello</document>")])
    check_archive(normal, Options())
    with pytest.raises(ExtractionError, match="expanded"):
        check_archive(normal, Options(max_archive_bytes=5))
    with pytest.raises(ExtractionError, match="entries"):
        check_archive(normal, Options(max_archive_entries=0))
    with pytest.raises(ExtractionError, match="compression ratio"):
        check_archive(archive([("large.xml", "x" * 100_000)]), Options(max_archive_ratio=10))


def test_markdown_table_ragged_rows_and_escaping():
    assert markdown_table([["A", "B"], ["pipe|slash\\\nnew"]]) == "| A | B |\n| --- | --- |\n| pipe\\|slash\\\\<br>new |  |"
