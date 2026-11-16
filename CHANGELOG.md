# Changelog

## 0.1.7

- Pin Dify SDK 0.9.0 for current Marketplace requirements.
- Add source repository and public maintainer contact metadata.
- Provide English generated diagnostics, Markdown labels, model-context wording, and bilingual documentation. Source document content is not translated; generated label text is a visible output change.
- Preserve conversion algorithms, resource limits, output fields, permissions, and newline normalization.
- Keep the original 0.1.6 package in its GitHub Release.
- Local SDK tests do not establish live Dify 1.17.1 or Dify Cloud compatibility; 0.1.7 has not been installed on the original live environment.

## 0.1.6

- Normalize CRLF and standalone CR to LF in the shared Markdown output without changing internal blank lines, punctuation, repeated paragraphs, indentation, or literal backslash sequences.
- Keep the existing chunker, dependencies, permissions, and model settings unchanged.
- Public distribution includes English README/privacy text, a Chinese README, source/contact links, and a portable packaging output path. Runtime Python code, plugin metadata, and pinned dependencies match the previously validated 0.1.6 package. The GitHub 0.1.6 release keeps the exact originally validated binary and its original checksum; the repository includes additional bilingual documentation.
- Runtime errors and warnings remain in Chinese. Dify Cloud has not been tested.

See README.md for earlier changes and format limitations.
