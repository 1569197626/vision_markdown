# Vision Markdown for Dify

[Simplified Chinese](README.zh_Hans.md) · [Repository](https://github.com/1569197626/vision_markdown) · [Issues and contact](https://github.com/1569197626/vision_markdown/issues) · [Privacy](PRIVACY.md)

`ninebot/vision_markdown:0.1.7` combines native text, tables, and image recognition results into Markdown in document order. Images are processed through a vision model already configured in the Dify workspace. Dependencies do not include Docling, PyTorch, Triton, NVIDIA CUDA, or local model weights.

This plugin produces document conversions that users can check against the source. Review the text, tables, and charts in representative business files. Successful installation or passing local tests does not establish OCR accuracy for arbitrary documents.

## Downloads

- [Original 0.1.6 GitHub release](https://github.com/1569197626/vision_markdown/releases/tag/v0.1.6): unchanged original package using SDK 0.6.0.
- [0.1.7 Marketplace submission package](https://github.com/1569197626/vision_markdown/raw/refs/heads/main/packages/vision_markdown-0.1.7.difypkg): SDK 0.9.0, English diagnostics and Marketplace metadata. Listing is pending review.
- [0.1.7 SHA256 checksum](https://github.com/1569197626/vision_markdown/blob/main/packages/vision_markdown-0.1.7.sha256).

## Usage

Target environment: Dify 1.17.1, plugin-daemon 0.6.10, and Python 3.12. The interfaces were checked against those versions and Dify SDK 0.9.0. The `model-selector` uses the `llm&vision` scope to select an existing workspace vision model; no additional model API key is required in this plugin.

English tool labels, documentation, errors, and warnings are provided. Document content retains its original language.

1. Install the `.difypkg` through Dify's plugin installation from a local file, following your workspace's installation policy. If the environment rejects an unsigned package, keep the error for diagnosis. This project does not change signature verification settings.
2. Create a separate test workflow: file input → **Vision Markdown** → output.
3. Bind the uploaded file to **File**. Select a configured model that accepts images in **Workspace vision model**. The model may be left empty for files that contain only native text.
4. Read the `markdown` variable or download the returned `.md` file. Also inspect `warnings` and `stats`.
5. Check a one- or two-page file for native text, text in images, tables, text after images, and reading order before connecting a knowledge-base chunker and a text embedding model such as BGE-M3. No additional whole-page OCR step is needed after this conversion.

**Upload entry points:** The host Dify application determines which file extensions its workflow or knowledge-base upload page accepts. The plugin's ability to convert PNG, JPEG, or WebP does not mean a particular upload entry point accepts images. A file rejected by the host before tool invocation has not reached this plugin.

Start with the default model parameters. The default output limit is 4,096 tokens per image; it can be changed in the model selector up to 8,192. The model must reliably follow the completion-marker instruction. A missing marker, an exhausted output limit, or a model error fails the entire file; previously recognized pages are not returned as a successful partial document. These checks cannot prove that a model has not omitted or misread source content.

## Supported formats and limitations

| Format | Processing | Main limitations |
| --- | --- | --- |
| PDF | Extract the text layer; render embedded-image/vector regions for the vision model; invoke the model once for each textless scanned page | Reading order is inferred from coordinates. Review columns, complex tables, and overlapping content. Encrypted PDFs are unsupported. |
| DOCX | Extract body paragraphs, tables, images, and referenced headers, footers, footnotes, and endnotes | Referenced extra content is collected separately with its source. Actual page numbers and dynamic fields are not reconstructed. Review comments, floating objects, and complex layouts. |
| PPTX | Extract text, tables, images, and common native category-chart cached data per slide, ordered by shape position | No PowerPoint rendering or chart recalculation. Chart appearance, scatter/bubble charts, and SmartArt have explicit limitations. |
| XLSX | Convert worksheets into Markdown tables; send embedded images to the vision model | Formulas are not executed. Warnings describe formula-cache, image-position, and chart limitations. |
| CSV | Convert original values into a Markdown table | The first row becomes the header. Formulas are not executed. |
| TXT / MD | Read text directly | UTF-8 and BOM-marked UTF-16; GB18030 fallback emits a warning. |
| PNG / JPEG / WebP | Normalize and send to the vision model | Single frame, resized before sending. Text and detail accuracy depend on the model. |

Legacy DOC/PPT/XLS, HTML, email, and batch conversion of archives are unsupported. Save legacy Office files in a modern format first. This is not a lossless converter for arbitrary formats.

When native text overlaps a PDF image region, the already extracted text positions are masked white to reduce duplicate recognition. Masking can affect graphic details in those positions. Complete regular native grids can be reconstructed directly as Markdown tables, but merged cells, curves, fills, and complex overlapping tables are not guaranteed to be preserved. Validate complex tables and complete chart relationships separately.

## Outputs

- `markdown`: the combined full text.
- Default tool text and downloadable `.md` file: the same content as `markdown`.
- `documents`: `[{page_content, metadata, provider, vector, children}]`, following the official Dify Extractor document shape. Each input file produces one Document for later chunking. For example, connect `markdown` to a general text chunker and then a knowledge base.
- `warnings`: limitations involving layout, formulas, and model recognition.
- `stats`: native-character count, page/logical-page count, image count, vision-call count, output length, and model-reported token usage.

## Resource limits and failure behavior

Each invocation processes one file. Default upper limits are 20 MiB, 20 pages/slides, 24 image regions to recognize, 20,000 table cells, and 500,000 output characters. DOCX pages are logical pages defined by explicit page breaks; XLSX worksheets are not print pages. Each image is limited to 20 million pixels and resized to a maximum long edge of 1,800 pixels before sending. All normalized images together must fit within 32 MiB. Office archive parsing also checks entry count, expanded size, compression ratio, and XML entities.

Exceeding a limit produces an explicit error. Images are recognized sequentially within a file. The plugin processes at most two requests concurrently. The SDK request timeout is 600 seconds, but the Dify API, gateway, or model service may impose a shorter timeout; split large documents first. Python and image decoding still use runtime memory. Production RSS has not been measured, so a specific memory footprint is not promised.

The model receives only the images that need recognition and fixed conversion instructions, without the full native text. Images are sent to the selected model's server and may incur model charges. The plugin does not execute instructions found in documents, run Office macros or formulas, or proactively visit document links. See [Privacy](PRIVACY.md).

## Changes through 0.1.7

### 0.1.7

Use Dify SDK 0.9.0 to meet the current Marketplace dependency floor. Add source-repository and maintainer-contact metadata, English runtime diagnostics, bilingual documentation, and portable packaging. The conversion algorithms, limits, newline normalization, model permissions, and output fields are unchanged. Generated Markdown labels (for example Footnote, Header, Slide and chart/table headings), warnings, errors, and model-context wording are now English; source document content is not translated. Generated output text therefore differs from 0.1.6, and longer labels can reach existing character limits sooner. Real-model responses may also vary with translated context wording. Version 0.1.6 remains available as its exact original GitHub release binary.

SDK 0.9.0 has been tested locally with synthetic inputs and model substitutes. The original 0.1.6 was validated on self-hosted Dify 1.17.1; the new 0.1.7 has not yet been installed on that live environment or tested on Dify Cloud.

### 0.1.6

Normalize actual CRLF (`\r\n`) and standalone CR (`\r`) to LF (`\n`) before combining text blocks. Native extraction and model responses therefore use the same newline convention. Internal consecutive blank lines, punctuation, repeated paragraphs, Markdown indentation, inline spaces, and literal backslash sequences are preserved. Existing trimming at block boundaries is unchanged. `markdown`, default text, the downloadable file, and `documents.page_content` share the normalized result. No chunker, dependency, permission, or model-parameter changes are included.

### 0.1.5

Add referenced Word headers/footers and footnotes/endnotes. Body references are retained; extra content is collected by source and applicable section, and shared content is extracted once. Per-page placement and dynamic page numbers are not simulated.

Add titles, axis titles, chart types, series, and cached data for common native PowerPoint category charts while retaining category/value associations and marking missing caches explicitly. Data is extracted natively without extra vision calls. Source workbooks are neither accessed nor recalculated. Chart appearance, data labels, trendlines, error bars, and unsupported types such as scatter/bubble charts are not reconstructed. This release did not change dependencies, model calls, plain-text newlines, or downstream chunking.

### 0.1.4

Fix punctuation-related PDF breaks, regular two-column and rotated-page order, and image placement after masking. Add native Markdown extraction for complete regular table grids. Fix inherited DOCX list styles, trailing styled-but-empty XLSX cells, and common currency formatting. Column detection remains coordinate-based. Native table reconstruction is limited to complete grids without fills or overlapping images. Only pure-white edges are cropped after masking, preserving pale image content.

### 0.1.3

Fix silent loss of native Chinese text when Linux PDFium returns degenerate glyph bounds for some unembedded fonts. Fall back from tight glyph bounds to font-range bounds with a warning; fail explicitly when neither is usable. Loading the candidate code in temporary processes using existing interpreters in two Linux plugin pods restored all native text in the same synthetic PDF without changing its order. This validation did not write production files or restart services.

### 0.1.2

Include the selector fix below and remove artificial space inference in native PDF text, which could split numbers such as `173`, words such as `picture`, or Chinese phrases. The PDFium text stream is used; PDFium may still normalize source whitespace and layout.

### 0.1.1

Fix an empty model dropdown. Dify requires `llm` to select the language-model list and `vision` to filter vision capability. Version 0.1.0 declared only `vision`, so a configured model could be absent from the dropdown. This metadata-only fix did not change credentials or server settings. See the [Dify 1.17.1 model-selector source](https://github.com/langgenius/dify/blob/1.17.1/web/app/components/plugins/plugin-detail-panel/model-selector/index.tsx).

## Local development and validation

Run these commands from the repository root, which contains `manifest.yaml` and `scripts/`:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --only-binary=:all: -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
.venv/bin/python scripts/package.py --cli /path/to/dify-plugin-cli --output ./dist/vision_markdown-0.1.7.difypkg
```

Replace `/path/to/dify-plugin-cli` with an official CLI executable. The explicit `--output` keeps release output inside this repository. The packaging script stages an allowlist of runtime code, icons, and documentation for the official CLI; it excludes `.venv`, caches, tests, and local credentials. Dify downloads dependencies during installation; Python libraries are not bundled.

Unit tests use synthetic documents and model substitutes. They cover native/scanned/mixed-content routing, order, duplicate-reduction behavior, limits, SDK messages, and failures. These tests establish program behavior; real-model Chinese OCR, chart descriptions, and production-platform behavior require separate end-to-end validation. A regression run used 15 frozen synthetic Chinese/English files with separately recorded results. It does not establish lossless conversion of arbitrary documents.

## References

- [Official Dify SDK repository](https://github.com/langgenius/dify-plugin-sdks)
- [Dify 1.17.1 model-selector parameter definitions](https://github.com/langgenius/dify/blob/1.17.1/api/core/entities/parameter_entities.py)
- [Official Extractor document output structure](https://github.com/langgenius/dify-official-plugins/blob/main/tools/dify_extractor/tools/document.py)
- [PDFium Python API and thread-safety requirements](https://pypdfium2.readthedocs.io/en/stable/python_api.html)
- [Official packaging CLI 0.6.10](https://github.com/langgenius/dify-plugin-daemon/releases/tag/0.6.10)

For questions or reproducible bug reports, use [repository Issues](https://github.com/1569197626/vision_markdown/issues). Do not include credentials or confidential source documents in public reports.


Maintainer contact: 1569197626@qq.com.
