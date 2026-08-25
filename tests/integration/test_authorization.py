import pytest
from enterprise_rag.api.errors import AppError
from enterprise_rag.application.security import DatabaseAuthorizationService, bump_acl_epoch
from enterprise_rag.domain.types import RequestIdentity, Role
from enterprise_rag.infrastructure.orm import (
    KnowledgeSpaceRecord,
    SpaceMembershipRecord,
    TenantRecord,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_retrieval_scope_filters_membership_and_tracks_epoch(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        session.add(TenantRecord(id="tenant-a", name="Tenant A"))
        await session.flush()
        session.add_all(
            [
                KnowledgeSpaceRecord(id="space-1", tenant_id="tenant-a", name="Allowed"),
                KnowledgeSpaceRecord(id="space-2", tenant_id="tenant-a", name="Denied"),
            ]
        )
        await session.flush()
        session.add_all(
            [
                SpaceMembershipRecord(
                    id="membership-1",
                    tenant_id="tenant-a",
                    knowledge_space_id="space-1",
                    principal_token="group:employees",
                    role="reader",
                ),
            ]
        )
        await session.commit()
    identity = RequestIdentity(
        "tenant-a", "alice", groups=("employees",), roles=frozenset({Role.READER})
    )
    service = DatabaseAuthorizationService(sessions)
    scope = await service.retrieval_scope(identity, ["space-1", "space-2"])
    assert scope.knowledge_space_ids == {"space-1"}
    assert scope.acl_epoch == 1
    async with sessions() as session:
        assert await bump_acl_epoch(session, "tenant-a") == 2
        await session.commit()
    assert (await service.retrieval_scope(identity, ["space-1"])).acl_epoch == 2


async def test_authorization_fails_closed_for_cross_tenant(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    service = DatabaseAuthorizationService(sessions)
    identity = RequestIdentity(
        "tenant-a", "alice", roles=frozenset({Role.ADMINISTRATOR})
    )
    with pytest.raises(AppError) as raised:
        await service.require(identity, "knowledge.admin", "tenant-b", "space-x")
    assert raised.value.status_code == 404
