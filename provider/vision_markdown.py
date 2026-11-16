from typing import Any

from dify_plugin import ToolProvider


class VisionMarkdownProvider(ToolProvider):
    def _validate_credentials(self, credentials: dict[str, Any]) -> None:
        # Model credentials are managed by Dify, never by this provider.
        return None
