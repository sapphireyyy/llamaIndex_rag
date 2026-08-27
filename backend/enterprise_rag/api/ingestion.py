"""文档摄取接口：接收上传文件并触发异步索引任务。"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, File, Form, Header, Request, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_rag.api.auth import Identity
from enterprise_rag.api.errors import AppError
from enterprise_rag.application.ingestion import IngestionService
from enterprise_rag.application.ports import (
    EmbeddingAdapter,
    ObjectStore,
    OCRAdapter,
    ParserAdapter,
    SearchAdapter,
)
from enterprise_rag.application.reconciliation import IndexReconciliationService
from enterprise_rag.application.security import DatabaseAuthorizationService
from enterprise_rag.application.tenant_configuration import TenantConfigurationService
from enterprise_rag.application.tenant_limits import tenant_operation_lease
from enterprise_rag.domain.tenant_config import (
    TenantConfiguration,
    ingestion_processing_snapshot,
)
from enterprise_rag.domain.types import ErrorCategory, RequestIdentity, new_id, stable_hash
from enterprise_rag.infrastructure.orm import (
    DocumentRecord,
    DocumentVersionRecord,
    IdempotencyRecord,
    IndexGenerationRecord,
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


class RebuildRequest(BaseModel):
    """现有文档重建请求，重建只改变处理代次而不伪造源版本。"""

    reason: str = Field(default="rebuild", min_length=1, max_length=80)


class ReconcileRequest(BaseModel):
    """索引对账请求，可选指定代次并按普通重建语义修复。"""

    generation_id: str | None = None
    repair: bool = False


class GenerationRollbackRequest(BaseModel):
    """历史处理代次回切请求。"""

    reason: str = Field(default="generation-rollback", min_length=1, max_length=80)


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
        ocr_adapter=cast(OCRAdapter, container.ocr),
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
        "claimed_by": job.claimed_by,
        "lease_expires_at": job.lease_expires_at,
        "next_attempt_at": job.next_attempt_at,
        "processing_config_version_id": job.processing_config_version_id,
        "processing_config_hash": job.processing_snapshot.get("policy_hash"),
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def _generation_view(
    generation: IndexGenerationRecord, active_generation_id: str | None = None
) -> dict[str, object]:
    """返回不含正文的处理代次状态和配置摘要。"""
    return {
        "id": generation.id,
        "document_version_id": generation.document_version_id,
        "generation_number": generation.generation_number,
        "state": generation.state,
        "active": generation.id == active_generation_id,
        "dense_state": generation.dense_state,
        "lexical_state": generation.lexical_state,
        "chunk_count": generation.chunk_count,
        "reason": generation.reason,
        "processing_config_version_id": generation.processing_config_version_id,
        "processing_strategy_version": generation.processing_strategy_version,
        "processing_config_hash": generation.processing_config_hash,
        "component_versions": generation.component_versions,
        "last_error": generation.last_error,
        "published_at": generation.published_at,
        "superseded_at": generation.superseded_at,
        "created_at": generation.created_at,
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
    """限流读取上传文件，在一个事务中创建 queued 任务和 outbox 事件。"""
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
            service = _service(request, session, tenant_config)
            service.processing_snapshot = ingestion_processing_snapshot(
                tenant_config, identity.configuration_version_id
            )
            job = await service.submit_upload(
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
            if request.app.state.settings.ingestion_execution_mode == "inline":
                # 仅供显式 development/test 夹具使用, production 配置校验会拒绝它。
                job = await service.process_with_retries(job.id)
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
                    "active_generation_id": item.active_generation_id,
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


@router.post("/documents/{document_id}/rebuild", status_code=202)
async def rebuild_document(
    request: Request,
    document_id: str,
    payload: RebuildRequest,
    identity: Identity,
) -> dict[str, object]:
    """按当前租户处理配置排队重建，旧活动代次在发布前保持不变。"""
    container = request.app.state.container
    if container.coordination is None or container.ingestion_slots is None:
        raise AppError(ErrorCategory.DEPENDENCY_UNAVAILABLE, "Ingestion controls unavailable.", 503)
    async with tenant_operation_lease(
        _sessions(request), container.coordination, identity, "upload"
    ) as tenant_config, container.ingestion_slots, _sessions(request)() as session:
        service = _service(request, session, tenant_config)
        service.processing_snapshot = ingestion_processing_snapshot(
            tenant_config, identity.configuration_version_id
        )
        job = await service.submit_rebuild(
            identity,
            document_id,
            request.state.correlation_id,
            reason=payload.reason,
        )
        await session.commit()
        return _job_view(job)


@router.post("/data-sources/{source_id}/rebuild", status_code=202)
async def rebuild_data_source(
    request: Request,
    source_id: str,
    payload: RebuildRequest,
    identity: Identity,
) -> dict[str, object]:
    """为数据源下的全部文档排队重建任务。"""
    container = request.app.state.container
    if container.coordination is None or container.ingestion_slots is None:
        raise AppError(ErrorCategory.DEPENDENCY_UNAVAILABLE, "Ingestion controls unavailable.", 503)
    async with tenant_operation_lease(
        _sessions(request), container.coordination, identity, "upload"
    ) as tenant_config, container.ingestion_slots, _sessions(request)() as session:
        service = _service(request, session, tenant_config)
        service.processing_snapshot = ingestion_processing_snapshot(
            tenant_config, identity.configuration_version_id
        )
        jobs = await service.submit_rebuild_for_source(
            identity,
            source_id,
            request.state.correlation_id,
            reason=payload.reason,
        )
        await session.commit()
        return {"items": [_job_view(job) for job in jobs], "count": len(jobs)}


@router.post("/knowledge-spaces/{knowledge_space_id}/rebuild", status_code=202)
async def rebuild_knowledge_space(
    request: Request,
    knowledge_space_id: str,
    payload: RebuildRequest,
    identity: Identity,
) -> dict[str, object]:
    """为知识空间下的全部文档排队重建任务。"""
    container = request.app.state.container
    if container.coordination is None or container.ingestion_slots is None:
        raise AppError(ErrorCategory.DEPENDENCY_UNAVAILABLE, "Ingestion controls unavailable.", 503)
    async with tenant_operation_lease(
        _sessions(request), container.coordination, identity, "upload"
    ) as tenant_config, container.ingestion_slots, _sessions(request)() as session:
        service = _service(request, session, tenant_config)
        service.processing_snapshot = ingestion_processing_snapshot(
            tenant_config, identity.configuration_version_id
        )
        jobs = await service.submit_rebuild_for_space(
            identity,
            knowledge_space_id,
            request.state.correlation_id,
            reason=payload.reason,
        )
        await session.commit()
        return {"items": [_job_view(job) for job in jobs], "count": len(jobs)}


@router.post("/documents/{document_id}/reconcile")
async def reconcile_document(
    request: Request,
    document_id: str,
    payload: ReconcileRequest,
    identity: Identity,
) -> dict[str, object]:
    """比较 PostgreSQL 权威分块与双索引投影，并可排队修复。"""
    container = request.app.state.container
    if not all(
        (
            container.dense_index,
            container.lexical_index,
            container.sessions,
        )
    ):
        raise AppError(
            ErrorCategory.DEPENDENCY_UNAVAILABLE,
            "Index reconciliation unavailable.",
            503,
        )
    async with _sessions(request)() as session:
        reconciler = IndexReconciliationService(
            session,
            DatabaseAuthorizationService(_sessions(request)),
            cast(SearchAdapter, container.dense_index),
            cast(SearchAdapter, container.lexical_index),
        )
        report = await reconciler.inspect(identity, document_id, payload.generation_id)
        repair_job: IngestionJobRecord | None = None
        if payload.repair and report.stale:
            _tenant, config_version, tenant_config = await TenantConfigurationService(
                session
            )._load_active(identity.tenant_id)
            service = _service(request, session, tenant_config)
            service.processing_snapshot = ingestion_processing_snapshot(
                tenant_config, config_version.id
            )
            repair_job = await service.submit_rebuild(
                identity,
                document_id,
                request.state.correlation_id,
                reason="index-reconciliation",
            )
            await session.commit()
        return {
            "report": report.as_dict(),
            "repair_job": _job_view(repair_job) if repair_job is not None else None,
        }


@router.post("/documents/{document_id}/generations/{generation_id}/rollback")
async def rollback_generation(
    request: Request,
    document_id: str,
    generation_id: str,
    payload: GenerationRollbackRequest,
    identity: Identity,
) -> dict[str, object]:
    """在双索引发布成功后回切历史处理代次。"""
    container = request.app.state.container
    if not all((container.dense_index, container.lexical_index, container.sessions)):
        raise AppError(
            ErrorCategory.DEPENDENCY_UNAVAILABLE,
            "Index rollback unavailable.",
            503,
        )
    async with _sessions(request)() as session:
        generation = await _service(request, session).rollback_generation(
            identity,
            document_id,
            generation_id,
            request.state.correlation_id,
            reason=payload.reason,
        )
        await session.commit()
        return {"generation": _generation_view(generation, generation.id), "reason": payload.reason}


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
        generations = list(
            (
                await session.scalars(
                    select(IndexGenerationRecord)
                    .where(
                        IndexGenerationRecord.document_id == document.id,
                        IndexGenerationRecord.tenant_id == identity.tenant_id,
                    )
                    .order_by(IndexGenerationRecord.generation_number.desc())
                )
            ).all()
        )
        generations_by_version: dict[str, list[dict[str, object]]] = {}
        for generation in generations:
            generations_by_version.setdefault(generation.document_version_id, []).append(
                _generation_view(generation, document.active_generation_id)
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
                    "active_generation_id": document.active_generation_id,
                    "generations": generations_by_version.get(item.id, []),
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
