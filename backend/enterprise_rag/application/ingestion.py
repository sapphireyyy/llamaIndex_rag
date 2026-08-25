"""摄取应用服务：解析文档、切分内容、写入版本并驱动索引发布。"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

from llama_index.core.node_parser import SentenceSplitter
from opentelemetry import trace
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.api.errors import AppError
from enterprise_rag.application.audit import AuditEventInput, AuditService
from enterprise_rag.application.ports import (
    EmbeddingAdapter,
    IndexDocument,
    ObjectStore,
    ParserAdapter,
    SearchAdapter,
)
from enterprise_rag.application.security import bump_acl_epoch
from enterprise_rag.application.tenant_configuration import TenantQuotaService
from enterprise_rag.domain.types import (
    ErrorCategory,
    QuotaResource,
    RequestIdentity,
    Role,
    new_id,
    stable_hash,
    utc_now,
)
from enterprise_rag.infrastructure.database import require_session_tenant
from enterprise_rag.infrastructure.object_store import tenant_object_key
from enterprise_rag.infrastructure.orm import (
    ChunkRecord,
    DataSourceRecord,
    DocumentRecord,
    DocumentVersionRecord,
    IngestionJobRecord,
    JobAttemptRecord,
    SpaceMembershipRecord,
    TenantRecord,
)
from enterprise_rag.infrastructure.queue import OutboxService
from enterprise_rag.infrastructure.telemetry import INGESTION_OUTCOMES


class IngestionService:
    """负责从上传原文件到活动索引发布的完整摄取生命周期。"""

    def __init__(
        self,
        session: AsyncSession,
        object_store: ObjectStore,
        parser: ParserAdapter,
        embedding: EmbeddingAdapter,
        dense_index: SearchAdapter,
        lexical_index: SearchAdapter,
        allowed_extensions: set[str],
        max_upload_bytes: int,
        chunk_size: int = 512,
        chunk_overlap: int = 64,
    ) -> None:
        """初始化文档摄取所需的解析、嵌入与双索引依赖。"""
        self.session = session
        self.object_store = object_store
        self.parser = parser
        self.embedding = embedding
        self.dense_index = dense_index
        self.lexical_index = lexical_index
        self.allowed_extensions = allowed_extensions
        self.max_upload_bytes = max_upload_bytes
        self.splitter = SentenceSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)

    @staticmethod
    def _require_editor(identity: RequestIdentity) -> None:
        """确认当前身份具备编辑知识内容的权限。"""
        if not identity.roles.intersection({Role.ADMINISTRATOR, Role.EDITOR}):
            raise AppError(ErrorCategory.FORBIDDEN, "Operation is not permitted.", 403)

    async def _source(
        self, identity: RequestIdentity, source_id: str
    ) -> DataSourceRecord:
        """读取当前租户中已启用的数据源。"""
        source = await self.session.scalar(
            select(DataSourceRecord).where(
                DataSourceRecord.id == source_id,
                DataSourceRecord.tenant_id == identity.tenant_id,
                DataSourceRecord.enabled.is_(True),
            )
        )
        if source is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Resource not found.", 404)
        return source

    def _validate_content(
        self, name: str, content: bytes, expected_hash: str | None
    ) -> str:
        """校验上传文件的格式、大小、完整性和基础恶意样本特征。"""
        extension = Path(name).suffix.lower()
        if (
            extension not in self.allowed_extensions
            or extension not in self.parser.supported_extensions
        ):
            raise AppError(ErrorCategory.INVALID_REQUEST, "Unsupported document format.", 415)
        if not content or len(content) > self.max_upload_bytes:
            raise AppError(ErrorCategory.INVALID_REQUEST, "Document size is not permitted.", 413)
        content_hash = stable_hash(content)
        if expected_hash and expected_hash.casefold() != content_hash:
            raise AppError(ErrorCategory.INVALID_REQUEST, "Document integrity check failed.", 422)
        if b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE" in content.upper():
            raise AppError(ErrorCategory.INVALID_REQUEST, "Document failed malware screening.", 422)
        return content_hash

    async def submit_upload(
        self,
        identity: RequestIdentity,
        source_id: str,
        name: str,
        content: bytes,
        content_type: str,
        correlation_id: str,
        expected_hash: str | None = None,
        source_identity: str | None = None,
        acl_tokens: Sequence[str] | None = None,
    ) -> IngestionJobRecord:
        """创建文档版本、保存原文件并投递摄取任务。"""
        self._require_editor(identity)
        self.session.info["tenant_id"] = identity.tenant_id
        source = await self._source(identity, source_id)
        content_hash = self._validate_content(name, content, expected_hash)
        stable_source_identity = source_identity or name
        document = await self.session.scalar(
            select(DocumentRecord).where(
                DocumentRecord.data_source_id == source.id,
                DocumentRecord.source_identity == stable_source_identity,
            )
        )
        if document is None:
            document_reservation = (
                await TenantQuotaService(self.session).reserve(
                    identity, QuotaResource.DOCUMENTS, 1
                )
                if identity.configuration_version_id
                else None
            )
            memberships = list(
                (
                    await self.session.scalars(
                        select(SpaceMembershipRecord.principal_token).where(
                            SpaceMembershipRecord.knowledge_space_id == source.knowledge_space_id
                        )
                    )
                ).all()
            )
            tokens = sorted(set(acl_tokens or memberships or identity.principal_tokens))
            document = DocumentRecord(
                id=new_id("document"),
                tenant_id=identity.tenant_id,
                knowledge_space_id=source.knowledge_space_id,
                data_source_id=source.id,
                source_identity=stable_source_identity,
                title=Path(name).name,
                acl_tokens=tokens,
            )
            self.session.add(document)
            if document_reservation is not None:
                await TenantQuotaService(self.session).release(
                    document_reservation, consume=True
                )
            await self.session.flush()
        active_version = (
            await self.session.scalar(
                select(DocumentVersionRecord).where(
                    DocumentVersionRecord.id == document.active_version_id,
                    DocumentVersionRecord.tenant_id == identity.tenant_id,
                )
            )
            if document.active_version_id
            else None
        )
        if active_version is not None and active_version.content_hash == content_hash:
            job = IngestionJobRecord(
                id=new_id("job"),
                tenant_id=identity.tenant_id,
                document_id=document.id,
                document_version_id=active_version.id,
                kind="upload",
                status="succeeded",
                stage="unchanged",
                correlation_id=correlation_id,
                payload={
                    "unchanged": True,
                    "name": Path(name).name,
                    "tenant_context_version": 1
                    if identity.authorization_epoch and identity.configuration_version_id
                    else 0,
                    "authorization_epoch": identity.authorization_epoch,
                    "configuration_version_id": identity.configuration_version_id,
                    "resource_version_id": active_version.id,
                    "policy_version_ids": [],
                },
            )
            self.session.add(job)
            await self.session.flush()
            return job
        version_id = new_id("document_version")
        storage_reservation = (
            await TenantQuotaService(self.session).reserve(
                identity, QuotaResource.STORED_BYTES, len(content)
            )
            if identity.configuration_version_id
            else None
        )
        source_key = tenant_object_key(
            identity.tenant_id, "documents", version_id, Path(name).name
        )
        await self.object_store.put(source_key, content, content_type)
        if storage_reservation is not None:
            await TenantQuotaService(self.session).release(
                storage_reservation, consume=True
            )
        version = DocumentVersionRecord(
            id=version_id,
            tenant_id=identity.tenant_id,
            document_id=document.id,
            content_hash=content_hash,
            source_key=source_key,
            mime_type=content_type,
            size_bytes=len(content),
            source_metadata={"name": Path(name).name, "source_identity": stable_source_identity},
        )
        job = IngestionJobRecord(
            id=new_id("job"),
            tenant_id=identity.tenant_id,
            document_id=document.id,
            document_version_id=version.id,
            kind="upload",
            status="queued",
            stage="stored",
            correlation_id=correlation_id,
            payload={
                "name": Path(name).name,
                "tenant_context_version": 1
                if identity.authorization_epoch and identity.configuration_version_id
                else 0,
                "authorization_epoch": identity.authorization_epoch,
                "configuration_version_id": identity.configuration_version_id,
                "resource_version_id": version.id,
                "policy_version_ids": [],
            },
        )
        self.session.add_all([version, job])
        await self.session.flush()
        INGESTION_OUTCOMES.labels(status=job.status).inc()
        return job

    async def process(self, job_id: str) -> IngestionJobRecord:
        """处理指定摄取任务，并记录任务尝试结果。"""
        tracer = trace.get_tracer("enterprise_rag.ingestion")
        with tracer.start_as_current_span("ingestion.attempt") as span:
            span.set_attribute("ingestion.job_id", job_id)
            job = await self._process(job_id, tracer)
            span.set_attribute("ingestion.status", job.status)
            span.set_attribute("ingestion.attempt_count", job.attempt_count)
            return job

    async def _process(
        self, job_id: str, tracer: trace.Tracer
    ) -> IngestionJobRecord:
        """执行解析、分块、嵌入、索引发布及失败回滚流程。"""
        tenant_context = require_session_tenant(self.session)
        job = await self.session.scalar(
            select(IngestionJobRecord)
            .where(
                IngestionJobRecord.id == job_id,
                IngestionJobRecord.tenant_id == tenant_context,
            )
            .with_for_update()
        )
        if job is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Ingestion job not found.", 404)
        tenant = await self.session.get(TenantRecord, job.tenant_id)
        if tenant is None:
            job.status = "failed"
            job.stage = "tenant_context_quarantined"
            job.error_category = "missing_tenant_context"
            job.error_message = "Tenant context is unavailable."
            return job
        if tenant.state != "active":
            job.status = "cancelled"
            job.stage = "tenant_lifecycle_rejected"
            job.error_category = "tenant_not_active"
            job.error_message = "Tenant is not active."
            return job
        if job.payload.get("tenant_context_version") == 1:
            stale_epoch = job.payload.get("authorization_epoch") != tenant.acl_epoch
            stale_config = (
                job.payload.get("configuration_version_id")
                != tenant.active_config_version_id
            )
            if stale_epoch or stale_config:
                job.status = "failed"
                job.stage = "tenant_context_stale"
                job.error_category = "stale_tenant_context"
                job.error_message = "Tenant authorization or configuration changed."
                return job
        if job.status == "succeeded" or job.stage == "unchanged":
            return job
        if job.cancelled:
            job.status = "cancelled"
            return job
        job.attempt_count += 1
        job.status = "running"
        job.stage = "parsing"
        attempt = JobAttemptRecord(
            id=new_id("attempt"),
            job_id=job.id,
            attempt=job.attempt_count,
            status="running",
        )
        self.session.add(attempt)
        await self.session.flush()
        publication_started = False
        rollback_document: DocumentRecord | None = None
        rollback_version_id: str | None = None
        try:
            if not job.document_version_id or not job.document_id:
                raise RuntimeError("job does not reference a document version")
            version = await self.session.scalar(
                select(DocumentVersionRecord).where(
                    DocumentVersionRecord.id == job.document_version_id,
                    DocumentVersionRecord.tenant_id == job.tenant_id,
                )
            )
            document = await self.session.scalar(
                select(DocumentRecord).where(
                    DocumentRecord.id == job.document_id,
                    DocumentRecord.tenant_id == job.tenant_id,
                )
            )
            tenant = await self.session.get(TenantRecord, job.tenant_id)
            if version is None or document is None or tenant is None:
                raise RuntimeError("ingestion resources are unavailable")
            rollback_document = document
            rollback_version_id = document.active_version_id
            with tracer.start_as_current_span("ingestion.parse"):
                source = await self.object_store.get(version.source_key)
                units = await self.parser.parse(str(job.payload["name"]), source)
            job.stage = "chunking"
            chunk_inputs: list[tuple[str, int | None, str | None, dict[str, object]]] = []
            for unit in units:
                normalized = "\n".join(line.rstrip() for line in unit.text.splitlines()).strip()
                for chunk_text in self.splitter.split_text(normalized):
                    if chunk_text.strip():
                        chunk_inputs.append(
                            (chunk_text.strip(), unit.page, unit.section, unit.metadata)
                        )
            if not chunk_inputs:
                raise ValueError("document contained no indexable text")
            job.stage = "embedding"
            with tracer.start_as_current_span("ingestion.embed"):
                embeddings = await self.embedding.embed([item[0] for item in chunk_inputs])
            index_documents: list[IndexDocument] = []
            chunks: list[ChunkRecord] = []
            for ordinal, (item, vector) in enumerate(zip(chunk_inputs, embeddings, strict=True)):
                text, page, section, metadata = item
                chunk_id = f"chunk_{stable_hash(f'{version.id}:{ordinal}:{text}')[:32]}"
                provenance: dict[str, object] = {
                    "tenant_id": document.tenant_id,
                    "knowledge_space_id": document.knowledge_space_id,
                    "document_id": document.id,
                    "document_version_id": version.id,
                    "title": document.title,
                    "page": page,
                    "section": section,
                    "acl_tokens": document.acl_tokens,
                    "acl_epoch": document.acl_epoch,
                    "source_authority": version.source_metadata.get("authority", "internal"),
                    **metadata,
                }
                chunks.append(
                    ChunkRecord(
                        id=chunk_id,
                        tenant_id=document.tenant_id,
                        knowledge_space_id=document.knowledge_space_id,
                        document_id=document.id,
                        document_version_id=version.id,
                        ordinal=ordinal,
                        text=text,
                        page=page,
                        section=section,
                        metadata_json=provenance,
                        acl_tokens=document.acl_tokens,
                        acl_epoch=document.acl_epoch,
                    )
                )
                index_documents.append(IndexDocument(chunk_id, text, vector, provenance))
            job.stage = "staging_indexes"
            with tracer.start_as_current_span("ingestion.stage_indexes"):
                await self.dense_index.stage(index_documents)
                await self.lexical_index.stage(index_documents)
            self.session.add_all(chunks)
            await self.session.flush()
            job.stage = "verifying"
            if len(index_documents) != len(chunks):
                raise RuntimeError("staging index verification failed")
            job.stage = "publishing"
            publication_started = True
            with tracer.start_as_current_span("ingestion.publish_indexes"):
                await self.dense_index.publish(version.id)
                await self.lexical_index.publish(version.id)
            old_version_id = document.active_version_id
            if old_version_id:
                old_version = await self.session.scalar(
                    select(DocumentVersionRecord).where(
                        DocumentVersionRecord.id == old_version_id,
                        DocumentVersionRecord.tenant_id == job.tenant_id,
                    )
                )
                if old_version:
                    old_version.state = "superseded"
                await self.session.execute(
                    update(ChunkRecord)
                    .where(ChunkRecord.document_version_id == old_version_id)
                    .values(active=False)
                )
            version.state = "active"
            document.active_version_id = version.id
            document.state = "active"
            await self.session.execute(
                update(ChunkRecord)
                .where(ChunkRecord.document_version_id == version.id)
                .values(active=True)
            )
            for index in (self.dense_index, self.lexical_index):
                setter = getattr(index, "set_acl_epoch", None)
                if setter:
                    setter(document.tenant_id, tenant.acl_epoch)
            job.status = "succeeded"
            job.stage = "published"
            attempt.status = "succeeded"
            attempt.finished_at = utc_now()
        except Exception as exc:
            if publication_started and rollback_document is not None:
                for index in (self.dense_index, self.lexical_index):
                    with suppress(Exception):
                        if rollback_version_id:
                            await index.publish(rollback_version_id)
                        else:
                            await index.delete_document(rollback_document.id)
            job.status = "failed"
            job.stage = "failed"
            job.error_category = (
                "transient" if isinstance(exc, (OSError, TimeoutError)) else "content"
            )
            job.error_message = str(exc)[:512]
            attempt.status = "failed"
            attempt.error_category = job.error_category
            attempt.error_message = job.error_message
            attempt.finished_at = utc_now()
        await self.session.flush()
        return job

    async def retry(self, identity: RequestIdentity, job_id: str) -> IngestionJobRecord:
        """在重试额度内重新执行失败的摄取任务。"""
        self._require_editor(identity)
        job = await self.session.scalar(
            select(IngestionJobRecord).where(
                IngestionJobRecord.id == job_id,
                IngestionJobRecord.tenant_id == identity.tenant_id,
            )
        )
        if job is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Ingestion job not found.", 404)
        if job.attempt_count >= job.max_attempts:
            raise AppError(ErrorCategory.CONFLICT, "Retry limit reached.", 409)
        job.status = "queued"
        job.cancelled = False
        job.error_category = None
        job.error_message = None
        await self.session.flush()
        return await self.process(job.id)

    async def process_with_retries(self, job_id: str) -> IngestionJobRecord:
        """自动重试可恢复的临时摄取失败。"""
        job = await self.process(job_id)
        while (
            job.status == "failed"
            and job.error_category == "transient"
            and job.attempt_count < job.max_attempts
        ):
            job.status = "queued"
            job = await self.process(job.id)
        return job

    async def cancel(self, identity: RequestIdentity, job_id: str) -> IngestionJobRecord:
        """取消尚未完成的摄取任务。"""
        self._require_editor(identity)
        job = await self.session.scalar(
            select(IngestionJobRecord).where(
                IngestionJobRecord.id == job_id,
                IngestionJobRecord.tenant_id == identity.tenant_id,
            )
        )
        if job is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Ingestion job not found.", 404)
        if job.status in {"succeeded", "cancelled"}:
            raise AppError(ErrorCategory.CONFLICT, "Job cannot be cancelled.", 409)
        job.cancelled = True
        job.status = "cancelled"
        await self.session.flush()
        return job

    async def require_document(
        self, identity: RequestIdentity, document_id: str
    ) -> DocumentRecord:
        """读取文档并验证当前身份对其所属知识空间的访问权。"""
        document = await self.session.scalar(
            select(DocumentRecord).where(
                DocumentRecord.id == document_id,
                DocumentRecord.tenant_id == identity.tenant_id,
                DocumentRecord.state != "deleted",
            )
        )
        if document is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Resource not found.", 404)
        if Role.ADMINISTRATOR not in identity.roles:
            membership = await self.session.scalar(
                select(SpaceMembershipRecord.id).where(
                    SpaceMembershipRecord.knowledge_space_id == document.knowledge_space_id,
                    SpaceMembershipRecord.principal_token.in_(identity.principal_tokens),
                )
            )
            if membership is None:
                raise AppError(ErrorCategory.NOT_FOUND, "Resource not found.", 404)
        return document

    async def delete_document(
        self, identity: RequestIdentity, document_id: str, correlation_id: str
    ) -> None:
        """删除文档的活动版本、索引内容，并记录清理事件。"""
        self._require_editor(identity)
        document = await self.require_document(identity, document_id)
        document.state = "deleted"
        active_version = (
            await self.session.scalar(
                select(DocumentVersionRecord).where(
                    DocumentVersionRecord.id == document.active_version_id,
                    DocumentVersionRecord.tenant_id == identity.tenant_id,
                )
            )
            if document.active_version_id
            else None
        )
        if active_version:
            active_version.state = "tombstoned"
        await self.session.execute(
            update(ChunkRecord).where(ChunkRecord.document_id == document.id).values(active=False)
        )
        await self.dense_index.delete_document(document.id)
        await self.lexical_index.delete_document(document.id)
        await bump_acl_epoch(self.session, identity.tenant_id)
        await OutboxService(self.session).add(
            identity.tenant_id,
            "document.cleanup",
            correlation_id,
            {
                "document_id": document.id,
                "authorization_epoch": identity.authorization_epoch,
                "configuration_version_id": identity.configuration_version_id,
                "resource_version_id": document.active_version_id,
                "policy_version_ids": [],
            },
            priority=100,
        )
        await AuditService(self.session).record(
            identity,
            AuditEventInput(
                "document.delete",
                "document",
                document.id,
                "allowed",
                "success",
                correlation_id,
                {},
            ),
        )

    async def preview(
        self, identity: RequestIdentity, document_id: str
    ) -> list[ChunkRecord]:
        """返回当前身份可访问文档的活动分块预览。"""
        document = await self.require_document(identity, document_id)
        if not document.active_version_id:
            return []
        return list(
            (
                await self.session.scalars(
                    select(ChunkRecord)
                    .where(
                        ChunkRecord.document_version_id == document.active_version_id,
                        ChunkRecord.active.is_(True),
                    )
                    .order_by(ChunkRecord.ordinal)
                )
            ).all()
        )

    async def download(
        self, identity: RequestIdentity, document_id: str
    ) -> tuple[str, str, bytes]:
        """返回当前身份可下载的原始文档内容。"""
        document = await self.require_document(identity, document_id)
        if not document.active_version_id:
            raise AppError(ErrorCategory.NOT_FOUND, "Resource not found.", 404)
        version = await self.session.scalar(
            select(DocumentVersionRecord).where(
                DocumentVersionRecord.id == document.active_version_id,
                DocumentVersionRecord.tenant_id == identity.tenant_id,
            )
        )
        if version is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Resource not found.", 404)
        return document.title, version.mime_type, await self.object_store.get(version.source_key)
