from enterprise_rag.application.audit import AuditEventInput, AuditService
from enterprise_rag.domain.types import RequestIdentity, Role
from enterprise_rag.infrastructure.orm import TenantRecord
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_audit_chain_is_tenant_scoped_and_verifiable(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    identity = RequestIdentity(
        tenant_id="tenant-a",
        user_id="admin",
        roles=frozenset({Role.ADMINISTRATOR}),
    )
    async with sessions() as session:
        session.add(TenantRecord(id="tenant-a", name="Tenant A"))
        audit = AuditService(session)
        await audit.record(
            identity,
            AuditEventInput(
                action="knowledge.create",
                resource_type="knowledge_space",
                resource_id="space-1",
                authorization_result="allowed",
                outcome="success",
                correlation_id="correlation-1",
                details={"api_token": "must-not-leak", "name": "Policies"},
            ),
        )
        await audit.record(
            identity,
            AuditEventInput(
                action="knowledge.archive",
                resource_type="knowledge_space",
                resource_id="space-1",
                authorization_result="allowed",
                outcome="success",
                correlation_id="correlation-2",
                details={},
            ),
        )
        await session.commit()
    async with sessions() as session:
        audit = AuditService(session)
        events = await audit.list_for_tenant("tenant-a")
        assert len(events) == 2
        assert events[-1].details["api_token"] == "[REDACTED]"
        assert await audit.verify_chain("tenant-a")
        await session.execute(
            update(type(events[0]))
            .where(type(events[0]).id == events[0].id)
            .values(outcome="tampered")
        )
        await session.commit()
    async with sessions() as session:
        assert not await AuditService(session).verify_chain("tenant-a")
