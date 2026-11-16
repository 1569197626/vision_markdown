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
        raise ExtractionError(f"{name} must be an integer from {minimum} to {maximum}.")
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
            raise ExtractionError("Provide one Dify file object. Only one file is supported per request.")
        filename = getattr(file, "filename", None)
        if not isinstance(filename, str) or not filename.strip():
            raise ExtractionError("The uploaded file has no filename. Upload it again with its file extension.")
        options = Options(
            max_pages=_bounded_integer(tool_parameters.get("max_pages", 20), "Maximum pages", 1, 20),
            max_images=_bounded_integer(tool_parameters.get("max_images", 24), "Maximum images to recognize", 0, 24),
        )
        known_size = getattr(file, "size", None)
        if isinstance(known_size, (int, float)) and known_size > options.max_file_bytes:
            raise ExtractionError("The file exceeds the 20 MiB limit. Split it before conversion.")
        try:
            data = file.blob
        except Exception:
            raise ExtractionError("The uploaded file could not be read. Check whether the plugin can access the Dify file service.") from None
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise ExtractionError("The uploaded file is empty or unreadable.")
        if len(data) > options.max_file_bytes:
            raise ExtractionError("The file exceeds the 20 MiB limit. Split it before conversion.")
        selection = tool_parameters.get("vision_model")
        vision = DifyVision(self.session, selection, max_output_chars=options.max_output_chars) if selection else None
        try:
            result = convert_document(bytes(data), filename, vision=vision, options=options)
        except ExtractionError:
            raise
        except Exception:
            raise ExtractionError("Document conversion failed. Check the file format, integrity, and encryption. No partial document was returned.") from None
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
