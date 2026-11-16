import re
from collections.abc import Generator
from pathlib import PurePosixPath
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage

from document_md.dify_vision import DifyVision
from document_md.types import ExtractionError, Options


def _bounded_integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= maximum or int(value) != value:
        raise ExtractionError(f"{name} 必须是 {minimum}–{maximum} 的整数。")
    return int(value)


def _markdown_filename(filename: str) -> str:
    name = PurePosixPath(filename.replace("\\", "/")).name
    stem = PurePosixPath(name).stem
    stem = re.sub(r'[\x00-\x1f\x7f/\\:"<>|?*]', "_", stem).strip(" .")[:120]
    return f"{stem or 'document'}.md"


class ConvertTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage, None, None]:
        from document_md.engine import convert_document

        file = tool_parameters.get("file")
        if file is None or isinstance(file, (list, tuple, dict, str)):
            raise ExtractionError("请传入一个 Dify 文件对象，每次仅支持单个文件。")
        filename = getattr(file, "filename", None)
        if not isinstance(filename, str) or not filename.strip():
            raise ExtractionError("上传文件缺少文件名，请保留文件扩展名后重新上传。")
        options = Options(
            max_pages=_bounded_integer(tool_parameters.get("max_pages", 20), "最大页数", 1, 20),
            max_images=_bounded_integer(tool_parameters.get("max_images", 24), "最多识别图片数", 0, 24),
        )
        known_size = getattr(file, "size", None)
        if isinstance(known_size, (int, float)) and known_size > options.max_file_bytes:
            raise ExtractionError("文件超过 20 MiB 上限，请拆分后再转换。")
        try:
            data = file.blob
        except Exception:
            raise ExtractionError("无法读取上传文件，请检查 Dify 文件服务是否可被插件访问。") from None
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise ExtractionError("上传文件为空或无法读取。")
        if len(data) > options.max_file_bytes:
            raise ExtractionError("文件超过 20 MiB 上限，请拆分后再转换。")
        selection = tool_parameters.get("vision_model")
        vision = DifyVision(self.session, selection, max_output_chars=options.max_output_chars) if selection else None
        try:
            result = convert_document(bytes(data), filename, vision=vision, options=options)
        except ExtractionError:
            raise
        except Exception:
            raise ExtractionError("文档转换失败，请检查文件格式、完整性与是否加密；本次未返回部分文档。") from None
        # Nothing is emitted until every page/image finishes successfully.
        yield self.create_text_message(result.markdown)
        yield self.create_blob_message(
            blob=result.markdown.encode("utf-8"),
            meta={"mime_type": "text/markdown", "filename": _markdown_filename(filename)},
        )
        yield self.create_variable_message("markdown", result.markdown)
        yield self.create_variable_message("documents", result.documents)
        yield self.create_variable_message("warnings", result.warnings)
        yield self.create_variable_message("stats", result.stats)
