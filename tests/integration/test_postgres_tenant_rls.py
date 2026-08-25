from __future__ import annotations

import os

import pytest
from enterprise_rag.infrastructure.database import create_engine as create_runtime_engine
from enterprise_rag.infrastructure.database import tenant_session_scope
from enterprise_rag.infrastructure.orm import KnowledgeSpaceRecord, TenantRecord
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.skipif(
    not os.getenv("RAG_TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="PostgreSQL RLS suite requires RAG_TEST_DATABASE_URL",
)


@pytest.mark.asyncio
async def test_postgres_rls_filters_identifiers_and_resets_pooled_context(
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        session.add_all(
            [
                TenantRecord(id="tenant-a", slug="tenant-a", name="Tenant A"),
                TenantRecord(id="tenant-b", slug="tenant-b", name="Tenant B"),
            ]
        )
        await session.flush()
        session.add_all(
            [
                KnowledgeSpaceRecord(id="space-a", tenant_id="tenant-a", name="A"),
                KnowledgeSpaceRecord(id="space-b", tenant_id="tenant-b", name="B"),
            ]
        )
        await session.commit()

    async with engine.begin() as connection:
        await connection.execute(text("ALTER TABLE knowledge_spaces ENABLE ROW LEVEL SECURITY"))
        await connection.execute(text("ALTER TABLE knowledge_spaces FORCE ROW LEVEL SECURITY"))
        await connection.execute(text("DROP POLICY IF EXISTS tenant_isolation ON knowledge_spaces"))
        await connection.execute(
            text(
                "CREATE POLICY tenant_isolation ON knowledge_spaces "
                "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')) "
                "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))"
            )
        )
        await connection.execute(
            text(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = "
                "'enterprise_rag_rls_test') THEN CREATE ROLE enterprise_rag_rls_test "
                "LOGIN PASSWORD 'enterprise_rag_rls_test'; END IF; END $$"
            )
        )
        await connection.execute(text("GRANT USAGE ON SCHEMA public TO enterprise_rag_rls_test"))
        await connection.execute(
            text("GRANT SELECT ON knowledge_spaces TO enterprise_rag_rls_test")
        )

    runtime_engine = create_runtime_engine(
        "postgresql+asyncpg://enterprise_rag_rls_test:enterprise_rag_rls_test"
        "@localhost:5432/enterprise_rag_test"
    )
    runtime_sessions = async_sessionmaker(runtime_engine, expire_on_commit=False)
    try:
        async with tenant_session_scope(runtime_sessions, "tenant-a") as session:
            rows = list((await session.scalars(select(KnowledgeSpaceRecord))).all())
            assert [row.id for row in rows] == ["space-a"]
            assert await session.get(KnowledgeSpaceRecord, "space-b") is None

        async with tenant_session_scope(runtime_sessions, "tenant-b") as session:
            rows = list((await session.scalars(select(KnowledgeSpaceRecord))).all())
            assert [row.id for row in rows] == ["space-b"]

        async with runtime_sessions() as session:
            rows = list((await session.scalars(select(KnowledgeSpaceRecord))).all())
            assert rows == []
    finally:
        await runtime_engine.dispose()
