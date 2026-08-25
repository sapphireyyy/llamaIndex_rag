"""应用层端口：定义数据库、对象存储、搜索、模型等可替换能力。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from enterprise_rag.domain.types import AuthorizationScope, JobMessage, RequestIdentity

T = TypeVar("T")


class Repository(Protocol[T]):
    """持久化实体的最小异步仓储接口。"""

    async def get(self, entity_id: str) -> T | None:
        """按标识读取实体。"""
        ...

    async def add(self, entity: T) -> T:
        """把实体加入当前事务并返回实体。"""
        ...

    async def delete(self, entity: T) -> None:
        """删除当前事务中的实体。"""
        ...


class UnitOfWork(Protocol, AbstractAsyncContextManager["UnitOfWork"]):
    """事务工作单元接口，允许应用层提交或回滚。"""

    async def commit(self) -> None:
        """提交当前工作单元。"""
        ...

    async def rollback(self) -> None:
        """回滚当前工作单元。"""
        ...


class ObjectStore(Protocol):
    """原始文档对象的写入、读取、删除和存在性检查接口。"""

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        """写入对象并返回对象键。"""
        ...

    async def get(self, key: str) -> bytes:
        """读取对象内容。"""
        ...

    async def delete(self, key: str) -> None:
        """删除对象。"""
        ...

    async def exists(self, key: str) -> bool:
        """检查对象是否存在。"""
        ...


class MessageQueue(Protocol):
    """异步任务消息发布和消费接口。"""

    async def publish(self, message: JobMessage) -> None:
        """发布异步任务消息。"""
        ...

    def consume(self) -> AsyncIterator[JobMessage]:
        """返回持续消费任务消息的异步迭代器。"""
        ...


class SecretStore(Protocol):
    """密钥引用解析和状态检查接口，不直接规定秘密来源。"""

    async def resolve(self, reference: str) -> str:
        """解析非秘密引用并返回实际值。"""
        ...

    async def status(self, reference: str) -> bool:
        """检查引用是否可解析。"""
        ...


class ConnectorAdapter(Protocol):
    """外部数据源发现与内容拉取接口。"""

    capabilities: frozenset[str]

    async def discover(
        self, cursor: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        """发现外部数据源记录并返回下一页游标。"""
        ...

    async def fetch(self, source_identity: str) -> tuple[bytes, dict[str, Any]]:
        """拉取外部记录的原始内容和元数据。"""
        ...


class AuthorizationService(Protocol):
    """资源操作鉴权和检索 ACL 范围编译接口。"""

    async def require(
        self, identity: RequestIdentity, operation: str, resource_tenant_id: str, resource_id: str
    ) -> None:
        """校验身份能否对资源执行指定操作。"""
        ...

    async def retrieval_scope(
        self, identity: RequestIdentity, knowledge_space_ids: Sequence[str]
    ) -> AuthorizationScope:
        """编译检索所需的授权范围。"""
        ...


@dataclass(frozen=True, slots=True)
class ParsedUnit:
    """解析器输出的文本单元及页码、章节和格式元数据。"""

    text: str
    page: int | None
    section: str | None
    metadata: dict[str, Any]


class ParserAdapter(Protocol):
    """按扩展名解析原始文件的接口。"""

    supported_extensions: frozenset[str]

    async def parse(self, name: str, content: bytes) -> list[ParsedUnit]:
        """把文件内容解析为结构化文本单元。"""
        ...


@dataclass(frozen=True, slots=True)
class IndexDocument:
    """送入索引的分块文本、向量和授权元数据。"""

    chunk_id: str
    text: str
    embedding: tuple[float, ...]
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RetrievalCandidate:
    """单路或融合检索返回的候选分块及其得分来源。"""

    chunk_id: str
    text: str
    score: float
    metadata: dict[str, Any]
    source: str


class EmbeddingAdapter(Protocol):
    """批量生成文本向量的接口。"""

    async def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """批量生成文本向量。"""
        ...


class SearchAdapter(Protocol):
    """支持暂存、发布、删除和授权检索的索引接口。"""

    async def stage(self, documents: Sequence[IndexDocument]) -> None:
        """暂存索引文档，不立即对查询可见。"""
        ...

    async def publish(self, document_version_id: str) -> None:
        """发布指定文档版本。"""
        ...

    async def delete_document(self, document_id: str) -> None:
        """删除文档的索引内容。"""
        ...

    async def retrieve(
        self, query: str, scope: AuthorizationScope, top_k: int
    ) -> list[RetrievalCandidate]:
        """在授权范围内检索前 K 个候选分块。"""
        ...


class RerankerAdapter(Protocol):
    """对召回候选进行二次排序的接口。"""

    async def rerank(
        self, query: str, candidates: Sequence[RetrievalCandidate], top_k: int
    ) -> list[RetrievalCandidate]:
        """对候选分块重排并返回前 K 个结果。"""
        ...


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """一次模型调用的 token 用量和可选成本。"""

    input_tokens: int
    output_tokens: int
    cost: float | None


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """模型回答文本、用量和供应商标识。"""

    text: str
    usage: ModelUsage
    provider: str


class ModelAdapter(Protocol):
    """同步语义上的完整生成和异步分片输出模型接口。"""

    capabilities: frozenset[str]

    async def complete(self, system: str, user: str, context: str) -> ModelResponse:
        """执行完整模型生成。"""
        ...

    def stream(self, system: str, user: str, context: str) -> AsyncIterator[str]:
        """以异步迭代器输出模型生成片段。"""
        ...


class GuardrailAdapter(Protocol):
    """输入与输出安全检查接口。"""

    async def inspect_input(self, text: str) -> list[str]:
        """检查用户输入并返回风险标签。"""
        ...

    async def inspect_output(self, text: str) -> list[str]:
        """检查模型输出并返回风险标签。"""
        ...


class TraceSink(Protocol):
    """结构化追踪事件写出接口。"""

    async def emit(self, event: dict[str, Any]) -> None:
        """写出一个结构化追踪事件。"""
        ...
