from __future__ import annotations

from typing import Any, cast

import fitz
import pytest
from enterprise_rag.application.ingestion import IngestionService
from enterprise_rag.application.ports import OCRPageResult, OCRRegion, ParsedUnit
from enterprise_rag.infrastructure.ocr import DeterministicOCRAdapter
from enterprise_rag.infrastructure.parsers import AllowListParser


class FixtureOCRAdapter:
    """覆盖原生、空白、低置信度和识别失败页面的确定性 OCR 夹具。"""

    provider = "fixture-ocr"

    async def recognize(self, page: ParsedUnit) -> OCRPageResult:
        if page.page == 2:
            return OCRPageResult(
                page=2,
                text="空白扫描页",
                regions=(OCRRegion("空白扫描页", 0.87, 4, 5, 40, 50, "zh"),),
                provider=self.provider,
                version="fixture-ocr-v1",
                confidence=0.87,
                elapsed_ms=3.0,
            )
        if page.page == 3:
            return OCRPageResult(
                page=3,
                text="低置信度文字",
                regions=(OCRRegion("低置信度文字", 0.21, 4, 5, 40, 50, "zh"),),
                provider=self.provider,
                version="fixture-ocr-v1",
                confidence=0.21,
                elapsed_ms=4.0,
            )
        raise RuntimeError("fixture OCR page failure")


@pytest.mark.asyncio
async def test_pdf_parser_keeps_blank_page_as_ocr_candidate() -> None:
    document = fitz.open()
    native_page = document.new_page()
    native_page.insert_text((72, 72), "Native page text")
    document.new_page()
    pdf_bytes = document.tobytes()
    document.close()

    units = await AllowListParser().parse("scan.pdf", pdf_bytes)

    assert len(units) == 2
    assert units[0].text == "Native page text"
    assert units[1].page == 2
    assert units[1].metadata["ocr_candidate"] is True
    assert units[1].page_image


@pytest.mark.asyncio
async def test_ocr_result_is_attached_to_parsed_unit_with_provenance() -> None:
    result = OCRPageResult(
        page=2,
        text="扫描页内容",
        regions=(OCRRegion("扫描页内容", 0.91, 1, 2, 30, 40, "zh"),),
        provider="fake-ocr",
        version="fake-ocr-v1",
        confidence=0.91,
        elapsed_ms=4.0,
    )
    service = IngestionService(
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        {".pdf"},
        1_000,
        ocr_adapter=DeterministicOCRAdapter({2: result}),
    )
    unit = ParsedUnit("", 2, None, {"format": "pdf"}, b"png")

    enriched = await service._apply_ocr(
        [unit],
        {
            "ocr_mode": "required",
            "ocr_min_text_chars": 32,
            "ocr_provider_id": "deterministic",
        },
    )

    assert enriched[0].text == "扫描页内容"
    assert enriched[0].metadata["ocr"]["provider"] == "fake-ocr"
    assert enriched[0].metadata["ocr"]["regions"][0]["confidence"] == 0.91


@pytest.mark.asyncio
async def test_ocr_fixture_preserves_native_low_confidence_and_failed_pages() -> None:
    service = IngestionService(
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        {".pdf"},
        1_000,
        ocr_adapter=FixtureOCRAdapter(),
    )
    units = [
        ParsedUnit("原生文本页", 1, None, {"format": "pdf"}, None),
        ParsedUnit("", 2, None, {"format": "pdf", "ocr_candidate": True}, b"blank"),
        ParsedUnit("短文本", 3, None, {"format": "pdf", "ocr_candidate": True}, b"low"),
        ParsedUnit("", 4, None, {"format": "pdf", "ocr_candidate": True}, b"failed"),
    ]

    enriched = await service._apply_ocr(
        units,
        {
            "ocr_mode": "best_effort",
            "ocr_min_text_chars": 32,
            "ocr_provider_id": "fixture",
        },
    )

    assert enriched[0].text == "原生文本页"
    assert enriched[1].text == "空白扫描页"
    assert enriched[1].metadata["ocr"]["confidence"] == 0.87
    assert enriched[2].text == "短文本\n低置信度文字"
    assert enriched[2].metadata["ocr"]["confidence"] == 0.21
    assert enriched[3].text == ""
    assert "ocr" not in enriched[3].metadata


@pytest.mark.asyncio
async def test_required_ocr_fails_closed_when_provider_is_unavailable() -> None:
    service = IngestionService(
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        {".pdf"},
        1_000,
    )

    with pytest.raises(RuntimeError, match="required OCR provider"):
        await service._apply_ocr(
            [ParsedUnit("", 1, None, {"format": "pdf"}, b"png")],
            {"ocr_mode": "required", "ocr_min_text_chars": 32},
        )
