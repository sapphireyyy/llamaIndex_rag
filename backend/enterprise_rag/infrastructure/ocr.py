"""Optional OCR adapters with deterministic test and no-op implementations."""

from __future__ import annotations

import asyncio
import importlib
import io
import time
from collections.abc import Mapping

from enterprise_rag.application.ports import OCRPageResult, OCRRegion, ParsedUnit


class NoopOCRAdapter:
    """不执行识别的安全默认实现，用于 disabled 或未配置 OCR 的环境。"""

    capabilities = frozenset({"ocr", "local", "noop"})
    provider = "none"
    version = "none-v1"

    async def recognize(self, page: ParsedUnit) -> OCRPageResult:
        """返回可诊断的空结果，不读取或记录页面正文。"""
        return OCRPageResult(
            page=page.page or 0,
            text="",
            regions=(),
            provider=self.provider,
            version=self.version,
            error="ocr adapter is not configured",
        )


class DeterministicOCRAdapter:
    """由测试显式提供页面结果的 OCR fake，不依赖外部服务或凭证。"""

    capabilities = frozenset({"ocr", "local", "deterministic", "test"})
    provider = "fake-ocr"
    version = "fake-ocr-v1"

    def __init__(self, results: Mapping[int, OCRPageResult] | None = None) -> None:
        """保存按页返回的固定结果。"""
        self.results = dict(results or {})

    async def recognize(self, page: ParsedUnit) -> OCRPageResult:
        """按页返回固定结果，缺省时返回空且可诊断结果。"""
        page_number = page.page or 0
        result = self.results.get(page_number)
        if result is not None:
            return result
        return OCRPageResult(
            page=page_number,
            text="",
            regions=(),
            provider=self.provider,
            version=self.version,
            error="fake OCR result is not configured",
        )


class TesseractOCRAdapter:
    """可选本地 Tesseract 适配器，依赖在首次调用时惰性加载。"""

    capabilities = frozenset({"ocr", "local", "regions", "confidence"})
    provider = "tesseract"
    version = "tesseract-v1"

    def __init__(self, language: str = "chi_sim+eng") -> None:
        """设置 Tesseract 语言组合；实际二进制由运行环境管理。"""
        self.language = language

    async def recognize(self, page: ParsedUnit) -> OCRPageResult:
        """在线程池中执行本地 OCR，避免阻塞 API 或 worker 事件循环。"""
        return await asyncio.to_thread(self._recognize_sync, page)

    def _recognize_sync(self, page: ParsedUnit) -> OCRPageResult:
        """解析页面图片并返回区域、置信度和耗时。"""
        started = time.perf_counter()
        if not page.page_image:
            return OCRPageResult(
                page=page.page or 0,
                text="",
                regions=(),
                provider=self.provider,
                version=self.version,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                error="page rendering input is missing",
            )
        try:
            image_module = importlib.import_module("PIL.Image")
            pytesseract = importlib.import_module("pytesseract")
            output = importlib.import_module("pytesseract").Output
            image = image_module.open(io.BytesIO(page.page_image))
            data = pytesseract.image_to_data(
                image, lang=self.language, output_type=output.DICT
            )
        except Exception as exc:
            return OCRPageResult(
                page=page.page or 0,
                text="",
                regions=(),
                provider=self.provider,
                version=self.version,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                error=str(exc)[:256],
            )
        regions: list[OCRRegion] = []
        for index, raw_text in enumerate(data.get("text", [])):
            text = str(raw_text).strip()
            if not text:
                continue
            raw_confidence = data.get("conf", ["-1"])[index]
            try:
                confidence = max(0.0, min(float(raw_confidence) / 100.0, 1.0))
            except (TypeError, ValueError):
                confidence = 0.0
            left = float(data.get("left", [0])[index])
            top = float(data.get("top", [0])[index])
            width = float(data.get("width", [0])[index])
            height = float(data.get("height", [0])[index])
            regions.append(
                OCRRegion(text, confidence, left, top, left + width, top + height)
            )
        text = " ".join(region.text for region in regions)
        average_confidence: float | None = (
            sum(region.confidence for region in regions) / len(regions) if regions else None
        )
        return OCRPageResult(
            page=page.page or 0,
            text=text,
            regions=tuple(regions),
            provider=self.provider,
            version=self.version,
            confidence=average_confidence,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )
