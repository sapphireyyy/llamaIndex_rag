"""问答接口：创建流式查询、取消查询并返回引用来源。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import cast

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_rag.api.auth import Identity
from enterprise_rag.api.errors import AppError
from enterprise_rag.application.guardrails import BuiltinGuardrails
from enterprise_rag.application.ingestion import IngestionService
from enterprise_rag.application.ports import (
    EmbeddingAdapter,
    ObjectStore,
    ParserAdapter,
    RerankerAdapter,
    SearchAdapter,
)
from enterprise_rag.application.providers import AdapterRegistry, ModelGateway
from enterprise_rag.application.query import QueryService
from enterprise_rag.application.retrieval import RetrievalService
from enterprise_rag.application.security import DatabaseAuthorizationService
from enterprise_rag.application.tenant_limits import tenant_operation_lease
from enterprise_rag.domain.types import ErrorCategory
from enterprise_rag.infrastructure.orm import (
    AnswerRecord,
    ChunkRecord,
    CitationRecord,
    DocumentRecord,
    QueryExecutionRecord,
)
from enterprise_rag.infrastructure.telemetry import TelemetryInput, TelemetryService

router = APIRouter(prefix="/api/v1", tags=["chat"])


class QueryInput(BaseModel):
    """问答请求参数：指定助手、问题以及可选的历史会话。"""

    assistant_id: str = Field(min_length=1)
    question: str = Field(min_length=1, max_length=12_000)
    conversation_id: str | None = None


def _sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    """从应用容器取得数据库会话工厂；未就绪时返回 503。"""
    sessions = request.app.state.container.sessions
    if sessions is None:
        raise AppError(ErrorCategory.DEPENDENCY_UNAVAILABLE, "Query service unavailable.", 503)
    return cast(async_sessionmaker[AsyncSession], sessions)


def _query_service(request: Request, session: AsyncSession) -> QueryService:
    """使用当前请求的基础设施组装完整问答服务及其检索依赖。"""
    container = request.app.state.container
    values = (
        container.registry,
        container.dense_index,
        container.lexical_index,
        container.reranker,
        container.model_gateway,
        container.guardrails,
    )
    if not all(values) or container.sessions is None:
        raise AppError(ErrorCategory.DEPENDENCY_UNAVAILABLE, "Query service unavailable.", 503)
    retrieval = RetrievalService(
        session,
        DatabaseAuthorizationService(container.sessions),
        cast(SearchAdapter, container.dense_index),
        cast(SearchAdapter, container.lexical_index),
        cast(RerankerAdapter, container.reranker),
    )
    return QueryService(
        session,
        cast(AdapterRegistry, container.registry),
        retrieval,
        cast(ModelGateway, container.model_gateway),
        cast(BuiltinGuardrails, container.guardrails),
    )


def _sse(event: str, payload: Mapping[str, object]) -> str:
    """把事件名和负载编码成浏览器可消费的 Server-Sent Events 格式。"""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


@router.post("/chat/query")
async def query(request: Request, payload: QueryInput, identity: Identity) -> StreamingResponse:
    """执行一次受限流保护的问答，并把答案和引用按 SSE 分块返回。"""
    container = request.app.state.container
    if container.coordination is None or container.query_slots is None:
        raise AppError(ErrorCategory.DEPENDENCY_UNAVAILABLE, "Query controls unavailable.", 503)
    async with (
        tenant_operation_lease(
            _sessions(request), container.coordination, identity, "query"
        ),
        container.query_slots,
        _sessions(request)() as session,
    ):
        conversation, execution, answer, citations = await _query_service(
            request, session
        ).execute(
            identity,
            payload.assistant_id,
            payload.question,
            request.state.correlation_id,
            payload.conversation_id,
        )
        await TelemetryService(
            session,
            request.app.state.settings.content_capture_enabled,
            request.app.state.settings.telemetry_sample_rate,
        ).record(
            TelemetryInput(
                identity.tenant_id,
                request.state.correlation_id,
                "query",
                "terminal",
                answer.status,
                attributes={
                    "query_id": execution.id,
                    "assistant_version_id": execution.assistant_version_id,
                    "component_versions": execution.component_versions,
                    "usage": execution.usage,
                },
                content={"question": payload.question, "answer": answer.text},
            )
        )
        await session.commit()
        query_id = execution.id
        answer_id = answer.id
        conversation_id = conversation.id
        answer_text = answer.text
        terminal_status = answer.status
        execution.status = "streaming"
        await session.commit()
        citation_payloads = [
            {
                "id": item.id,
                "label": item.label,
                "page": item.page,
                "section": item.section,
                "source_url": f"/api/v1/citations/{item.id}/source",
            }
            for item in citations
        ]

    async def events() -> AsyncIterator[str]:
        """逐步发送查询、答案、引用和终止状态，同时响应取消或断开。"""
        async def current_status() -> str | None:
            """读取数据库中的查询状态，以便流式输出感知取消请求。"""
            async with _sessions(request)() as status_session:
                status = await status_session.scalar(
                    select(QueryExecutionRecord.status).where(
                        QueryExecutionRecord.id == query_id,
                        QueryExecutionRecord.tenant_id == identity.tenant_id,
                    )
                )
                return status

        async def finish(status: str) -> None:
            """把流式查询的最终状态持久化到查询执行记录。"""
            async with _sessions(request)() as status_session:
                execution_record = await status_session.scalar(
                    select(QueryExecutionRecord).where(
                        QueryExecutionRecord.id == query_id,
                        QueryExecutionRecord.tenant_id == identity.tenant_id,
                    )
                )
                if execution_record is not None:
                    execution_record.status = status
                    await status_session.commit()

        yield _sse(
            "query",
            {
                "query_id": query_id,
                "answer_id": answer_id,
                "conversation_id": conversation_id,
            },
        )
        for offset in range(0, len(answer_text), 120):
            if await request.is_disconnected():
                await finish("cancelled")
                return
            if await current_status() == "cancelled":
                yield _sse("terminal", {"status": "cancelled"})
                return
            yield _sse("answer", {"delta": answer_text[offset : offset + 120]})
        for citation in citation_payloads:
            yield _sse("citation", citation)
        await finish(terminal_status)
        yield _sse("terminal", {"status": terminal_status})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/queries/{query_id}/cancel")
async def cancel_query(request: Request, query_id: str, identity: Identity) -> dict[str, object]:
    """校验查询属于当前租户，并将运行中查询标记为已取消。"""
    async with _sessions(request)() as session:
        execution = await session.scalar(
            select(QueryExecutionRecord).where(
                QueryExecutionRecord.id == query_id,
                QueryExecutionRecord.tenant_id == identity.tenant_id,
            )
        )
        if execution is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Query not found.", 404)
        if execution.status in {"running", "streaming"}:
            execution.status = "cancelled"
            await session.commit()
        return {"id": execution.id, "status": execution.status}


@router.get("/citations/{citation_id}/source")
async def citation_source(
    request: Request, citation_id: str, identity: Identity
) -> dict[str, object]:
    """校验引用仍指向当前租户的活动文档，并返回预览与下载入口。"""
    async with _sessions(request)() as session:
        row = await session.execute(
            select(CitationRecord, ChunkRecord, DocumentRecord)
            .join(AnswerRecord, AnswerRecord.id == CitationRecord.answer_id)
            .join(QueryExecutionRecord, QueryExecutionRecord.id == AnswerRecord.query_id)
            .join(ChunkRecord, ChunkRecord.id == CitationRecord.chunk_id)
            .join(DocumentRecord, DocumentRecord.id == ChunkRecord.document_id)
            .where(
                CitationRecord.id == citation_id,
                QueryExecutionRecord.tenant_id == identity.tenant_id,
            )
        )
        result = row.one_or_none()
        if result is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Citation not found.", 404)
        citation, chunk, document = result
        container = request.app.state.container
        if not all(
            (
                container.object_store,
                container.parser,
                container.embedding,
                container.dense_index,
                container.lexical_index,
            )
        ):
            raise AppError(ErrorCategory.DEPENDENCY_UNAVAILABLE, "Source unavailable.", 503)
        ingestion = IngestionService(
            session,
            cast(ObjectStore, container.object_store),
            cast(ParserAdapter, container.parser),
            cast(EmbeddingAdapter, container.embedding),
            cast(SearchAdapter, container.dense_index),
            cast(SearchAdapter, container.lexical_index),
            request.app.state.settings.allowed_extensions,
            request.app.state.settings.max_upload_bytes,
        )
        await ingestion.require_document(identity, document.id)
        if document.active_version_id != citation.document_version_id or not chunk.active:
            raise AppError(ErrorCategory.NOT_FOUND, "Citation source is no longer active.", 404)
        return {
            "citation_id": citation.id,
            "document_id": document.id,
            "title": document.title,
            "page": citation.page,
            "section": citation.section,
            "preview_url": f"/api/v1/documents/{document.id}/preview",
            "download_url": f"/api/v1/documents/{document.id}/download",
        }
