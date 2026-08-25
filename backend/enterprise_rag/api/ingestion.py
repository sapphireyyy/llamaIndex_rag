"""文档摄取接口：接收上传文件并触发异步索引任务。"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, File, Form, Header, Request, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_rag.api.auth import Identity
from enterprise_rag.api.errors import AppError
from enterprise_rag.application.ingestion import IngestionService
from enterprise_rag.application.ports import (
    EmbeddingAdapter,
    ObjectStore,
    ParserAdapter,
    SearchAdapter,
)
from enterprise_rag.application.tenant_limits import tenant_operation_lease
from enterprise_rag.domain.tenant_config import TenantConfiguration
from enterprise_rag.domain.types import ErrorCategory, RequestIdentity, new_id, stable_hash
from enterprise_rag.infrastructure.orm import (
    DocumentRecord,
    DocumentVersionRecord,
    IdempotencyRecord,
    IngestionJobRecord,
)
from enterprise_rag.infrastructure.telemetry import TelemetryInput, TelemetryService

router = APIRouter(prefix="/api/v1", tags=["ingestion"])


class UploadResponse(BaseModel):
    """上传接口返回的任务、文档和摄取阶段摘要。"""

    job_id: str
    document_id: str | None
    document_version_id: str | None
    status: str
    stage: str


def _service(
    request: Request,
    session: AsyncSession,
    tenant_config: TenantConfiguration | None = None,
) -> IngestionService:
    """从容器组装当前请求使用的文档摄取服务。"""
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
        raise AppError(ErrorCategory.DEPENDENCY_UNAVAILABLE, "Ingestion unavailable.", 503)
    return IngestionService(
        session,
        cast(ObjectStore, container.object_store),
        cast(ParserAdapter, container.parser),
        cast(EmbeddingAdapter, container.embedding),
        cast(SearchAdapter, container.dense_index),
        cast(SearchAdapter, container.lexical_index),
        set(tenant_config.uploads.allowed_extensions)
        if tenant_config is not None
        else request.app.state.settings.allowed_extensions,
        tenant_config.uploads.max_file_bytes
        if tenant_config is not None
        else request.app.state.settings.max_upload_bytes,
    )


def _sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    """取得文档摄取接口所需的异步数据库会话工厂。"""
    sessions = request.app.state.container.sessions
    if sessions is None:
        raise AppError(ErrorCategory.DEPENDENCY_UNAVAILABLE, "Ingestion unavailable.", 503)
    return cast(async_sessionmaker[AsyncSession], sessions)


def _job_view(job: IngestionJobRecord) -> dict[str, object]:
    """将摄取任务 ORM 对象转换为不包含内部对象的 API 摘要。"""
    return {
        "id": job.id,
        "document_id": job.document_id,
        "document_version_id": job.document_version_id,
        "status": job.status,
        "stage": job.stage,
        "attempt_count": job.attempt_count,
        "max_attempts": job.max_attempts,
        "error_category": job.error_category,
        "error_message": job.error_message,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


async def _idempotent_job(
    session: AsyncSession,
    identity: RequestIdentity,
    key: str | None,
    request_hash: str,
) -> IngestionJobRecord | None:
    """根据幂等键复用已提交任务，并拒绝请求体哈希不一致的复用。"""
    if not key:
        return None
    record = await session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.tenant_id == identity.tenant_id,
            IdempotencyRecord.operation == "document.upload",
            IdempotencyRecord.key == key,
        )
    )
    if record is None:
        return None
    if record.request_hash != request_hash:
        raise AppError(ErrorCategory.CONFLICT, "Idempotency key was reused.", 409)
    if not record.response_body:
        raise AppError(ErrorCategory.CONFLICT, "Idempotent request is still processing.", 409)
    job = await session.scalar(
        select(IngestionJobRecord).where(
            IngestionJobRecord.id == record.response_body.decode("utf-8"),
            IngestionJobRecord.tenant_id == identity.tenant_id,
        )
    )
    if job is None:
        raise AppError(ErrorCategory.CONFLICT, "Idempotent response is unavailable.", 409)
    return job


@router.post("/documents/upload", status_code=202)
async def upload_document(
    request: Request,
    identity: Identity,
    source_id: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    content_sha256: Annotated[str | None, Header(alias="X-Content-SHA256")] = None,
) -> dict[str, object]:
    """限流读取上传文件，创建幂等摄取任务并执行可重试处理。"""
    container = request.app.state.container
    if container.coordination is None or container.ingestion_slots is None:
        raise AppError(ErrorCategory.DEPENDENCY_UNAVAILABLE, "Ingestion controls unavailable.", 503)
    async with tenant_operation_lease(
        _sessions(request), container.coordination, identity, "upload"
    ) as tenant_config:
        content = await file.read(tenant_config.uploads.max_file_bytes + 1)
        request_hash = stable_hash(f"{source_id}:{file.filename}:{stable_hash(content)}")
        async with container.ingestion_slots, _sessions(request)() as session:
            existing = await _idempotent_job(session, identity, idempotency_key, request_hash)
            if existing:
                return _job_view(existing)
            job = await _service(request, session, tenant_config).submit_upload(
                identity,
                source_id,
                file.filename or "upload.bin",
                content,
                file.content_type or "application/octet-stream",
                request.state.correlation_id,
                content_sha256,
            )
            if idempotency_key:
                session.add(
                    IdempotencyRecord(
                        id=new_id("idempotency"),
                        tenant_id=identity.tenant_id,
                        operation="document.upload",
                        key=idempotency_key,
                        request_hash=request_hash,
                        response_status=202,
                        response_body=job.id.encode("utf-8"),
                    )
                )
            job = await _service(request, session, tenant_config).process_with_retries(job.id)
            await TelemetryService(
                session,
                request.app.state.settings.content_capture_enabled,
                request.app.state.settings.telemetry_sample_rate,
            ).record(
                TelemetryInput(
                    identity.tenant_id,
                    request.state.correlation_id,
                    "ingestion",
                    job.stage,
                    job.status,
                    attributes={
                        "job_id": job.id,
                        "document_version_id": job.document_version_id,
                        "attempt_count": job.attempt_count,
                    },
                    content={"filename": file.filename},
                )
            )
            await session.commit()
            return _job_view(job)


@router.get("/ingestion-jobs")
async def list_jobs(request: Request, identity: Identity) -> dict[str, object]:
    """列出当前租户的文档摄取任务。"""
    async with _sessions(request)() as session:
        records = list(
            (
                await session.scalars(
                    select(IngestionJobRecord)
                    .where(IngestionJobRecord.tenant_id == identity.tenant_id)
                    .order_by(IngestionJobRecord.created_at.desc())
                )
            ).all()
        )
        return {"items": [_job_view(item) for item in records]}


@router.get("/ingestion-jobs/{job_id}")
async def get_job(request: Request, job_id: str, identity: Identity) -> dict[str, object]:
    """读取当前租户指定摄取任务的状态。"""
    async with _sessions(request)() as session:
        job = await session.scalar(
            select(IngestionJobRecord).where(
                IngestionJobRecord.id == job_id,
                IngestionJobRecord.tenant_id == identity.tenant_id,
            )
        )
        if job is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Ingestion job not found.", 404)
        return _job_view(job)


@router.post("/ingestion-jobs/{job_id}/retry")
async def retry_job(request: Request, job_id: str, identity: Identity) -> dict[str, object]:
    """重新排队失败任务，并在允许范围内执行重试。"""
    async with _sessions(request)() as session:
        job = await _service(request, session).retry(identity, job_id)
        await session.commit()
        return _job_view(job)


@router.post("/ingestion-jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: str, identity: Identity) -> dict[str, object]:
    """把尚未结束的摄取任务标记为取消。"""
    async with _sessions(request)() as session:
        job = await _service(request, session).cancel(identity, job_id)
        await session.commit()
        return _job_view(job)


@router.get("/documents")
async def list_documents(request: Request, identity: Identity) -> dict[str, object]:
    """列出当前租户未删除的文档和活动版本。"""
    async with _sessions(request)() as session:
        records = list(
            (
                await session.scalars(
                    select(DocumentRecord)
                    .where(
                        DocumentRecord.tenant_id == identity.tenant_id,
                        DocumentRecord.state != "deleted",
                    )
                    .order_by(DocumentRecord.updated_at.desc())
                )
            ).all()
        )
        return {
            "items": [
                {
                    "id": item.id,
                    "title": item.title,
                    "state": item.state,
                    "knowledge_space_id": item.knowledge_space_id,
                    "active_version_id": item.active_version_id,
                    "updated_at": item.updated_at,
                }
                for item in records
            ]
        }


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(
    request: Request, document_id: str, identity: Identity
) -> Response:
    """软删除文档、停用分块并发布索引清理任务。"""
    async with _sessions(request)() as session:
        await _service(request, session).delete_document(
            identity, document_id, request.state.correlation_id
        )
        await session.commit()
    return Response(status_code=204)


@router.get("/documents/{document_id}/versions")
async def document_versions(
    request: Request, document_id: str, identity: Identity
) -> dict[str, object]:
    """返回文档的版本状态、哈希和来源元数据。"""
    async with _sessions(request)() as session:
        document = await _service(request, session).require_document(identity, document_id)
        records = list(
            (
                await session.scalars(
                    select(DocumentVersionRecord)
                    .where(DocumentVersionRecord.document_id == document.id)
                    .order_by(DocumentVersionRecord.created_at.desc())
                )
            ).all()
        )
        return {
            "items": [
                {
                    "id": item.id,
                    "content_hash": item.content_hash,
                    "mime_type": item.mime_type,
                    "size_bytes": item.size_bytes,
                    "state": item.state,
                    "active": item.id == document.active_version_id,
                    "created_at": item.created_at,
                }
                for item in records
            ]
        }


@router.get("/documents/{document_id}/preview")
async def preview_document(
    request: Request, document_id: str, identity: Identity
) -> dict[str, object]:
    """返回活动文档分块的文本预览。"""
    async with _sessions(request)() as session:
        chunks = await _service(request, session).preview(identity, document_id)
        return {
            "items": [
                {
                    "id": item.id,
                    "text": item.text,
                    "page": item.page,
                    "section": item.section,
                    "ordinal": item.ordinal,
                }
                for item in chunks
            ]
        }


@router.get("/documents/{document_id}/download")
async def download_document(
    request: Request, document_id: str, identity: Identity
) -> Response:
    """读取活动原文件并以下载响应返回。"""
    async with _sessions(request)() as session:
        name, media_type, content = await _service(request, session).download(
            identity, document_id
        )
        return Response(
            content,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )
