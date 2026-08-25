from __future__ import annotations

import os
import uuid

import httpx
import pytest
from enterprise_rag.api.auth import verify_oidc_token
from enterprise_rag.application.ports import IndexDocument
from enterprise_rag.config import Settings
from enterprise_rag.domain.types import AuthorizationScope, JobMessage, PlatformRole
from enterprise_rag.infrastructure.container import Container
from enterprise_rag.infrastructure.object_store import S3ObjectStore, tenant_object_key
from enterprise_rag.infrastructure.queue import RabbitMQQueue
from enterprise_rag.infrastructure.search import (
    DeterministicEmbedding,
    OpenSearchLexicalSearch,
    QdrantVectorSearch,
)

pytestmark = pytest.mark.skipif(
    os.getenv("RAG_RUN_PRODUCTION_TESTS") != "1",
    reason="production provider suite is opt-in",
)


def _name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


async def test_keycloak_issues_verifiable_tenant_and_role_claims() -> None:
    async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
        response = await client.post(
            "http://localhost:8080/realms/enterprise-rag/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "enterprise-rag",
                "username": "rag-admin",
                "password": "rag-admin-dev",
            },
        )
    response.raise_for_status()
    identity = verify_oidc_token(
        response.json()["access_token"],
        Settings(
            environment="staging",
            oidc_enabled=True,
            oidc_issuer="http://localhost:8080/realms/enterprise-rag",
            oidc_jwks_url=(
                "http://localhost:8080/realms/enterprise-rag/"
                "protocol/openid-connect/certs"
            ),
            oidc_audience="enterprise-rag",
            dev_auth_enabled=False,
        ),
    )
    assert identity.tenant_hint == "tenant-alpha"
    assert "platform-administrators" in identity.groups
    assert identity.platform_roles == frozenset({PlatformRole.PLATFORM_ADMINISTRATOR})


async def test_minio_round_trip_uses_tenant_scoped_key() -> None:
    store = S3ObjectStore(
        "http://localhost:9000",
        _name("enterprise-rag-test"),
        "enterprise-rag",
        "enterprise-rag-dev-secret",
    )
    await store.start()
    key = tenant_object_key("tenant-a", "originals", "document-a", "policy.txt")
    await store.put(key, b"production-like object", "text/plain")
    other_key = tenant_object_key("tenant-b", "originals", "document-a", "policy.txt")
    await store.put(other_key, b"isolated tenant object", "text/plain")
    assert await store.health()
    assert await store.exists(key)
    assert await store.get(key) == b"production-like object"
    assert await store.get(other_key) == b"isolated tenant object"
    await store.delete(key)
    await store.delete(other_key)
    assert not await store.exists(key)


async def test_rabbitmq_durable_queue_round_trip_and_priority() -> None:
    queue = RabbitMQQueue("amqp://guest:guest@localhost:5672/", _name("rag-jobs"))
    await queue.start()
    try:
        await queue.publish(JobMessage("low", "ingest", "tenant-a", "corr-1", {}, priority=1))
        await queue.publish(
            JobMessage("high", "revoke", "tenant-a", "corr-2", {}, priority=100)
        )
        assert await queue.health()
        assert (await queue.get()).id == "high"
        assert (await queue.get()).id == "low"
    finally:
        await queue.close()


async def test_qdrant_and_opensearch_enforce_identical_security_contract() -> None:
    suffix = uuid.uuid4().hex
    embedding = DeterministicEmbedding()
    dense = QdrantVectorSearch(
        "http://localhost:6333", f"rag-chunks-{suffix}", embedding
    )
    lexical = OpenSearchLexicalSearch(
        "http://localhost:9200", f"rag-chunks-{suffix}"
    )
    await dense.start()
    await lexical.start()
    try:
        text = "VPN access requires manager approval and multi-factor authentication."
        other_text = "VPN credentials for a different tenant must remain private."
        vector, other_vector = await embedding.embed([text, other_text])
        allowed_document = IndexDocument(
            "chunk-a-v1",
            text,
            vector,
            {
                "tenant_id": "tenant-a",
                "knowledge_space_id": "space-a",
                "document_id": "document-a",
                "document_version_id": "version-a-v1",
                "acl_tokens": ["group:employees"],
                "acl_epoch": 4,
            },
        )
        cross_tenant_document = IndexDocument(
            "chunk-b-v1",
            other_text,
            other_vector,
            {
                "tenant_id": "tenant-b",
                "knowledge_space_id": "space-b",
                "document_id": "document-b",
                "document_version_id": "version-b-v1",
                "acl_tokens": ["group:employees"],
                "acl_epoch": 4,
            },
        )
        for index in (dense, lexical):
            await index.stage([allowed_document, cross_tenant_document])
            await index.publish("version-a-v1")
            await index.publish("version-b-v1")
            index.set_acl_epoch("tenant-a", 4)

        allowed = AuthorizationScope(
            "tenant-a",
            frozenset({"group:employees"}),
            frozenset({"space-a"}),
            4,
        )
        denied = AuthorizationScope(
            "tenant-a",
            frozenset({"group:contractors"}),
            frozenset({"space-a"}),
            4,
        )
        stale = AuthorizationScope(
            "tenant-a",
            frozenset({"group:employees"}),
            frozenset({"space-a"}),
            3,
        )
        for index in (dense, lexical):
            results = await index.retrieve("VPN access", allowed, 10)
            assert [item.chunk_id for item in results] == ["chunk-a-v1"]
            assert await index.retrieve("VPN access", denied, 10) == []
            with pytest.raises(PermissionError, match="stale"):
                await index.retrieve("VPN access", stale, 10)

        updated_text = "VPN access now requires security-team approval."
        updated_vector = (await embedding.embed([updated_text]))[0]
        updated = IndexDocument(
            "chunk-a-v2",
            updated_text,
            updated_vector,
            {
                **allowed_document.metadata,
                "document_version_id": "version-a-v2",
            },
        )
        for index in (dense, lexical):
            await index.stage([updated])
            await index.publish("version-a-v2")
            results = await index.retrieve("VPN access", allowed, 10)
            assert [item.chunk_id for item in results] == ["chunk-a-v2"]
            await index.delete_document("document-a")
            assert await index.retrieve("VPN access", allowed, 10) == []
    finally:
        await dense.close()
        await lexical.close()


async def test_container_selects_and_checks_all_production_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suffix = uuid.uuid4().hex
    monkeypatch.setenv("MINIO_ROOT_USER", "enterprise-rag")
    monkeypatch.setenv("MINIO_ROOT_PASSWORD", "enterprise-rag-dev-secret")
    monkeypatch.setenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
    settings = Settings(
        environment="test",
        database_url=(
            "postgresql+asyncpg://enterprise_rag:enterprise_rag_dev"
            "@localhost:5432/enterprise_rag"
        ),
        object_store_backend="s3",
        s3_bucket=f"enterprise-rag-container-{suffix}",
        queue_backend="rabbitmq",
        rabbitmq_queue_name=f"enterprise-rag-jobs-{suffix}",
        cache_backend="redis",
        vector_backend="qdrant",
        qdrant_collection=f"enterprise-rag-chunks-{suffix}",
        lexical_backend="opensearch",
        opensearch_index=f"enterprise-rag-chunks-{suffix}",
    )
    container = Container(settings)
    await container.start()
    try:
        checks = await container.readiness()
        assert checks
        assert all(checks.values()), checks
    finally:
        await container.stop()
