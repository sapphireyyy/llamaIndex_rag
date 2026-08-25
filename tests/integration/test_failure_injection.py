import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from enterprise_rag.api.errors import AppError
from enterprise_rag.application.ingestion import IngestionService
from enterprise_rag.application.ports import (
    IndexDocument,
    ModelResponse,
    ParsedUnit,
    RetrievalCandidate,
)
from enterprise_rag.application.providers import ExtractiveModelAdapter, ModelGateway
from enterprise_rag.application.retrieval import RetrievalPolicy, RetrievalService
from enterprise_rag.application.security import DatabaseAuthorizationService
from enterprise_rag.domain.types import AuthorizationScope, RequestIdentity, Role
from enterprise_rag.infrastructure.database import check_database, create_engine
from enterprise_rag.infrastructure.object_store import FileSystemObjectStore
from enterprise_rag.infrastructure.orm import (
    DataSourceRecord,
    KnowledgeSpaceRecord,
    OutboxRecord,
    SpaceMembershipRecord,
    TenantRecord,
)
from enterprise_rag.infrastructure.queue import (
    InProcessQueue,
    OutboxDispatcher,
    consume_with_retry,
)
from enterprise_rag.infrastructure.search import (
    DeterministicEmbedding,
    MemoryLexicalSearch,
    MemoryVectorSearch,
    TokenOverlapReranker,
)
from enterprise_rag.infrastructure.telemetry import TelemetryInput, TelemetryService
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class UnavailableSearch:
    async def stage(self, documents: Sequence[IndexDocument]) -> None:
        del documents

    async def publish(self, document_version_id: str) -> None:
        del document_version_id

    async def delete_document(self, document_id: str) -> None:
        del document_id

    async def retrieve(
        self, query: str, scope: AuthorizationScope, top_k: int
    ) -> list[RetrievalCandidate]:
        del query, scope, top_k
        raise ConnectionError("search outage")


class FailingReranker:
    async def rerank(
        self, query: str, candidates: Sequence[RetrievalCandidate], top_k: int
    ) -> list[RetrievalCandidate]:
        del query, candidates, top_k
        raise ConnectionError("reranker outage")


class StaticSearch(UnavailableSearch):
    async def retrieve(
        self, query: str, scope: AuthorizationScope, top_k: int
    ) -> list[RetrievalCandidate]:
        del query, scope, top_k
        return [RetrievalCandidate("chunk", "evidence", 1.0, {}, "static")]


class FailingModel:
    capabilities = frozenset({"completion"})

    async def complete(self, system: str, user: str, context: str) -> ModelResponse:
        del system, user, context
        raise TimeoutError("model timeout")

    def stream(self, system: str, user: str, context: str) -> AsyncIterator[str]:
        del system, user, context

        async def iterator() -> AsyncIterator[str]:
            if False:
                yield ""

        return iterator()


async def test_all_search_dependencies_down_fail_closed(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        session.add(TenantRecord(id="tenant-a", name="A"))
        await session.flush()
        session.add(KnowledgeSpaceRecord(id="space-a", tenant_id="tenant-a", name="A"))
        await session.flush()
        session.add_all(
            [
                SpaceMembershipRecord(
                    id="member-a",
                    tenant_id="tenant-a",
                    knowledge_space_id="space-a",
                    principal_token="user:alice",
                    role="reader",
                ),
            ]
        )
        await session.commit()
    identity = RequestIdentity("tenant-a", "alice", roles=frozenset({Role.READER}))
    async with sessions() as session:
        service = RetrievalService(
            session,
            DatabaseAuthorizationService(sessions),
            UnavailableSearch(),
            UnavailableSearch(),
            TokenOverlapReranker(),
        )
        with pytest.raises(AppError) as raised:
            await service.retrieve(identity, ["space-a"], "question", [], RetrievalPolicy())
        assert raised.value.status_code == 503


async def test_model_timeout_uses_only_explicit_fallback() -> None:
    gateway = ModelGateway(FailingModel(), ExtractiveModelAdapter(), retries=0)
    with pytest.raises(RuntimeError):
        await gateway.complete("", "", "evidence", allow_fallback=False)
    assert (await gateway.complete("", "", "evidence", allow_fallback=True)).text == "evidence"


async def test_object_store_outage_and_queue_retry_are_bounded(tmp_path) -> None:
    store = FileSystemObjectStore(tmp_path / "objects")
    with pytest.raises(FileNotFoundError):
        await store.get("missing/document")

    queue = InProcessQueue()
    attempts = 0

    async def handler(message) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise ConnectionError("transient")
        raise asyncio.CancelledError

    from enterprise_rag.domain.types import JobMessage

    await queue.publish(JobMessage("message", "test", "tenant", "corr", {}, max_attempts=2))
    with pytest.raises(asyncio.CancelledError):
        await consume_with_retry(queue, handler)
    assert attempts == 2


async def test_database_and_telemetry_outages_degrade_without_disclosure(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'missing' / 'db.sqlite').as_posix()}")
    assert await check_database(engine) is False
    await engine.dispose()

    class FailingSession:
        def add(self, value: object) -> None:
            del value

        async def scalar(self, statement: object) -> None:
            del statement
            return None

        async def flush(self) -> None:
            raise ConnectionError("telemetry exporter unavailable")

    service = TelemetryService(cast(Any, FailingSession()), False)
    with pytest.raises(ConnectionError, match="telemetry exporter"):
        await service.record(TelemetryInput("tenant", "corr", "api", "request", "200"))


async def test_reranker_outage_degrades_only_when_policy_allows(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        session.add(TenantRecord(id="tenant-b", name="B"))
        await session.flush()
        session.add(KnowledgeSpaceRecord(id="space-b", tenant_id="tenant-b", name="B"))
        await session.flush()
        session.add_all(
            [
                SpaceMembershipRecord(
                    id="member-b",
                    tenant_id="tenant-b",
                    knowledge_space_id="space-b",
                    principal_token="user:bob",
                    role="reader",
                ),
            ]
        )
        await session.commit()
        identity = RequestIdentity("tenant-b", "bob", roles=frozenset({Role.READER}))
        service = RetrievalService(
            session,
            DatabaseAuthorizationService(sessions),
            StaticSearch(),
            StaticSearch(),
            FailingReranker(),
        )
        degraded = await service.retrieve(
            identity, ["space-b"], "question", [], RetrievalPolicy()
        )
        assert degraded.degraded is True
        with pytest.raises(AppError):
            await service.retrieve(
                identity,
                ["space-b"],
                "question",
                [],
                RetrievalPolicy(allow_single_search_degradation=False),
            )


class FailingParser:
    supported_extensions = frozenset({".txt"})

    async def parse(self, name: str, content: bytes) -> list[ParsedUnit]:
        del name, content
        raise RuntimeError("parser crashed")


class FailingEmbedding:
    async def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        del texts
        raise ConnectionError("embedding outage")


class FailingStage(UnavailableSearch):
    async def stage(self, documents: Sequence[IndexDocument]) -> None:
        del documents
        raise ConnectionError("index outage")


async def test_parser_embedding_and_index_failures_never_publish(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    identity = RequestIdentity(
        "tenant-c", "admin", roles=frozenset({Role.ADMINISTRATOR})
    )
    async with sessions() as session:
        session.add(TenantRecord(id="tenant-c", name="C"))
        await session.flush()
        session.add(KnowledgeSpaceRecord(id="space-c", tenant_id="tenant-c", name="C"))
        await session.flush()
        session.add_all(
            [
                DataSourceRecord(
                    id="source-c",
                    tenant_id="tenant-c",
                    knowledge_space_id="space-c",
                    external_id="upload",
                    kind="upload",
                ),
            ]
        )
        await session.commit()
        store = FileSystemObjectStore(tmp_path / "objects")
        embedding = DeterministicEmbedding()
        dense = MemoryVectorSearch(embedding)
        lexical = MemoryLexicalSearch()

        parser_failure = IngestionService(
            session, store, FailingParser(), embedding, dense, lexical, {".txt"}, 1000
        )
        parser_job = await parser_failure.submit_upload(
            identity, "source-c", "parse.txt", b"content", "text/plain", "parser"
        )
        assert (await parser_failure.process(parser_job.id)).status == "failed"

        from enterprise_rag.infrastructure.parsers import AllowListParser

        embedding_failure = IngestionService(
            session,
            store,
            AllowListParser(),
            FailingEmbedding(),
            dense,
            lexical,
            {".txt"},
            1000,
        )
        embedding_job = await embedding_failure.submit_upload(
            identity, "source-c", "embed.txt", b"content", "text/plain", "embedding"
        )
        assert (await embedding_failure.process(embedding_job.id)).status == "failed"

        index_failure = IngestionService(
            session,
            store,
            AllowListParser(),
            embedding,
            FailingStage(),
            lexical,
            {".txt"},
            1000,
        )
        index_job = await index_failure.submit_upload(
            identity, "source-c", "index.txt", b"content", "text/plain", "index"
        )
        assert (await index_failure.process(index_job.id)).status == "failed"


async def test_outbox_survives_dispatcher_restart_and_deduplicates_delivery(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        session.add(
            OutboxRecord(
                id="event-restart",
                tenant_id="tenant",
                kind="cleanup",
                correlation_id="correlation",
                payload={},
            )
        )
        await session.commit()
    delivered: list[str] = []

    async def publish(message) -> None:
        delivered.append(message.id)

    first_process = OutboxDispatcher(sessions, publish)
    assert await first_process.dispatch_batch() == (1, 0)
    restarted_process = OutboxDispatcher(sessions, publish)
    assert await restarted_process.dispatch_batch() == (0, 0)
    assert delivered == ["event-restart"]
