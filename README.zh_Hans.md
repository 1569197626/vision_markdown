# Vision Markdown：用 Dify 视觉模型转换文档

[English](README.md) · [项目仓库](https://github.com/1569197626/vision_markdown) · [问题反馈与联系](https://github.com/1569197626/vision_markdown/issues) · [数据处理说明](PRIVACY.md)

`ninebot/vision_markdown:0.1.7`。将原生文字、表格和图片识别结果按顺序合并为 Markdown。图片识别通过 Dify 已配置的工作空间视觉模型执行；依赖中没有 Docling、PyTorch、Triton、NVIDIA CUDA 或本地模型权重。

插件用于可核对的文档转换；具体业务文件仍需检查原文、表格和图表。安装成功或本地测试通过不代表任意文档 OCR 准确率已验证。

## 使用方式

目标环境：Dify 1.17.1，plugin-daemon 0.6.10，Python 3.12。接口已按该版本源码与 SDK 0.9.0 核对。插件使用 `model-selector` 的 `llm&vision` 范围，选取工作空间已有视觉模型，无须在插件里重复填写 API Key。

0.1.7 提供英文工具标签、英文文档，以及英文错误、警告和辅助标签；用户文档内容保留原语言。

0.1.1 修复模型下拉列表为空的问题：Dify 前端需要用 `llm` 选择语言模型列表，再用 `vision` 筛选视觉能力；0.1.0 仅声明 `vision`，会出现模型已配置但下拉列表为空的情况。这次修复仅调整插件元数据，不改模型凭据或服务器配置。依据为 [Dify 1.17.1 插件模型选择器源码](https://github.com/langgenius/dify/blob/1.17.1/web/app/components/plugins/plugin-detail-panel/model-selector/index.tsx)。

0.1.2 同时包含上述修复，并去掉 PDF 原生文字的人工空格推断，避免把 `173`、`picture`、`24个月` 等内容拆开。现沿用 PDFium 提取的文字流；PDF 本身的空白与版式仍可能被 PDFium 归一化。

0.1.3 修复 Linux PDFium 对部分未嵌入字体返回退化字形边界时，原生中文被静默丢弃的问题。紧边界无法可靠定位时使用字体范围边界，并返回警告；两种边界均不可用时明确报错。已用两个 Linux 插件 Pod 的现有解释器在短期进程内加载候选源码验证，同一合成 PDF 的全部原生文字恢复且顺序不变；验证过程没有写入线上文件或重启服务。

0.1.4 修复 PDF 标点导致的断句、规则双栏及旋转页面顺序、遮罩后图片位置，新增完整规则网格的原生 Markdown 表格提取；修复 DOCX 继承列表样式、XLSX 尾部样式空白和常见金额格式。双栏仍使用坐标启发式；只对完整、无填充且无叠图的规则网格直接重建表格，复杂表格仍需核对。遮罩后只裁纯白边缘，避免丢弃浅色图像。

0.1.5 补齐 Word 已引用的页眉/页脚与脚注/尾注：正文保留引用标记，附属内容按来源和适用节集中列出，共享内容提取一次；不模拟逐页位置或动态页码。补齐 PPT 常见原生分类图表的标题、轴标题、类型、系列及缓存数据，保留类别和数值的关联，缺失缓存明确标记。图表数据直接原生提取，不增加视觉调用；不重算或访问源工作簿，也不还原图表外观、数据标签、趋势线、误差线及散点/气泡等尚未支持类型。依赖、模型调用、纯文本换行和下游分块行为均未改动。

0.1.6 在所有文本块合并输出前，将真实 CRLF（`\r\n`）和单独 CR（`\r`）统一成 LF（`\n`），使原生文字与视觉模型返回使用相同换行约定。保留内部连续空行、句号、重复段落、Markdown缩进/行内空格以及字面的反斜杠序列；既有块首尾空白清理规则不变。`markdown`、默认文本、下载文件和`documents.page_content`共用同一规范化结果。无分块器、依赖、权限或模型参数变更。

1. 经授权后，从 Dify「集成/插件 → 本地文件」上传 `.difypkg`。如果当前环境拒绝未签名包，先保留错误信息；本项目没有更改签名校验配置。
2. 新建独立的测试工作流：文件输入 → **文档转 Markdown** → 输出。
3. 工具的「文件」绑定上传文件；「工作空间视觉模型」选择已配置、支持图片的模型。纯文字文件可留空。
4. 输出 `markdown` 变量或下载工具返回的 `.md` 文件；同时查看 `warnings` 和 `stats`。
5. 先用一份 1–2 页文件核对文字、图中文字、表格、图后正文和阅读顺序，再接知识库分块及 BGE-M3。无需在这个节点后再整页 OCR。

**上传入口限制：** Dify 宿主应用决定工作流或知识库网页接受哪些扩展名。插件能够转换 PNG、JPEG、WebP，不代表具体网页上传入口允许图片；在宿主上传阶段被拒绝的文件尚未进入本插件。

推荐先使用模型参数默认值；单张图片默认输出上限为 4096 tokens，可在模型选择器中设置，当前允许最高 8192。模型必须能稳定遵循结束标记指令；标记缺失、输出达到上限或模型报错都会让整个文件转换失败，不会把已识别前几页作为成功文件返回。仍无法以程序证明模型没有漏字或误读。

## 第一版支持范围

| 文件 | 处理方式 | 主要边界 |
| --- | --- | --- |
| PDF | 有文字层时提取文字；嵌图/矢量区域渲染后调用模型；无文字的扫描页整页调用一次 | 阅读顺序按坐标推断；多栏、复杂表格、图文叠加需核对；不支持加密 PDF |
| DOCX | 正文段落、表格、图片，以及已引用的页眉/页脚、脚注/尾注 | 附属内容标注来源后集中列出；不恢复实际页码或动态域，批注/浮动复杂布局仍需核对 |
| PPTX | 逐页提取文字、表格、图片及常见分类图表缓存数据，按形状位置排序 | 不使用 PowerPoint 渲染或重算图表；图表外观、散点/气泡、SmartArt等仍有明确限制 |
| XLSX | 工作表转换成 Markdown 表格，嵌图送视觉模型 | 不执行公式；公式缓存/图片位置、图表等限制见转换警告 |
| CSV | 原始值转换为 Markdown 表格 | 第一行作为表头；不执行公式 |
| TXT / MD | 直接提取文本 | UTF-8/带 BOM 的 UTF-16，另支持带提示的 GB18030 回退 |
| PNG / JPEG / WebP | 规范化后调用视觉模型 | 单帧；先缩放，模型能力决定文字和细节准确度 |

暂不支持旧版 DOC/PPT/XLS、HTML、邮件、压缩包批量转换；旧 Office 文件可先另存为新格式。不能把第一版理解为任意文件格式的无损转换器。

PDF 图像区域与原生文字重叠时，会把已提取文字的位置遮白，减少重复识别。遮罩可能影响对应位置的图形细节；完整规则原生网格可直接重建 Markdown 表格；合并格、曲线、填充及复杂叠图表格不保证还原。若目标是复杂表格或完整图表关系，需要单独验证该类文件。

## 输出

- `markdown`：合并后的全文。
- 工具默认文本输出、`.md` 文件：内容与 `markdown` 相同。
- `documents`：`[{page_content, metadata, provider, vector, children}]`，采用官方 Dify Extractor 文档字段形状。这里是一份输入文件对应一个 Document，后续再分块；可将 `markdown` 接入通用文本分块节点，再写入知识库。
- `warnings`：版式、公式、模型识别等限制。
- `stats`：原生字符数、页数/逻辑页数、图片数、视觉调用数、输出长度和模型返回的 token 数。

## 资源和失败策略

每次处理一个文件，默认上限为 20 MiB、20 页/幻灯片、24 个待识别图像区域、20,000 个表格单元格和 500,000 个输出字符。DOCX 的页数指显式分页逻辑页，XLSX 不是打印分页。图片单张最大 2000 万像素，发送前最长边缩至 1800；所有规范化图片合计不得超过 32 MiB。Office 压缩包还有条目数、解压体积、压缩比及 XML 实体检查。

超过限制明确报错。图片请求顺序执行，插件最多同时处理 2 个请求。SDK 请求超时设为 600 秒，但 Dify API、网关或模型服务可能设置更短超时；大文件应先拆分。插件仍有 Python 运行时和图片解码内存开销；当前未测量线上 RSS，不能承诺具体内存用量。

模型只收到本次需要识别的图片和固定转换提示，不接收原生全文。图片内容会发送到所选模型的服务端并产生模型费用。插件不执行文档中包含的指令，不执行 Office 宏/公式，不主动访问文档里的链接。

## 本地开发与验证

在含有 `manifest.yaml` 与 `scripts/` 的仓库根目录执行：

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --only-binary=:all: -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
.venv/bin/python scripts/package.py --cli /path/to/dify-plugin-cli --output ./dist/vision_markdown-0.1.7.difypkg
```

将 `/path/to/dify-plugin-cli` 替换为官方 CLI 可执行文件路径。显式指定 `--output`，将发布产物保留在本仓库内。打包脚本使用白名单暂存源码，只将运行所需文件、图标与说明交给官方 CLI，不把 `.venv`、缓存、测试或本地凭据打入包。依赖由 Dify 安装时下载，包内不携带 Python 库。

测试采用合成文档和模型替身，覆盖原生/扫描/图文混排分流、顺序、去重策略、大小限制、SDK 消息与失败处理。单元测试验证程序行为；真实模型的中文 OCR、图表描述及生产平台表现需另做端到端测试。本次回归使用15份冻结的中英文合成文件，测试结果单独记录，不能推广为任意文档无损。

## 核对依据

- [Dify SDK 官方仓库](https://github.com/langgenius/dify-plugin-sdks)
- [Dify 1.17.1 模型选择器参数定义](https://github.com/langgenius/dify/blob/1.17.1/api/core/entities/parameter_entities.py)
- [官方 Extractor 输出文档结构](https://github.com/langgenius/dify-official-plugins/blob/main/tools/dify_extractor/tools/document.py)
- [PDFium Python API 与线程安全要求](https://pypdfium2.readthedocs.io/en/stable/python_api.html)
- [官方打包 CLI 0.6.10](https://github.com/langgenius/dify-plugin-daemon/releases/tag/0.6.10)

## 联系与反馈

请通过[仓库 Issues](https://github.com/1569197626/vision_markdown/issues)提交问题或可复现的错误报告。公开反馈中不要包含凭据或保密原文。


## 0.1.7 市场兼容版

SDK 升级为固定 0.9.0，增加源码仓库及维护邮箱元数据，运行时诊断英文化。转换算法、资源限制、输出字段、权限及换行规范不变。0.1.6 原始安装包在 GitHub Release 保留；0.1.7 未安装到原线上环境，尚未测试 Dify Cloud。


Maintainer contact: 1569197626@qq.com.
