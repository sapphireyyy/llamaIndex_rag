"""摄取应用服务：解析文档、切分内容、写入版本并驱动索引发布。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any

from llama_index.core.node_parser import SentenceSplitter
from opentelemetry import trace
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.api.errors import AppError
from enterprise_rag.application.audit import AuditEventInput, AuditService
from enterprise_rag.application.ports import (
    EmbeddingAdapter,
    IndexDocument,
    ObjectStore,
    OCRAdapter,
    ParsedUnit,
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
    IndexGenerationRecord,
    IngestionJobRecord,
    JobAttemptRecord,
    KnowledgeSpaceRecord,
    SpaceMembershipRecord,
    TenantRecord,
)
from enterprise_rag.infrastructure.queue import OutboxService
from enterprise_rag.infrastructure.telemetry import (
    INGESTION_OUTCOMES,
    record_index_generation,
    record_ingestion_stage,
    record_ocr_page,
)


class IngestionCancelled(RuntimeError):
    """任务在下一处安全边界被取消，不应发布新的活动代次。"""


class IngestionLeaseLost(RuntimeError):
    """任务租约已丢失，当前 worker 不得继续提交处理结果。"""


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
        processing_snapshot: dict[str, Any] | None = None,
        ocr_adapter: OCRAdapter | None = None,
        lease_check: Callable[[], Awaitable[bool]] | None = None,
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
        self.ocr_adapter = ocr_adapter
        self.lease_check = lease_check
        self.processing_snapshot = dict(processing_snapshot or {})
        policy = self.processing_snapshot.get("policy", {})
        configured_chunk_size = int(policy.get("chunk_size", chunk_size))
        configured_chunk_overlap = int(policy.get("chunk_overlap", chunk_overlap))
        self.splitter = SentenceSplitter(
            chunk_size=configured_chunk_size,
            chunk_overlap=configured_chunk_overlap,
        )

    def _snapshot_for_job(self, job: IngestionJobRecord) -> dict[str, Any]:
        """读取任务提交时冻结的处理配置，并兼容旧任务的 512/64 默认值。"""
        snapshot = dict(job.processing_snapshot or job.payload.get("processing_snapshot", {}))
        if not snapshot:
            snapshot = {
                "schema_version": 1,
                "configuration_version_id": job.payload.get("configuration_version_id"),
                "strategy_version": "ingestion-v1",
                "policy": {
                    "strategy_version": "ingestion-v1",
                    "chunk_size": 512,
                    "chunk_overlap": 64,
                    "ocr_mode": "disabled",
                },
                "policy_hash": stable_hash("ingestion-v1:512:64:disabled"),
            }
        return snapshot

    @staticmethod
    def _check_cancelled(job: IngestionJobRecord) -> None:
        """在不可逆外部副作用前阻止已取消任务继续推进。"""
        if job.cancelled or job.status == "cancelled":
            raise IngestionCancelled

    @staticmethod
    def _release_lease(job: IngestionJobRecord) -> None:
        """清理终态任务的 worker 租约字段。"""
        job.claimed_by = None
        job.lease_expires_at = None
        job.heartbeat_at = None
        job.next_attempt_at = None

    async def _check_runtime(self, job: IngestionJobRecord) -> None:
        """刷新取消状态并确认 worker 仍持有任务租约。"""
        await self.session.refresh(
            job, ["cancelled", "status", "claimed_by", "lease_expires_at"]
        )
        self._check_cancelled(job)
        if self.lease_check is not None and not await self.lease_check():
            raise IngestionLeaseLost

    @staticmethod
    async def _publish_index(
        index: SearchAdapter,
        document_version_id: str,
        index_generation_id: str | None,
    ) -> None:
        """优先发布具体代次，并兼容尚未升级的外部索引适配器。"""
        publisher = getattr(index, "publish_generation", None)
        if publisher is not None and index_generation_id is not None:
            await publisher(document_version_id, index_generation_id)
            return
        await index.publish(document_version_id)

    async def _apply_ocr(
        self,
        units: list[ParsedUnit],
        policy: dict[str, Any],
    ) -> list[ParsedUnit]:
        """按任务快照执行 disabled、best_effort 或 required 页面 OCR。"""
        mode = str(policy.get("ocr_mode", "disabled"))
        if mode == "disabled":
            return units
        threshold = int(policy.get("ocr_min_text_chars", 32))
        candidates = [
            (index, unit)
            for index, unit in enumerate(units)
            if unit.page_image is not None
            and len(str(unit.text).strip()) < threshold
        ]
        max_pages = int(policy.get("ocr_max_pages", 100))
        if len(candidates) > max_pages and mode == "required":
            raise ValueError("document exceeds the configured OCR page limit")
        if not candidates or self.ocr_adapter is None:
            if mode == "required" and candidates:
                raise RuntimeError("required OCR provider is unavailable")
            if self.ocr_adapter is None:
                for _index, _unit in candidates:
                    record_ocr_page(mode, "unavailable", "skipped")
            return units
        timeout = float(policy.get("ocr_timeout_seconds", 30.0))
        enriched = list(units)
        for unit_index, unit in candidates[:max_pages]:
            try:
                result = await asyncio.wait_for(
                    self.ocr_adapter.recognize(unit), timeout=timeout
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                record_ocr_page(mode, self.ocr_adapter.provider, "failed")
                if mode == "required":
                    raise RuntimeError("required OCR failed") from exc
                continue
            if not result.text.strip():
                record_ocr_page(
                    mode,
                    result.provider,
                    "empty",
                    result.elapsed_ms,
                )
                if mode == "required":
                    raise RuntimeError(result.error or "required OCR returned no text")
                continue
            record_ocr_page(mode, result.provider, "succeeded", result.elapsed_ms)
            ocr_metadata = {
                "provider": result.provider,
                "version": result.version,
                "confidence": result.confidence,
                "elapsed_ms": result.elapsed_ms,
                "regions": [
                    {
                        "text": region.text,
                        "confidence": region.confidence,
                        "left": region.left,
                        "top": region.top,
                        "right": region.right,
                        "bottom": region.bottom,
                        "language": region.language,
                    }
                    for region in result.regions
                ],
            }
            replacement = replace(
                unit,
                text=(f"{unit.text}\n{result.text}" if unit.text.strip() else result.text).strip(),
                metadata={**unit.metadata, "ocr": ocr_metadata},
            )
            enriched[unit_index] = replacement
        return enriched

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
        processing_snapshot = dict(self.processing_snapshot)
        if not processing_snapshot:
            processing_snapshot = {
                "schema_version": 1,
                "configuration_version_id": identity.configuration_version_id,
                "strategy_version": "ingestion-v1",
                "policy": {
                    "strategy_version": "ingestion-v1",
                    "chunk_size": 512,
                    "chunk_overlap": 64,
                    "ocr_mode": "disabled",
                },
                "policy_hash": stable_hash("ingestion-v1:512:64:disabled"),
            }
        processing_config_version_id = processing_snapshot.get(
            "configuration_version_id", identity.configuration_version_id
        )
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
            active_generation = (
                await self.session.scalar(
                    select(IndexGenerationRecord).where(
                        IndexGenerationRecord.id == document.active_generation_id,
                        IndexGenerationRecord.tenant_id == identity.tenant_id,
                        IndexGenerationRecord.document_id == document.id,
                        IndexGenerationRecord.state == "active",
                    )
                )
                if document.active_generation_id
                else None
            )
            if not (
                active_generation is not None
                and active_generation.processing_config_version_id
                == processing_config_version_id
                and active_generation.processing_config_hash
                == str(processing_snapshot.get("policy_hash", ""))
            ):
                # The content is unchanged, but the immutable processing policy changed.
                # Reuse the authoritative version and create a new generation instead of
                # incorrectly treating the upload as a no-op.
                return await self.submit_rebuild(
                    identity,
                    document.id,
                    correlation_id,
                    reason="processing-config-change",
                    processing_snapshot=processing_snapshot,
                )
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
                    "processing_snapshot": processing_snapshot,
                },
                processing_config_version_id=processing_config_version_id,
                processing_snapshot=processing_snapshot,
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
                "processing_snapshot": processing_snapshot,
            },
            processing_config_version_id=processing_config_version_id,
            processing_snapshot=processing_snapshot,
        )
        self.session.add_all([version, job])
        await self.session.flush()
        await OutboxService(self.session).add(
            identity.tenant_id,
            "ingestion.process",
            correlation_id,
            {
                "job_id": job.id,
                "document_id": document.id,
                "document_version_id": version.id,
                "processing_config_version_id": processing_config_version_id,
                "processing_snapshot": processing_snapshot,
                "authorization_epoch": identity.authorization_epoch,
                "resource_version_id": version.id,
                "policy_version_ids": [],
            },
            priority=job.priority,
            max_attempts=job.max_attempts,
        )
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
            record_ingestion_stage(job.stage, job.status)
            INGESTION_OUTCOMES.labels(status=job.status).inc()
            if job.stage == "published":
                record_index_generation("published")
            elif job.status == "failed":
                record_index_generation("failed")
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
        )
        if job is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Ingestion job not found.", 404)
        tenant = await self.session.get(TenantRecord, job.tenant_id)
        if tenant is None:
            job.status = "failed"
            job.stage = "tenant_context_quarantined"
            job.error_category = "missing_tenant_context"
            job.error_message = "Tenant context is unavailable."
            self._release_lease(job)
            return job
        if tenant.state != "active":
            job.status = "cancelled"
            job.stage = "tenant_lifecycle_rejected"
            job.error_category = "tenant_not_active"
            job.error_message = "Tenant is not active."
            self._release_lease(job)
            return job
        if job.payload.get("tenant_context_version") == 1:
            stale_epoch = job.payload.get("authorization_epoch") != tenant.acl_epoch
            if stale_epoch:
                job.status = "failed"
                job.stage = "tenant_context_stale"
                job.error_category = "stale_tenant_context"
                job.error_message = "Tenant authorization changed."
                self._release_lease(job)
                return job
        if job.status == "succeeded" or job.stage == "unchanged":
            self._release_lease(job)
            return job
        if job.cancelled:
            job.status = "cancelled"
            self._release_lease(job)
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
        generation: IndexGenerationRecord | None = None
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
            await self._check_runtime(job)
            snapshot = self._snapshot_for_job(job)
            policy = dict(snapshot.get("policy", {}))
            configured_chunk_size = int(policy.get("chunk_size", 512))
            configured_chunk_overlap = int(policy.get("chunk_overlap", 64))
            if configured_chunk_overlap >= configured_chunk_size:
                raise ValueError("invalid persisted chunking policy")
            splitter = SentenceSplitter(
                chunk_size=configured_chunk_size,
                chunk_overlap=configured_chunk_overlap,
            )
            max_generation = await self.session.scalar(
                select(func.max(IndexGenerationRecord.generation_number)).where(
                    IndexGenerationRecord.document_id == document.id,
                    IndexGenerationRecord.tenant_id == document.tenant_id,
                )
            )
            generation_number = int(max_generation or 0) + 1
            generation = IndexGenerationRecord(
                id=new_id("index_generation"),
                tenant_id=document.tenant_id,
                document_id=document.id,
                document_version_id=version.id,
                generation_number=generation_number,
                processing_config_version_id=job.processing_config_version_id
                or snapshot.get("configuration_version_id"),
                processing_strategy_version=str(snapshot.get("strategy_version", "ingestion-v1")),
                processing_config_hash=str(snapshot.get("policy_hash", ""))[:64],
                component_versions={
                    "parser": type(self.parser).__name__,
                    "embedding": type(self.embedding).__name__,
                    "dense_index": type(self.dense_index).__name__,
                    "lexical_index": type(self.lexical_index).__name__,
                },
                reason=str(job.payload.get("reason", "upload")),
                state="staging",
            )
            self.session.add(generation)
            await self.session.flush()
            await self._check_runtime(job)
            with tracer.start_as_current_span("ingestion.parse"):
                source = await self.object_store.get(version.source_key)
                units = await self.parser.parse(str(job.payload["name"]), source)
            units = await self._apply_ocr(units, policy)
            job.stage = "chunking"
            chunk_inputs: list[tuple[str, int | None, str | None, dict[str, object]]] = []
            for unit in units:
                normalized = "\n".join(line.rstrip() for line in unit.text.splitlines()).strip()
                for chunk_text in splitter.split_text(normalized):
                    if chunk_text.strip():
                        chunk_inputs.append(
                            (chunk_text.strip(), unit.page, unit.section, unit.metadata)
                        )
            if not chunk_inputs:
                raise ValueError("document contained no indexable text")
            if len(chunk_inputs) > int(policy.get("max_chunks", 100_000)):
                raise ValueError("document exceeds the configured chunk limit")
            await self._check_runtime(job)
            job.stage = "embedding"
            with tracer.start_as_current_span("ingestion.embed"):
                embeddings = await self.embedding.embed([item[0] for item in chunk_inputs])
            index_documents: list[IndexDocument] = []
            chunks: list[ChunkRecord] = []
            for ordinal, (item, vector) in enumerate(zip(chunk_inputs, embeddings, strict=True)):
                text, page, section, metadata = item
                chunk_key = f"{version.id}:{generation.id}:{ordinal}:{text}"
                chunk_id = f"chunk_{stable_hash(chunk_key)[:32]}"
                provenance: dict[str, object] = {
                    "tenant_id": document.tenant_id,
                    "knowledge_space_id": document.knowledge_space_id,
                    "document_id": document.id,
                    "document_version_id": version.id,
                    "index_generation_id": generation.id,
                    "generation_number": generation.generation_number,
                    "processing_config_version_id": generation.processing_config_version_id,
                    "processing_config_hash": generation.processing_config_hash,
                    "processing_strategy_version": generation.processing_strategy_version,
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
                        index_generation_id=generation.id,
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
            await self._check_runtime(job)
            with tracer.start_as_current_span("ingestion.stage_indexes"):
                await self.dense_index.stage(index_documents)
                generation.dense_state = "staged"
                await self.lexical_index.stage(index_documents)
                generation.lexical_state = "staged"
            self.session.add_all(chunks)
            await self.session.flush()
            job.stage = "verifying"
            if len(index_documents) != len(chunks):
                raise RuntimeError("staging index verification failed")
            job.stage = "publishing"
            await self._check_runtime(job)
            publication_started = True
            with tracer.start_as_current_span("ingestion.publish_indexes"):
                await self._publish_index(self.dense_index, version.id, generation.id)
                generation.dense_state = "published"
                await self._publish_index(self.lexical_index, version.id, generation.id)
                generation.lexical_state = "published"
            await self._check_runtime(job)
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
            await self.session.execute(
                update(ChunkRecord)
                .where(
                    ChunkRecord.document_version_id == version.id,
                    ChunkRecord.index_generation_id != generation.id,
                )
                .values(active=False)
            )
            version.state = "active"
            document.active_version_id = version.id
            old_generation_id = document.active_generation_id
            document.active_generation_id = generation.id
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
            generation.state = "active"
            generation.chunk_count = len(chunks)
            generation.published_at = utc_now()
            if old_generation_id and old_generation_id != generation.id:
                await self.session.execute(
                    update(IndexGenerationRecord)
                    .where(
                        IndexGenerationRecord.id == old_generation_id,
                        IndexGenerationRecord.tenant_id == document.tenant_id,
                    )
                    .values(state="superseded", superseded_at=utc_now())
                )
            job.status = "succeeded"
            job.stage = "published"
            attempt.status = "succeeded"
            attempt.finished_at = utc_now()
        except IngestionLeaseLost:
            await self.session.rollback()
            raise
        except Exception as exc:
            if generation is not None:
                generation.state = "failed"
                generation.last_error = str(exc)[:512]
            if publication_started and rollback_document is not None:
                for index in (self.dense_index, self.lexical_index):
                    with suppress(Exception):
                        if rollback_version_id:
                            await self._publish_index(
                                index,
                                rollback_version_id,
                                rollback_document.active_generation_id,
                            )
                        else:
                            await index.delete_document(rollback_document.id)
            if isinstance(exc, IngestionCancelled):
                job.status = "cancelled"
                job.stage = "cancelled"
                job.error_category = "cancelled"
                job.error_message = "Job was cancelled."
                attempt.status = "cancelled"
            else:
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
        self._release_lease(job)
        await self.session.flush()
        return job

    async def submit_rebuild(
        self,
        identity: RequestIdentity,
        document_id: str,
        correlation_id: str,
        *,
        reason: str = "rebuild",
        processing_snapshot: dict[str, Any] | None = None,
    ) -> IngestionJobRecord:
        """为现有活动内容版本创建新的可回滚处理代次任务。"""
        self._require_editor(identity)
        self.session.info["tenant_id"] = identity.tenant_id
        document = await self.require_document(identity, document_id)
        if not document.active_version_id:
            raise AppError(ErrorCategory.CONFLICT, "Document has no active version.", 409)
        version = await self.session.scalar(
            select(DocumentVersionRecord).where(
                DocumentVersionRecord.id == document.active_version_id,
                DocumentVersionRecord.tenant_id == identity.tenant_id,
                DocumentVersionRecord.state == "active",
            )
        )
        if version is None:
            raise AppError(ErrorCategory.CONFLICT, "Document version is unavailable.", 409)
        snapshot = dict(processing_snapshot or self.processing_snapshot)
        if not snapshot:
            snapshot = self._snapshot_for_job(
                IngestionJobRecord(
                    id="rebuild-default",
                    tenant_id=identity.tenant_id,
                    correlation_id=correlation_id,
                    payload={},
                )
            )
        job = IngestionJobRecord(
            id=new_id("job"),
            tenant_id=identity.tenant_id,
            document_id=document.id,
            document_version_id=version.id,
            kind="rebuild",
            status="queued",
            stage="queued",
            correlation_id=correlation_id,
            payload={
                "name": str(version.source_metadata.get("name", document.title)),
                "reason": reason,
                "authorization_epoch": identity.authorization_epoch,
                "configuration_version_id": identity.configuration_version_id,
                "resource_version_id": version.id,
                "policy_version_ids": [],
                "processing_snapshot": snapshot,
            },
            processing_config_version_id=snapshot.get(
                "configuration_version_id", identity.configuration_version_id
            ),
            processing_snapshot=snapshot,
        )
        self.session.add(job)
        await self.session.flush()
        await OutboxService(self.session).add(
            identity.tenant_id,
            "ingestion.process",
            correlation_id,
            {
                "job_id": job.id,
                "document_id": document.id,
                "document_version_id": version.id,
                "processing_config_version_id": job.processing_config_version_id,
                "processing_snapshot": snapshot,
                "authorization_epoch": identity.authorization_epoch,
                "resource_version_id": version.id,
                "policy_version_ids": [],
            },
            priority=job.priority,
            max_attempts=job.max_attempts,
        )
        await self.session.flush()
        await AuditService(self.session).record(
            identity,
            AuditEventInput(
                "document.rebuild.submit",
                "document",
                document.id,
                "allowed",
                "queued",
                correlation_id,
                {
                    "document_version_id": version.id,
                    "processing_config_version_id": job.processing_config_version_id,
                    "reason": reason,
                },
            ),
        )
        return job

    async def submit_rebuild_for_source(
        self,
        identity: RequestIdentity,
        source_id: str,
        correlation_id: str,
        *,
        reason: str = "source-rebuild",
        processing_snapshot: dict[str, Any] | None = None,
    ) -> list[IngestionJobRecord]:
        """为一个租户数据源下的所有文档排队重建任务。"""
        self._require_editor(identity)
        source = await self.session.scalar(
            select(DataSourceRecord).where(
                DataSourceRecord.id == source_id,
                DataSourceRecord.tenant_id == identity.tenant_id,
            )
        )
        if source is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Data source not found.", 404)
        document_ids = list(
            (
                await self.session.scalars(
                    select(DocumentRecord.id)
                    .where(
                        DocumentRecord.tenant_id == identity.tenant_id,
                        DocumentRecord.data_source_id == source.id,
                        DocumentRecord.state != "deleted",
                    )
                    .order_by(DocumentRecord.id)
                )
            ).all()
        )
        return [
            await self.submit_rebuild(
                identity,
                document_id,
                correlation_id,
                reason=reason,
                processing_snapshot=processing_snapshot,
            )
            for document_id in document_ids
        ]

    async def submit_rebuild_for_space(
        self,
        identity: RequestIdentity,
        knowledge_space_id: str,
        correlation_id: str,
        *,
        reason: str = "space-rebuild",
        processing_snapshot: dict[str, Any] | None = None,
    ) -> list[IngestionJobRecord]:
        """为一个租户知识空间下的所有文档排队重建任务。"""
        self._require_editor(identity)
        space = await self.session.scalar(
            select(KnowledgeSpaceRecord).where(
                KnowledgeSpaceRecord.id == knowledge_space_id,
                KnowledgeSpaceRecord.tenant_id == identity.tenant_id,
                KnowledgeSpaceRecord.state == "active",
            )
        )
        if space is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Knowledge space not found.", 404)
        document_ids = list(
            (
                await self.session.scalars(
                    select(DocumentRecord.id)
                    .where(
                        DocumentRecord.tenant_id == identity.tenant_id,
                        DocumentRecord.knowledge_space_id == knowledge_space_id,
                        DocumentRecord.state != "deleted",
                    )
                    .order_by(DocumentRecord.id)
                )
            ).all()
        )
        return [
            await self.submit_rebuild(
                identity,
                document_id,
                correlation_id,
                reason=reason,
                processing_snapshot=processing_snapshot,
            )
            for document_id in document_ids
        ]

    async def rollback_generation(
        self,
        identity: RequestIdentity,
        document_id: str,
        generation_id: str,
        correlation_id: str,
        reason: str = "generation-rollback",
    ) -> IndexGenerationRecord:
        """将已验证的历史代次重新发布，并原子切换文档活动指针。"""
        self._require_editor(identity)
        document = await self.require_document(identity, document_id)
        if document.active_generation_id == generation_id:
            raise AppError(ErrorCategory.CONFLICT, "Generation is already active.", 409)
        target = await self.session.scalar(
            select(IndexGenerationRecord).where(
                IndexGenerationRecord.id == generation_id,
                IndexGenerationRecord.tenant_id == identity.tenant_id,
                IndexGenerationRecord.document_id == document.id,
                IndexGenerationRecord.state.in_({"active", "superseded"}),
            )
        )
        if target is None or target.chunk_count <= 0:
            raise AppError(ErrorCategory.CONFLICT, "Generation is not rollbackable.", 409)
        target_version = await self.session.scalar(
            select(DocumentVersionRecord).where(
                DocumentVersionRecord.id == target.document_version_id,
                DocumentVersionRecord.tenant_id == identity.tenant_id,
                DocumentVersionRecord.document_id == document.id,
                DocumentVersionRecord.state.in_({"active", "superseded"}),
            )
        )
        if target_version is None:
            raise AppError(ErrorCategory.CONFLICT, "Generation source version is unavailable.", 409)

        current_version_id = document.active_version_id
        current_generation_id = document.active_generation_id
        published_indexes: list[SearchAdapter] = []
        try:
            for index in (self.dense_index, self.lexical_index):
                await self._publish_index(index, target_version.id, target.id)
                published_indexes.append(index)
        except Exception as exc:
            for index in published_indexes:
                with suppress(Exception):
                    if current_version_id:
                        await self._publish_index(index, current_version_id, current_generation_id)
                    else:
                        await index.delete_document(document.id)
            raise AppError(
                ErrorCategory.DEPENDENCY_UNAVAILABLE,
                "Index generation rollback failed; the previous database pointer was kept.",
                503,
            ) from exc

        if current_version_id and current_version_id != target_version.id:
            current_version = await self.session.scalar(
                select(DocumentVersionRecord).where(
                    DocumentVersionRecord.id == current_version_id,
                    DocumentVersionRecord.tenant_id == identity.tenant_id,
                )
            )
            if current_version is not None:
                current_version.state = "superseded"
        await self.session.execute(
            update(ChunkRecord).where(ChunkRecord.document_id == document.id).values(active=False)
        )
        await self.session.execute(
            update(ChunkRecord)
            .where(
                ChunkRecord.document_id == document.id,
                ChunkRecord.index_generation_id == target.id,
            )
            .values(active=True)
        )
        target_version.state = "active"
        document.active_version_id = target_version.id
        document.active_generation_id = target.id
        document.state = "active"
        target.state = "active"
        target.superseded_at = None
        if current_generation_id and current_generation_id != target.id:
            await self.session.execute(
                update(IndexGenerationRecord)
                .where(
                    IndexGenerationRecord.id == current_generation_id,
                    IndexGenerationRecord.tenant_id == identity.tenant_id,
                )
                .values(state="superseded", superseded_at=utc_now())
            )
        tenant = await self.session.scalar(
            select(TenantRecord).where(TenantRecord.id == identity.tenant_id)
        )
        if tenant is not None:
            for index in (self.dense_index, self.lexical_index):
                setter = getattr(index, "set_acl_epoch", None)
                if setter:
                    setter(identity.tenant_id, tenant.acl_epoch)
        await AuditService(self.session).record(
            identity,
            AuditEventInput(
                "document.generation.rollback",
                "index_generation",
                target.id,
                "allowed",
                "success",
                correlation_id,
                {
                    "document_id": document.id,
                    "previous_generation_id": current_generation_id,
                    "target_version_id": target_version.id,
                    "reason": reason,
                },
            ),
        )
        await self.session.flush()
        return target

    async def retry(self, identity: RequestIdentity, job_id: str) -> IngestionJobRecord:
        """在重试额度内重新排队失败的摄取任务。"""
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
        job.next_attempt_at = utc_now()
        await OutboxService(self.session).add(
            identity.tenant_id,
            "ingestion.process",
            job.correlation_id,
            {
                "job_id": job.id,
                "document_id": job.document_id,
                "document_version_id": job.document_version_id,
                "processing_config_version_id": job.processing_config_version_id,
                "processing_snapshot": job.processing_snapshot,
                "authorization_epoch": job.payload.get("authorization_epoch"),
                "resource_version_id": job.document_version_id,
                "policy_version_ids": job.payload.get("policy_version_ids", []),
            },
            priority=job.priority,
            max_attempts=job.max_attempts,
        )
        await self.session.flush()
        return job

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
        document.active_generation_id = None
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
