"""文档解析基础设施：按扩展名白名单解析文本、HTML、PDF 和 Office 文档。"""

from __future__ import annotations

import asyncio
import importlib
import io
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from enterprise_rag.application.ports import ParsedUnit


class _TextExtractor(HTMLParser):
    """HTML 文本提取器，只保留标签中的可见文本片段。"""

    def __init__(self) -> None:
        """初始化 HTML 解析器和文本片段缓冲区。"""
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        """接收 HTML 文本节点，并忽略纯空白内容。"""
        if data.strip():
            self.parts.append(data.strip())


class AllowListParser:
    """按扩展名白名单把文档转换为带页码或章节元数据的文本单元。"""

    supported_extensions = frozenset(
        {".txt", ".md", ".html", ".pdf", ".docx", ".pptx", ".xlsx"}
    )

    async def parse(self, name: str, content: bytes) -> list[ParsedUnit]:
        """在线程池中运行可能阻塞的具体文档解析。"""
        return await asyncio.to_thread(self._parse_sync, name, content)

    def _parse_sync(self, name: str, content: bytes) -> list[ParsedUnit]:
        """按扩展名选择纯文本、HTML、PDF、Office 或表格解析器。"""
        extension = Path(name).suffix.lower()
        if extension not in self.supported_extensions:
            raise ValueError("unsupported document format")
        if extension in {".txt", ".md"}:
            return self._parse_text(content)
        if extension == ".html":
            parser = _TextExtractor()
            parser.feed(content.decode("utf-8", errors="replace"))
            return [ParsedUnit("\n".join(parser.parts), None, None, {"format": "html"})]
        if extension == ".pdf":
            module = importlib.import_module("fitz")
            document = module.open(stream=content, filetype="pdf")
            units: list[ParsedUnit] = []
            try:
                for index, page in enumerate(document):
                    text = page.get_text().strip()
                    metadata: dict[str, Any] = {
                        "format": "pdf",
                        "native_text_chars": len(text),
                        "ocr_candidate": len(text) < 32,
                    }
                    page_image = None
                    if len(text) < 32:
                        # 保留扫描页的可供 OCR 输入, 即使原生文本为空。
                        pixmap = page.get_pixmap(matrix=module.Matrix(1.5, 1.5), alpha=False)
                        page_image = pixmap.tobytes("png")
                    units.append(ParsedUnit(text, index + 1, None, metadata, page_image))
            finally:
                document.close()
            return units
        if extension == ".docx":
            module = importlib.import_module("docx")
            document = module.Document(io.BytesIO(content))
            return self._units_from_paragraphs(document.paragraphs, "docx")
        if extension == ".pptx":
            module = importlib.import_module("pptx")
            presentation = module.Presentation(io.BytesIO(content))
            slide_units: list[ParsedUnit] = []
            for slide_number, slide in enumerate(presentation.slides, 1):
                texts = [shape.text for shape in slide.shapes if hasattr(shape, "text")]
                text = "\n".join(item.strip() for item in texts if item.strip())
                if text:
                    slide_units.append(
                        ParsedUnit(text, slide_number, f"Slide {slide_number}", {"format": "pptx"})
                    )
            return slide_units
        module = importlib.import_module("openpyxl")
        workbook = module.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        spreadsheet_units: list[ParsedUnit] = []
        for worksheet in workbook.worksheets:
            rows = [
                "\t".join("" if value is None else str(value) for value in row)
                for row in worksheet.iter_rows(values_only=True)
            ]
            text = "\n".join(row for row in rows if row.strip())
            if text:
                spreadsheet_units.append(
                    ParsedUnit(text, None, worksheet.title, {"format": "xlsx"})
                )
        return spreadsheet_units

    @staticmethod
    def _parse_text(content: bytes) -> list[ParsedUnit]:
        """按 Markdown 标题切分文本，并保留当前章节名称。"""
        text = content.decode("utf-8", errors="replace").replace("\x00", "")
        heading: str | None = None
        units: list[ParsedUnit] = []
        buffer: list[str] = []
        for line in text.splitlines():
            if re.match(r"^#{1,6}\s+", line):
                if buffer:
                    units.append(ParsedUnit("\n".join(buffer), None, heading, {"format": "text"}))
                    buffer = []
                heading = line.lstrip("# ").strip()
            else:
                buffer.append(line)
        if buffer or not units:
            units.append(ParsedUnit("\n".join(buffer).strip(), None, heading, {"format": "text"}))
        return [unit for unit in units if unit.text]

    @staticmethod
    def _units_from_paragraphs(paragraphs: Any, document_format: str) -> list[ParsedUnit]:
        """按 Word 标题样式切分段落，生成带格式元数据的文本单元。"""
        units: list[ParsedUnit] = []
        section: str | None = None
        buffer: list[str] = []
        for paragraph in paragraphs:
            text = str(paragraph.text).strip()
            if not text:
                continue
            style_name = str(getattr(getattr(paragraph, "style", None), "name", ""))
            if style_name.lower().startswith("heading"):
                if buffer:
                    units.append(
                        ParsedUnit("\n".join(buffer), None, section, {"format": document_format})
                    )
                    buffer = []
                section = text
            else:
                buffer.append(text)
        if buffer:
            units.append(ParsedUnit("\n".join(buffer), None, section, {"format": document_format}))
        return units
