from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from enterprise_rag.application.configuration import ConfigurationService
from enterprise_rag.application.orphans import OrphanObjectSweeper, OrphanSweepResult
from enterprise_rag.application.task_leases import TaskLeaseService
from enterprise_rag.config import Settings
from enterprise_rag.domain.tenant_config import (
    TenantConfiguration,
    ingestion_processing_snapshot,
)
from enterprise_rag.domain.types import JobMessage
from enterprise_rag.infrastructure.object_store import FileSystemObjectStore
from enterprise_rag.infrastructure.orm import (
    Base,
    DataSourceRecord,
    DocumentRecord,
    DocumentVersionRecord,
    IngestionJobRecord,
    KnowledgeSpaceRecord,
    TenantRecord,
)
from enterprise_rag.infrastructure.queue import InProcessQueue, OutboxDispatcher, OutboxService
from enterprise_rag.infrastructure.rls import (
    RLS_POLICY_EXPRESSIONS,
    RLS_PROTECTED_TABLES,
    _policy_is_tenant_scoped,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def test_ingestion_configuration_is_versioned_and_validated() -> None:
    configuration = TenantConfiguration()
    snapshot = ingestion_processing_snapshot(configuration, "config-v1")

    assert configuration.ingestion.chunk_size == 512
    assert configuration.ingestion.chunk_overlap == 64
    assert configuration.ingestion.ocr_mode == "disabled"
    assert snapshot["configuration_version_id"] == "config-v1"
    assert snapshot["policy_hash"]

    with pytest.raises(ValueError, match="smaller than chunk_size"):
        TenantConfiguration.model_validate(
            {"ingestion": {"chunk_size": 32, "chunk_overlap": 32}}
        )
    with pytest.raises(ValueError, match="required OCR mode"):
        TenantConfiguration.model_validate({"ingestion": {"ocr_mode": "required"}})


def test_production_configuration_requires_durable_explicit_provider() -> None:
    valid = Settings(
        environment="production",
        dev_auth_enabled=False,
        oidc_enabled=True,
        oidc_issuer="https://issuer.example",
        oidc_jwks_url="https://issuer.example/.well-known/jwks.json",
        database_url="postgresql+asyncpg://runtime/db",
        migration_database_url="postgresql+asyncpg://migration/db",
        queue_backend="rabbitmq",
        ingestion_execution_mode="queued",
        model_backend="openai_compatible",
        model_api_base="https://model.example/v1",
        model_name="chat-model",
        model_api_key_reference="env://MODEL_API_KEY",
    )
    assert valid.validate_runtime() == []

    invalid = valid.model_copy(update={"model_backend": "extractive"})
    assert "production requires an explicit generative model provider" in (
        invalid.validate_runtime()
    )
    same_database_role = valid.model_copy(
        update={
            "migration_database_url": valid.database_url,
            "database_runtime_role": "enterprise_rag",
        }
    )
    errors = same_database_role.validate_runtime()
    assert "migration and runtime database URLs must be separate" in errors
    assert "production requires a non-owner database runtime role" in errors


def test_provider_config_rejects_nested_raw_secrets() -> None:
    sensitive_keys = {
        "password",
        "secret",
        "client_secret",
        "api_key",
        "access_token",
        "refresh_token",
        "private_key",
    }

    assert ConfigurationService._contains_raw_secret(
        {"transport": {"credentials": [{"api_key": "raw"}]}}, sensitive_keys
    )
    assert not ConfigurationService._contains_raw_secret(
        {"transport": {"secret_binding": "binding-id"}}, sensitive_keys
    )


@pytest.mark.asyncio
async def test_queue_delivery_requires_explicit_settlement() -> None:
    queue = InProcessQueue()
    message = JobMessage(
        "job-delivery",
        "ingestion.process",
        "tenant-a",
        "correlation",
        {"tenant_id": "tenant-a"},
        max_attempts=2,
    )
    await queue.publish(message)

    delivery = await queue.get()
    assert delivery.message.id == message.id
    await delivery.nack(requeue=True)
    await delivery.nack(requeue=True)
    assert queue.size == 1

    retry = await queue.get()
    assert retry.message.attempt == 1
    await retry.ack()
    await retry.ack()
    assert queue.size == 0


@pytest.mark.asyncio
async def test_outbox_dispatch_can_be_scoped_to_one_tenant(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        await OutboxService(session).add("tenant-a", "ingestion.process", "corr-a", {})
        await OutboxService(session).add("tenant-b", "ingestion.process", "corr-b", {})
        await session.commit()

    published: list[JobMessage] = []

    async def publish(message: JobMessage) -> None:
        published.append(message)

    dispatcher = OutboxDispatcher(sessions, publish)
    assert await dispatcher.dispatch_batch(tenant_id="tenant-a") == (1, 0)
    assert [message.tenant_id for message in published] == ["tenant-a"]
    assert await dispatcher.dispatch_batch(tenant_id="tenant-b") == (1, 0)
    assert [message.tenant_id for message in published] == ["tenant-a", "tenant-b"]


@pytest.mark.asyncio
async def test_lease_recovery_is_tenant_scoped_and_claim_is_conditional(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with sessions() as session:
        session.add_all(
            [
                TenantRecord(id="tenant-a", slug="lease-a", name="Lease A"),
                TenantRecord(id="tenant-b", slug="lease-b", name="Lease B"),
                IngestionJobRecord(
                    id="job-a",
                    tenant_id="tenant-a",
                    kind="ingestion.process",
                    status="running",
                    stage="embedding",
                    correlation_id="corr-a",
                    attempt_count=0,
                    max_attempts=3,
                    claimed_by="dead-worker",
                    lease_expires_at=now - timedelta(seconds=1),
                ),
                IngestionJobRecord(
                    id="job-b",
                    tenant_id="tenant-b",
                    kind="ingestion.process",
                    status="running",
                    stage="embedding",
                    correlation_id="corr-b",
                    attempt_count=0,
                    max_attempts=3,
                    claimed_by="dead-worker",
                    lease_expires_at=now - timedelta(seconds=1),
                ),
                IngestionJobRecord(
                    id="job-claim",
                    tenant_id="tenant-a",
                    kind="ingestion.process",
                    status="queued",
                    stage="queued",
                    correlation_id="corr-claim",
                    attempt_count=0,
                    max_attempts=3,
                ),
            ]
        )
        await session.commit()

    async with sessions() as session:
        claimed = await TaskLeaseService(session, "worker-a").claim(
            "job-claim", "tenant-a", now=now
        )
        assert claimed is not None
        assert claimed.status == "running"
        await session.commit()

    async with sessions() as session:
        assert await TaskLeaseService(session, "worker-b").claim(
            "job-claim", "tenant-a", now=now
        ) is None
        recovered = await TaskLeaseService(session, "worker-b").recover_expired(
            tenant_id="tenant-a", now=now
        )
        await session.commit()

    assert recovered == ["job-a"]
    async with sessions() as session:
        tenant_a_job = await session.get(IngestionJobRecord, "job-a")
        tenant_b_job = await session.get(IngestionJobRecord, "job-b")
        assert tenant_a_job is not None and tenant_a_job.status == "queued"
        assert tenant_b_job is not None and tenant_b_job.status == "running"


@pytest.mark.asyncio
async def test_orphan_sweeper_retains_referenced_and_recent_objects(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant_id = "tenant-orphan"
    referenced_key = f"tenants/{tenant_id}/originals/doc-1/referenced.txt"
    orphan_key = f"tenants/{tenant_id}/originals/doc-2/orphan.txt"
    recent_key = f"tenants/{tenant_id}/originals/doc-3/recent.txt"
    store = FileSystemObjectStore(tmp_path)
    await store.put(referenced_key, b"referenced", "text/plain")
    await store.put(orphan_key, b"orphan", "text/plain")
    await store.put(recent_key, b"recent", "text/plain")
    now = datetime.now(UTC)
    old_timestamp = (now - timedelta(days=2)).timestamp()
    os.utime(tmp_path / orphan_key, (old_timestamp, old_timestamp))

    async with sessions() as session:
        source = DataSourceRecord(
            id="source-orphan",
            tenant_id=tenant_id,
            knowledge_space_id="space-orphan",
            external_id="upload",
            kind="upload",
        )
        session.add_all(
            [
                TenantRecord(id=tenant_id, slug=tenant_id, name="Orphan tenant"),
                KnowledgeSpaceRecord(
                    id="space-orphan", tenant_id=tenant_id, name="Orphan space"
                ),
                source,
                DocumentRecord(
                    id="doc-1",
                    tenant_id=tenant_id,
                    knowledge_space_id="space-orphan",
                    data_source_id=source.id,
                    source_identity="doc-1",
                    title="Referenced",
                ),
                DocumentVersionRecord(
                    id="version-1",
                    tenant_id=tenant_id,
                    document_id="doc-1",
                    content_hash="a" * 64,
                    source_key=referenced_key,
                    mime_type="text/plain",
                    size_bytes=10,
                ),
            ]
        )
        await session.commit()
        result = await OrphanObjectSweeper(session, store, retention_seconds=86_400).sweep(
            tenant_id, now=now
        )

    assert result == OrphanSweepResult(3, 1, 2, 1)
    assert not await store.exists(orphan_key)
    assert await store.exists(referenced_key)
    assert await store.exists(recent_key)


def test_rls_inventory_covers_every_orm_table() -> None:
    tables = set(Base.metadata.tables)
    assert tables == set(RLS_PROTECTED_TABLES)
    assert set(RLS_POLICY_EXPRESSIONS) == tables
    assert all("app.tenant_id" in expression for expression in RLS_POLICY_EXPRESSIONS.values())


def test_rls_preflight_rejects_unscoped_alternate_policies() -> None:
    assert _policy_is_tenant_scoped(
        {"cmd": "*", "qual": "tenant_id = app.tenant_id", "with_check": "tenant_id = app.tenant_id"}
    )
    assert not _policy_is_tenant_scoped(
        {"cmd": "*", "qual": "true", "with_check": "tenant_id = app.tenant_id"}
    )
    assert _policy_is_tenant_scoped(
        {"cmd": "r", "qual": "tenant_id = app.tenant_id", "with_check": None}
    )
