from enterprise_rag.application.assistants import AssistantDraft, AssistantService
from enterprise_rag.application.providers import default_registry
from enterprise_rag.domain.types import RequestIdentity, Role
from enterprise_rag.infrastructure.orm import KnowledgeSpaceRecord, TenantRecord
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_cross_tenant_references_fail_activation_validation(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    identity = RequestIdentity(
        "tenant-a", "admin", roles=frozenset({Role.ADMINISTRATOR})
    )
    async with sessions() as session:
        session.add_all(
            [TenantRecord(id="tenant-a", name="A"), TenantRecord(id="tenant-b", name="B")]
        )
        await session.flush()
        session.add_all(
            [
                KnowledgeSpaceRecord(
                    id="space-b", tenant_id="tenant-b", name="Restricted B"
                ),
            ]
        )
        await session.flush()
        service = AssistantService(session, default_registry())
        _, version = await service.create_assistant(
            identity,
            "Assistant A",
            AssistantDraft(["space-b"], {}, {}),
        )
        errors = await service.validate_version(identity, version)
        assert errors == ["all knowledge spaces must be active and tenant-owned"]
