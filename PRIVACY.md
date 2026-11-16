# Data processing and privacy

Repository: [1569197626/vision_markdown](https://github.com/1569197626/vision_markdown). Contact: [repository Issues](https://github.com/1569197626/vision_markdown/issues).

The plugin does not collect personal data for analytics or an independent service. Uploaded documents may contain personal data; they are processed only for the requested conversion.

The plugin reads uploaded file objects supplied by Dify and parses native text, tables, and images within the plugin process. It does not independently retain input documents or write business-content logs. Dify's own file storage, workflow-run records, and log-retention policies still apply.

Images requiring recognition are sent through Dify's model invocation capability to the workspace model selected by the user. The model provider may be an internal service or a third party. Billing and data handling depend on the workspace's existing configuration and the selected provider. The plugin does not request a separate model API key, write credentials into output or logs, or automatically select an external model.

The model receives normalized images and fixed conversion instructions. PDF image regions may contain part of a page; scanned pages are sent as whole-page images. Natively extracted text is not proactively appended to the model request.

Generated Markdown text, downloadable files, document metadata, warnings, and model token usage are returned to Dify. The plugin adds no analytics tracking or telemetry, downloads no model weights, and does not proactively visit document links or execute macros or formulas.

The host Dify application controls accepted upload extensions. Support for image conversion in this plugin does not establish that every knowledge-base or workflow upload entry point accepts image files.

Use [repository Issues](https://github.com/1569197626/vision_markdown/issues) for questions about this policy. Public reports should not contain credentials or confidential document content.
