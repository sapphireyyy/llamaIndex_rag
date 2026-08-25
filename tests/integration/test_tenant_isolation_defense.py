from __future__ import annotations

from enterprise_rag.application.ports import RetrievalCandidate
from enterprise_rag.application.retrieval import RetrievalPolicy, RetrievalService
from enterprise_rag.application.security import DatabaseAuthorizationService
from enterprise_rag.domain.types import RequestIdentity, Role
from enterprise_rag.infrastructure.orm import (
    ChunkRecord,
    DataSourceRecord,
    DocumentRecord,
    DocumentVersionRecord,
    KnowledgeSpaceRecord,
    SpaceMembershipRecord,
    TelemetryEventRecord,
    TenantRecord,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class LeakySearch:
    def __init__(self, candidates: list[RetrievalCandidate]) -> None:
        self.candidates = candidates

    async def retrieve(self, query: str, scope: object, limit: int) -> list[RetrievalCandidate]:
        del query, scope, limit
        return self.candidates


class PassReranker:
    async def rerank(
        self, query: str, candidates: list[RetrievalCandidate], limit: int
    ) -> list[RetrievalCandidate]:
        del query
        return candidates[:limit]


async def test_post_adapter_validation_rejects_foreign_candidates_and_records_security_event(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        for suffix in ("a", "b"):
            tenant_id = f"tenant-{suffix}"
            space_id = f"space-{suffix}"
            source_id = f"source-{suffix}"
            document_id = f"document-{suffix}"
            version_id = f"version-{suffix}"
            session.add(TenantRecord(id=tenant_id, slug=tenant_id, name=tenant_id))
            session.add(KnowledgeSpaceRecord(id=space_id, tenant_id=tenant_id, name=space_id))
            await session.flush()
            session.add(
                SpaceMembershipRecord(
                    id=f"space-membership-{suffix}",
                    tenant_id=tenant_id,
                    knowledge_space_id=space_id,
                    principal_token="group:employees",
                    role="reader",
                )
            )
            session.add(
                DataSourceRecord(
                    id=source_id,
                    tenant_id=tenant_id,
                    knowledge_space_id=space_id,
                    external_id=source_id,
                    kind="upload",
                )
            )
            await session.flush()
            document = DocumentRecord(
                id=document_id,
                tenant_id=tenant_id,
                knowledge_space_id=space_id,
                data_source_id=source_id,
                source_identity=document_id,
                title=document_id,
                active_version_id=version_id,
                acl_tokens=["group:employees"],
            )
            session.add(document)
            session.add(
                DocumentVersionRecord(
                    id=version_id,
                    tenant_id=tenant_id,
                    document_id=document_id,
                    content_hash=suffix * 64,
                    source_key=f"tenants/{tenant_id}/documents/{version_id}/source.txt",
                    mime_type="text/plain",
                    size_bytes=10,
                    state="active",
                )
            )
            session.add(
                ChunkRecord(
                    id=f"chunk-{suffix}",
                    tenant_id=tenant_id,
                    knowledge_space_id=space_id,
                    document_id=document_id,
                    document_version_id=version_id,
                    ordinal=0,
                    text=f"content {suffix}",
                    acl_tokens=["group:employees"],
                    active=True,
                )
            )
        await session.commit()

    candidates = [
        RetrievalCandidate(
            f"chunk-{suffix}",
            f"content {suffix}",
            1.0,
            {
                "tenant_id": f"tenant-{suffix}",
                "knowledge_space_id": f"space-{suffix}",
                "document_id": f"document-{suffix}",
                "document_version_id": f"version-{suffix}",
                "acl_tokens": ["group:employees"],
            },
            "leaky",
        )
        for suffix in ("a", "b")
    ]
    identity = RequestIdentity(
        "tenant-a",
        "alice",
        groups=("employees",),
        roles=frozenset({Role.READER}),
    )
    async with sessions() as session:
        service = RetrievalService(
            session,
            DatabaseAuthorizationService(sessions),
            LeakySearch(candidates),
            LeakySearch(candidates),
            PassReranker(),
        )
        result = await service.retrieve(
            identity, ["space-a"], "content", [], RetrievalPolicy()
        )
        await session.commit()
    assert [item.chunk_id for item in result.evidence] == ["chunk-a"]
    assert result.trace["foreign_candidates_rejected"] == 2
    async with sessions() as session:
        events = list(
            (
                await session.scalars(
                    select(TelemetryEventRecord).where(
                        TelemetryEventRecord.tenant_id == "tenant-a",
                        TelemetryEventRecord.kind == "security",
                    )
                )
            ).all()
        )
        assert len(events) == 1
        assert events[0].content == {}
