"""依赖容器：组装数据库、队列、搜索、模型和应用服务所需的运行时资源。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from enterprise_rag.application.guardrails import BuiltinGuardrails
from enterprise_rag.application.ports import MessageQueue, ModelAdapter, ObjectStore, SearchAdapter
from enterprise_rag.application.providers import (
    AdapterRegistry,
    ExtractiveModelAdapter,
    ModelGateway,
    OpenAICompatibleModelAdapter,
    default_registry,
)
from enterprise_rag.application.tenant_bootstrap import (
    ensure_tenant_control_plane,
    tenants_without_administrator,
)
from enterprise_rag.config import Settings
from enterprise_rag.domain.types import Role, new_id
from enterprise_rag.infrastructure.cache import MemoryCoordinationStore, RedisCoordinationStore
from enterprise_rag.infrastructure.database import (
    check_database,
    create_engine,
    create_schema_for_tests,
    create_session_factory,
)
from enterprise_rag.infrastructure.limits import SlidingWindowRateLimiter
from enterprise_rag.infrastructure.object_store import FileSystemObjectStore, S3ObjectStore
from enterprise_rag.infrastructure.orm import (
    GroupRecord,
    PrincipalGroupRecord,
    PrincipalRecord,
    TenantGroupRoleRecord,
    TenantMembershipRecord,
    TenantRecord,
)
from enterprise_rag.infrastructure.parsers import AllowListParser
from enterprise_rag.infrastructure.queue import InProcessQueue, RabbitMQQueue
from enterprise_rag.infrastructure.search import (
    DeterministicEmbedding,
    MemoryLexicalSearch,
    MemoryVectorSearch,
    MilvusVectorSearch,
    OpenSearchLexicalSearch,
    QdrantVectorSearch,
    TokenOverlapReranker,
)
from enterprise_rag.infrastructure.secrets import EnvironmentSecretStore


@dataclass(slots=True)
class Container:
    """应用运行时资源容器，统一持有可替换的基础设施实例。"""

    settings: Settings
    engine: AsyncEngine | None = None
    sessions: async_sessionmaker[AsyncSession] | None = None
    object_store: ObjectStore | None = None
    queue: MessageQueue | None = None
    coordination: MemoryCoordinationStore | RedisCoordinationStore | None = None
    registry: AdapterRegistry | None = None
    parser: AllowListParser | None = None
    embedding: DeterministicEmbedding | None = None
    dense_index: SearchAdapter | None = None
    lexical_index: SearchAdapter | None = None
    reranker: TokenOverlapReranker | None = None
    model_gateway: ModelGateway | None = None
    guardrails: BuiltinGuardrails | None = None
    rate_limiter: SlidingWindowRateLimiter | None = None
    query_slots: asyncio.Semaphore | None = None
    ingestion_slots: asyncio.Semaphore | None = None

    async def start(self) -> None:
        """按当前环境配置装配数据库、检索、模型与并发控制依赖。"""
        problems = self.settings.validate_runtime()
        if problems:
            raise RuntimeError("invalid runtime configuration: " + "; ".join(problems))
        self.engine = create_engine(self.settings.database_url)
        self.sessions = create_session_factory(self.engine)
        secrets = EnvironmentSecretStore()
        if self.settings.object_store_backend == "s3":
            self.object_store = S3ObjectStore(
                self.settings.s3_endpoint_url,
                self.settings.s3_bucket,
                await secrets.resolve(self.settings.s3_access_key_reference),
                await secrets.resolve(self.settings.s3_secret_key_reference),
                self.settings.s3_region,
            )
        else:
            self.settings.object_store_root.mkdir(parents=True, exist_ok=True)
            self.object_store = FileSystemObjectStore(self.settings.object_store_root)
        starter = getattr(self.object_store, "start", None)
        if starter is not None:
            await starter()
        if self.settings.queue_backend == "rabbitmq":
            self.queue = RabbitMQQueue(
                await secrets.resolve(self.settings.rabbitmq_url_reference),
                self.settings.rabbitmq_queue_name,
            )
        else:
            self.queue = InProcessQueue()
        starter = getattr(self.queue, "start", None)
        if starter is not None:
            await starter()
        if self.settings.cache_backend == "redis":
            self.coordination = RedisCoordinationStore(self.settings.redis_url)
        else:
            self.coordination = MemoryCoordinationStore()
        await self.coordination.start()
        self.registry = default_registry()
        self.parser = AllowListParser()
        self.embedding = DeterministicEmbedding()
        if self.settings.vector_backend == "qdrant":
            self.dense_index = QdrantVectorSearch(
                self.settings.qdrant_url,
                self.settings.qdrant_collection,
                self.embedding,
            )
        elif self.settings.vector_backend == "milvus":
            milvus_token = None
            if self.settings.milvus_token_reference:
                milvus_token = await secrets.resolve(
                    self.settings.milvus_token_reference
                )
            self.dense_index = MilvusVectorSearch(
                self.settings.milvus_url,
                self.settings.milvus_collection,
                self.embedding,
                token=milvus_token,
            )
        else:
            self.dense_index = MemoryVectorSearch(self.embedding)
        starter = getattr(self.dense_index, "start", None)
        if starter is not None:
            await starter()
        if self.settings.lexical_backend == "opensearch":
            self.lexical_index = OpenSearchLexicalSearch(
                self.settings.opensearch_url, self.settings.opensearch_index
            )
        else:
            self.lexical_index = MemoryLexicalSearch()
        starter = getattr(self.lexical_index, "start", None)
        if starter is not None:
            await starter()
        self.reranker = TokenOverlapReranker()
        primary_model: ModelAdapter = ExtractiveModelAdapter()
        if self.settings.model_backend == "openai_compatible":
            api_key = await EnvironmentSecretStore().resolve(
                self.settings.model_api_key_reference
            )
            primary_model = OpenAICompatibleModelAdapter(
                self.settings.model_api_base,
                api_key,
                self.settings.model_name,
                self.settings.request_timeout_seconds,
            )
        self.model_gateway = ModelGateway(
            primary_model,
            fallback=ExtractiveModelAdapter()
            if self.settings.environment == "development"
            else None,
            timeout_seconds=self.settings.request_timeout_seconds,
            retries=self.settings.provider_retry_attempts,
            requests_per_minute=self.settings.query_rate_limit_per_minute,
            concurrency_limit=self.settings.query_concurrency_limit,
        )
        self.guardrails = BuiltinGuardrails()
        self.rate_limiter = SlidingWindowRateLimiter()
        self.query_slots = asyncio.Semaphore(self.settings.query_concurrency_limit)
        self.ingestion_slots = asyncio.Semaphore(self.settings.ingestion_concurrency_limit)
        if self.settings.environment == "test":
            await create_schema_for_tests(self.engine)
        if self.settings.dev_auth_enabled and self.settings.environment in {"development", "test"}:
            async with self.sessions() as session:
                tenant = await session.get(TenantRecord, self.settings.dev_tenant_id)
                if tenant is None:
                    tenant = TenantRecord(
                        id=self.settings.dev_tenant_id,
                        slug=self.settings.dev_tenant_id.replace("_", "-").lower(),
                        name="Development tenant",
                    )
                    session.add(tenant)
                    await session.flush()
                await ensure_tenant_control_plane(
                    session,
                    tenant,
                    created_by_subject="development-fixture",
                    administrator_external_id=self.settings.dev_user_id,
                    administrator_display_name="Development administrator",
                )
                if self.settings.environment == "development":
                    for tenant_id, tenant_name, reader_id in (
                        ("tenant-alpha", "Alpha development tenant", "alpha-reader"),
                        ("tenant-beta", "Beta development tenant", "beta-reader"),
                    ):
                        extra_tenant = await session.get(TenantRecord, tenant_id)
                        if extra_tenant is None:
                            extra_tenant = TenantRecord(
                                id=tenant_id, slug=tenant_id, name=tenant_name
                            )
                            session.add(extra_tenant)
                            await session.flush()
                        await ensure_tenant_control_plane(
                            session,
                            extra_tenant,
                            created_by_subject="development-fixture",
                            administrator_external_id=self.settings.dev_user_id,
                            administrator_display_name="Development administrator",
                        )
                        reader = await session.scalar(
                            select(PrincipalRecord).where(
                                PrincipalRecord.tenant_id == tenant_id,
                                PrincipalRecord.external_id == reader_id,
                            )
                        )
                        if reader is None:
                            reader = PrincipalRecord(
                                id=new_id("principal"),
                                tenant_id=tenant_id,
                                external_id=reader_id,
                                display_name=f"{tenant_name} reader",
                                source="development-fixture",
                            )
                            session.add(reader)
                            await session.flush()
                            session.add(
                                TenantMembershipRecord(
                                    id=new_id("tenant_membership"),
                                    tenant_id=tenant_id,
                                    principal_id=reader.id,
                                    direct_roles=[Role.READER.value],
                                    source="development-fixture",
                                )
                            )
                        group = await session.scalar(
                            select(GroupRecord).where(
                                GroupRecord.tenant_id == tenant_id,
                                GroupRecord.external_id == "employees",
                            )
                        )
                        if group is None:
                            group = GroupRecord(
                                id=new_id("group"),
                                tenant_id=tenant_id,
                                external_id="employees",
                                display_name="Employees",
                                source="development-fixture",
                            )
                            session.add(group)
                            await session.flush()
                        if await session.get(
                            PrincipalGroupRecord, (tenant_id, reader.id, group.id)
                        ) is None:
                            session.add(
                                PrincipalGroupRecord(
                                    tenant_id=tenant_id,
                                    principal_id=reader.id,
                                    group_id=group.id,
                                    source="development-fixture",
                                )
                            )
                        group_role = await session.scalar(
                            select(TenantGroupRoleRecord).where(
                                TenantGroupRoleRecord.tenant_id == tenant_id,
                                TenantGroupRoleRecord.group_id == group.id,
                                TenantGroupRoleRecord.role == Role.READER.value,
                            )
                        )
                        if group_role is None:
                            session.add(
                                TenantGroupRoleRecord(
                                    id=new_id("tenant_group_role"),
                                    tenant_id=tenant_id,
                                    group_id=group.id,
                                    role=Role.READER.value,
                                    source="development-fixture",
                                )
                            )
                        await ensure_tenant_control_plane(
                            session,
                            extra_tenant,
                            created_by_subject="development-fixture",
                        )
                await session.commit()

    async def stop(self) -> None:
        """关闭已初始化的基础设施资源。"""
        for component in (
            self.lexical_index,
            self.dense_index,
            self.coordination,
            self.queue,
            self.object_store,
        ):
            closer = getattr(component, "close", None)
            if closer is not None:
                await closer()
        if self.engine is not None:
            await self.engine.dispose()

    async def readiness(self) -> dict[str, bool]:
        """汇总配置、对象存储、数据库和队列的就绪状态。"""
        async def healthy(component: object | None) -> bool:
            """调用组件的可选 health 方法，并将异常/缺失视为未就绪。"""
            if component is None:
                return False
            check = getattr(component, "health", None)
            return bool(await check()) if check is not None else True

        return {
            "configuration": not self.settings.validate_runtime(),
            "tenant_bootstrap": not await self.bootstrap_problems(),
            "object_store": await healthy(self.object_store),
            "database": self.engine is not None and await check_database(self.engine),
            "queue": await healthy(self.queue),
            "cache": await healthy(self.coordination),
            "vector_search": await healthy(self.dense_index),
            "lexical_search": await healthy(self.lexical_index),
        }

    async def bootstrap_problems(self) -> list[str]:
        """Report production tenant-control-plane prerequisites without claim fallback."""
        if self.settings.environment in {"development", "test"}:
            return []
        if self.sessions is None:
            return ["tenant database session is unavailable"]
        try:
            async with self.sessions() as session:
                missing = await tenants_without_administrator(session)
                if missing:
                    return [
                        "active tenants without an administrator: " + ", ".join(missing)
                    ]
        except Exception:
            return ["tenant bootstrap state could not be verified"]
        return []
