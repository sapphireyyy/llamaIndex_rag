from typing import Any, cast

from enterprise_rag.application.query import (
    CitationDraft,
    QueryOutcome,
    QueryService,
)
from enterprise_rag.domain.types import RequestIdentity, TerminalStatus
from enterprise_rag.infrastructure.orm import (
    ChunkRecord,
    DataSourceRecord,
    DocumentRecord,
    DocumentVersionRecord,
    KnowledgeSpaceRecord,
    TelemetryEventRecord,
    TenantRecord,
)
from enterprise_rag.infrastructure.telemetry import TelemetryInput, TelemetryService
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_post_generation_validation_blocks_revoked_evidence(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        session.add(TenantRecord(id="tenant", name="Tenant"))
        await session.flush()
        session.add(KnowledgeSpaceRecord(id="space", tenant_id="tenant", name="Space"))
        await session.flush()
        session.add(
            DataSourceRecord(
                id="source",
                tenant_id="tenant",
                knowledge_space_id="space",
                external_id="source",
                kind="upload",
            )
        )
        await session.flush()
        document = DocumentRecord(
            id="document",
            tenant_id="tenant",
            knowledge_space_id="space",
            data_source_id="source",
            source_identity="source",
            title="Title",
            active_version_id=None,
            state="active",
        )
        session.add(document)
        await session.flush()
        version = DocumentVersionRecord(
            id="version",
            tenant_id="tenant",
            document_id=document.id,
            content_hash="a" * 64,
            source_key="tenants/tenant/documents/version/source.txt",
            mime_type="text/plain",
            size_bytes=13,
            state="active",
        )
        session.add(version)
        await session.flush()
        document.active_version_id = version.id
        chunk = ChunkRecord(
            id="chunk",
            tenant_id="tenant",
            knowledge_space_id="space",
            document_id=document.id,
            document_version_id="version",
            ordinal=0,
            text="Grounded fact",
            active=True,
        )
        session.add(chunk)
        await session.flush()
        service = QueryService(
            session,
            cast(Any, None),
            cast(Any, None),
            cast(Any, None),
            cast(Any, None),
        )
        initial = QueryOutcome(
            TerminalStatus.SUCCESS,
            "Grounded fact",
            (CitationDraft("chunk", "version", "Source", None, None),),
            True,
            False,
            {},
            {},
        )
        identity = RequestIdentity("tenant", "reader")
        assert (
            await service._post_generation_validate(identity, initial)
        ).status == TerminalStatus.SUCCESS
        document.state = "deleted"
        await session.flush()
        blocked = await service._post_generation_validate(identity, initial)
        assert blocked.status == TerminalStatus.FAILED
        assert blocked.citations == ()


async def test_telemetry_tenant_isolation_retry_linkage_and_version_provenance(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        service = TelemetryService(session, False)
        await service.record(
            TelemetryInput(
                "tenant-a",
                "correlation-a",
                "ingestion",
                "retry",
                "failed",
                attributes={
                    "job_id": "job-a",
                    "attempt_count": 2,
                    "document_version_id": "version-a",
                    "assistant_version_id": "assistant-version-a",
                    "cost": None,
                },
                content={"prompt": "must not persist"},
            )
        )
        await service.record(
            TelemetryInput(
                "tenant-b", "correlation-b", "query", "terminal", "success"
            )
        )
        await session.commit()
        tenant_a = list(
            (
                await session.scalars(
                    select(TelemetryEventRecord).where(
                        TelemetryEventRecord.tenant_id == "tenant-a"
                    )
                )
            ).all()
        )
        assert len(tenant_a) == 1
        assert tenant_a[0].correlation_id == "correlation-a"
        assert tenant_a[0].attributes["attempt_count"] == 2
        assert tenant_a[0].attributes["document_version_id"] == "version-a"
        assert tenant_a[0].attributes["assistant_version_id"] == "assistant-version-a"
        assert tenant_a[0].attributes["cost"] is None
        assert tenant_a[0].content == {}
