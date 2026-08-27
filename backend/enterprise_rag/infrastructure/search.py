"""搜索基础设施：实现内存、Milvus、Qdrant、OpenSearch 检索及 token 重排。"""

from __future__ import annotations

import asyncio
import json
import math
import re
import uuid
from collections.abc import Collection, Mapping, Sequence
from typing import Any

import httpx

from enterprise_rag.application.ports import (
    IndexDocument,
    RetrievalCandidate,
)
from enterprise_rag.domain.types import AuthorizationScope, stable_hash, stable_json_hash


def _tokens(text: str) -> set[str]:
    """分词并应用中英文同义词归一化，供词法检索和重排共用。"""
    raw = set(re.findall(r"[a-z0-9_-]+|[\u4e00-\u9fff]+", text.casefold()))
    aliases = {
        "vacation": "leave",
        "holiday": "leave",
        "supervisor": "manager",
        "authorization": "approval",
        "authorisation": "approval",
        "mfa": "multi-factor",
        "vpn": "remote-access",
        "休假": "leave",
        "假期": "leave",
        "经理": "manager",
        "主管": "manager",
        "审批": "approval",
        "批准": "approval",
        "远程访问": "remote-access",
    }
    tokens = {aliases.get(token, token) for token in raw}
    for token in raw:
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            tokens.update(
                aliases.get(token[index : index + 2], token[index : index + 2])
                for index in range(len(token) - 1)
            )
    return tokens


class DeterministicEmbedding:
    """用稳定哈希把文本映射为可重复向量，适用于本地开发和测试。"""

    def __init__(self, dimensions: int = 64) -> None:
        """设置向量维度。"""
        self.dimensions = dimensions

    async def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """为每段文本生成归一化的确定性向量。"""
        output: list[tuple[float, ...]] = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for token in _tokens(text):
                digest = bytes.fromhex(stable_hash(token))
                index = int.from_bytes(digest[:4], "big") % self.dimensions
                vector[index] += -1.0 if digest[4] % 2 else 1.0
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            output.append(tuple(value / norm for value in vector))
        return output


class _MemorySearchBase:
    """内存索引共享分阶段写入、发布、删除和 ACL 校验逻辑。"""

    source_name = "memory"

    def __init__(self) -> None:
        """初始化内存文档、活动版本和 ACL 版本索引。"""
        self._documents: dict[str, IndexDocument] = {}
        self._active_versions_by_document: dict[str, str] = {}
        self._active_generations_by_document: dict[str, str] = {}
        self._acl_epochs: dict[str, int] = {}

    async def stage(self, documents: Sequence[IndexDocument]) -> None:
        """把文档暂存到内存，不立即使其对检索可见。"""
        for document in documents:
            self._documents[document.chunk_id] = document

    async def publish(self, document_version_id: str) -> None:
        """将指定文档版本标记为对应文档的活动版本。"""
        document = next(
            (
                item
                for item in self._documents.values()
                if item.metadata.get("document_version_id") == document_version_id
            ),
            None,
        )
        if document is None:
            raise RuntimeError("cannot publish an empty staged document version")
        self._active_versions_by_document[str(document.metadata["document_id"])] = (
            document_version_id
        )
        generation_id = document.metadata.get("index_generation_id")
        if generation_id:
            self._active_generations_by_document[str(document.metadata["document_id"])] = str(
                generation_id
            )

    async def publish_generation(
        self, document_version_id: str, index_generation_id: str
    ) -> None:
        """仅把指定内容版本的指定处理代次切换为活动投影。"""
        document = next(
            (
                item
                for item in self._documents.values()
                if item.metadata.get("document_version_id") == document_version_id
                and str(item.metadata.get("index_generation_id")) == index_generation_id
            ),
            None,
        )
        if document is None:
            raise RuntimeError("cannot publish an empty staged index generation")
        document_id = str(document.metadata["document_id"])
        self._active_versions_by_document[document_id] = document_version_id
        self._active_generations_by_document[document_id] = index_generation_id

    async def delete_document(self, document_id: str) -> None:
        """删除文档的活动指针和全部内存分块。"""
        self._active_versions_by_document.pop(document_id, None)
        self._active_generations_by_document.pop(document_id, None)
        self._documents = {
            key: value
            for key, value in self._documents.items()
            if value.metadata.get("document_id") != document_id
        }

    async def delete_generation(self, document_id: str, index_generation_id: str) -> None:
        """删除指定文档的非活动处理代次，保留其他代次供回滚。"""
        self._documents = {
            key: value
            for key, value in self._documents.items()
            if not (
                str(value.metadata.get("document_id")) == document_id
                and str(value.metadata.get("index_generation_id")) == index_generation_id
            )
        }

    async def inspect_generation(
        self, document_id: str, index_generation_id: str
    ) -> dict[str, object]:
        """返回不含正文的代次数量和关键 metadata 指纹。"""
        documents = [
            item
            for item in self._documents.values()
            if str(item.metadata.get("document_id")) == document_id
            and str(item.metadata.get("index_generation_id")) == index_generation_id
        ]
        signatures = [
            {
                key: item.metadata.get(key)
                for key in (
                    "tenant_id",
                    "knowledge_space_id",
                    "document_id",
                    "document_version_id",
                    "index_generation_id",
                    "acl_epoch",
                    "acl_tokens",
                )
            }
            for item in documents
        ]
        return {
            "count": len(documents),
            "metadata_hash": stable_json_hash(
                {"items": sorted(signatures, key=lambda item: str(item))}
            ),
            "published_count": sum(
                self._active_generations_by_document.get(document_id)
                == str(item.metadata.get("index_generation_id"))
                for item in documents
            ),
        }

    def set_acl_epoch(self, tenant_id: str, epoch: int) -> None:
        """更新租户 ACL 版本，用于拒绝过期授权范围。"""
        self._acl_epochs[tenant_id] = epoch

    def _authorized(self, document: IndexDocument, scope: AuthorizationScope) -> bool:
        """检查租户、空间、活动版本、文档范围和主体令牌是否全部匹配。"""
        metadata = document.metadata
        current_epoch = self._acl_epochs.get(scope.tenant_id, scope.acl_epoch)
        if scope.acl_epoch != current_epoch:
            raise PermissionError("authorization scope is stale")
        acl_tokens = set(metadata.get("acl_tokens", []))
        active_generation = self._active_generations_by_document.get(
            str(metadata.get("document_id"))
        )
        return bool(
            metadata.get("tenant_id") == scope.tenant_id
            and metadata.get("knowledge_space_id") in scope.knowledge_space_ids
            and metadata.get("document_version_id")
            == self._active_versions_by_document.get(str(metadata.get("document_id")))
            and (
                active_generation is None
                or str(metadata.get("index_generation_id")) == active_generation
            )
            and (
                not scope.index_generation_ids
                or str(metadata.get("index_generation_id")) in scope.index_generation_ids
            )
            and (
                not scope.document_ids
                or metadata.get("document_id") in scope.document_ids
            )
            and acl_tokens.intersection(scope.principal_tokens)
        )


class MemoryVectorSearch(_MemorySearchBase):
    """基于确定性向量余弦相似度的内存稠密检索实现。"""

    source_name = "dense"

    def __init__(self, embedding: DeterministicEmbedding) -> None:
        """注入向量生成器并初始化内存索引。"""
        super().__init__()
        self.embedding = embedding

    async def retrieve(
        self, query: str, scope: AuthorizationScope, top_k: int
    ) -> list[RetrievalCandidate]:
        """过滤授权活动文档，按余弦相似度返回前 K 个候选。"""
        if not scope.knowledge_space_ids or scope.acl_epoch <= 0:
            raise PermissionError("compiled authorization scope is required")
        query_vector = (await self.embedding.embed([query]))[0]
        candidates = []
        for document in self._documents.values():
            if not self._authorized(document, scope):
                continue
            score = sum(a * b for a, b in zip(query_vector, document.embedding, strict=True))
            if score <= 0:
                continue
            candidates.append(
                RetrievalCandidate(
                    document.chunk_id,
                    document.text,
                    score,
                    document.metadata,
                    self.source_name,
                )
            )
        return sorted(candidates, key=lambda item: (-item.score, item.chunk_id))[:top_k]


class MemoryLexicalSearch(_MemorySearchBase):
    """基于归一化 token 交集比例的内存词法检索实现。"""

    source_name = "lexical"

    async def retrieve(
        self, query: str, scope: AuthorizationScope, top_k: int
    ) -> list[RetrievalCandidate]:
        """过滤授权活动文档，按查询词覆盖率返回前 K 个候选。"""
        if not scope.knowledge_space_ids or scope.acl_epoch <= 0:
            raise PermissionError("compiled authorization scope is required")
        query_tokens = _tokens(query)
        candidates = []
        for document in self._documents.values():
            if not self._authorized(document, scope):
                continue
            document_tokens = _tokens(document.text)
            score = len(query_tokens.intersection(document_tokens)) / max(len(query_tokens), 1)
            if score <= 0:
                continue
            candidates.append(
                RetrievalCandidate(
                    document.chunk_id,
                    document.text,
                    score,
                    document.metadata,
                    self.source_name,
                )
            )
        return sorted(candidates, key=lambda item: (-item.score, item.chunk_id))[:top_k]


def _require_scope(scope: AuthorizationScope) -> None:
    """确认检索调用携带非空知识空间和有效 ACL 版本。"""
    if not scope.knowledge_space_ids or scope.acl_epoch <= 0:
        raise PermissionError("compiled authorization scope is required")


class _RemoteSearchBase:
    """Milvus/Qdrant/OpenSearch 远程索引共享 ACL 版本校验逻辑。"""

    source_name = "remote"

    def __init__(self) -> None:
        """初始化远程索引的租户 ACL 版本缓存。"""
        self._acl_epochs: dict[str, int] = {}

    def set_acl_epoch(self, tenant_id: str, epoch: int) -> None:
        """更新远程索引使用的租户 ACL 版本。"""
        self._acl_epochs[tenant_id] = epoch

    def _validate_scope(self, scope: AuthorizationScope) -> None:
        """校验检索范围存在且没有落后于当前 ACL 版本。"""
        _require_scope(scope)
        current_epoch = self._acl_epochs.get(scope.tenant_id, scope.acl_epoch)
        if scope.acl_epoch != current_epoch:
            raise PermissionError("authorization scope is stale")

    @staticmethod
    def _metadata_signature(source: Mapping[str, Any]) -> dict[str, object]:
        """Extract reconciliation metadata without retaining indexed document text."""
        nested = source.get("metadata")
        metadata = nested if isinstance(nested, Mapping) else source
        return {
            key: metadata.get(key, source.get(key))
            for key in (
                "tenant_id",
                "knowledge_space_id",
                "document_id",
                "document_version_id",
                "index_generation_id",
                "acl_epoch",
                "acl_tokens",
            )
        }

    @classmethod
    def _metadata_hash(cls, rows: Sequence[Mapping[str, Any]]) -> str:
        """Compute the same stable metadata fingerprint as PostgreSQL reconciliation."""
        signatures = [cls._metadata_signature(row) for row in rows]
        return stable_json_hash({"items": sorted(signatures, key=lambda item: str(item))})


class QdrantVectorSearch(_RemoteSearchBase):
    """Qdrant 稠密检索适配器，索引查询始终带租户和 ACL 过滤器。"""

    source_name = "dense"

    def __init__(
        self,
        url: str,
        collection: str,
        embedding: DeterministicEmbedding,
        dimensions: int = 64,
    ) -> None:
        """创建不使用环境代理的 Qdrant HTTP 客户端。"""
        super().__init__()
        self.url = url.rstrip("/")
        self.collection = collection
        self.embedding = embedding
        self.dimensions = dimensions
        self._client = httpx.AsyncClient(
            base_url=self.url,
            timeout=20.0,
            # Qdrant is an internal service; local development must not route
            # localhost requests through an ambient HTTP proxy.
            trust_env=False,
        )

    async def start(self) -> None:
        """创建向量集合，已存在时保持现有集合。"""
        response = await self._client.put(
            f"/collections/{self.collection}",
            json={"vectors": {"size": self.dimensions, "distance": "Cosine"}},
        )
        if response.status_code not in {200, 201, 409}:
            response.raise_for_status()

    async def stage(self, documents: Sequence[IndexDocument]) -> None:
        """以 published=false 写入待发布的向量点和元数据。"""
        if not documents:
            return
        points: list[dict[str, Any]] = []
        for document in documents:
            payload: dict[str, Any] = dict(document.metadata)
            payload.update(
                {
                    "chunk_id": document.chunk_id,
                    "text": document.text,
                    "published": False,
                }
            )
            points.append(
                {
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, document.chunk_id)),
                "vector": list(document.embedding),
                "payload": payload,
                },
            )
        response = await self._client.put(
            f"/collections/{self.collection}/points",
            params={"wait": "true"},
            json={"points": points},
        )
        response.raise_for_status()

    @staticmethod
    def _match(key: str, value: object) -> dict[str, object]:
        """构造 Qdrant payload 精确匹配条件。"""
        return {"key": key, "match": {"value": value}}

    async def _version_document_id(
        self, document_version_id: str, index_generation_id: str | None = None
    ) -> str:
        """从已暂存版本中找到文档 ID，防止发布空版本。"""
        must = [self._match("document_version_id", document_version_id)]
        if index_generation_id is not None:
            must.append(self._match("index_generation_id", index_generation_id))
        response = await self._client.post(
            f"/collections/{self.collection}/points/scroll",
            json={
                "filter": {"must": must},
                "limit": 1,
                "with_payload": True,
                "with_vector": False,
            },
        )
        response.raise_for_status()
        points = response.json().get("result", {}).get("points", [])
        if not points:
            raise RuntimeError("cannot publish an empty staged document version")
        return str(points[0]["payload"]["document_id"])

    async def _set_published(self, must: list[dict[str, object]], value: bool) -> None:
        """按过滤条件批量切换向量点 published 标记。"""
        response = await self._client.post(
            f"/collections/{self.collection}/points/payload",
            params={"wait": "true"},
            json={"payload": {"published": value}, "filter": {"must": must}},
        )
        response.raise_for_status()

    async def publish(self, document_version_id: str) -> None:
        """先取消同文档旧版本，再发布目标版本，保证切换原子语义。"""
        document_id = await self._version_document_id(document_version_id)
        await self._set_published([self._match("document_id", document_id)], False)
        await self._set_published(
            [self._match("document_version_id", document_version_id)], True
        )

    async def publish_generation(
        self, document_version_id: str, index_generation_id: str
    ) -> None:
        """只发布指定内容版本的处理代次。"""
        document_id = await self._version_document_id(document_version_id, index_generation_id)
        await self._set_published([self._match("document_id", document_id)], False)
        await self._set_published(
            [
                self._match("document_version_id", document_version_id),
                self._match("index_generation_id", index_generation_id),
            ],
            True,
        )

    async def inspect_generation(
        self, document_id: str, index_generation_id: str
    ) -> dict[str, object]:
        """Count a generation and fingerprint metadata without reading indexed text."""
        points: list[dict[str, Any]] = []
        offset: object | None = None
        while True:
            payload: dict[str, object] = {
                "filter": {
                    "must": [
                        self._match("document_id", document_id),
                        self._match("index_generation_id", index_generation_id),
                    ]
                },
                "limit": 10_000,
                "with_payload": [
                    "tenant_id",
                    "knowledge_space_id",
                    "document_id",
                    "document_version_id",
                    "index_generation_id",
                    "acl_epoch",
                    "acl_tokens",
                    "published",
                ],
                "with_vector": False,
            }
            if offset is not None:
                payload["offset"] = offset
            response = await self._client.post(
                f"/collections/{self.collection}/points/scroll", json=payload
            )
            response.raise_for_status()
            result = response.json().get("result", {})
            batch = result.get("points", []) if isinstance(result, dict) else []
            for point in batch if isinstance(batch, list) else []:
                if isinstance(point, dict):
                    points.append(dict(point.get("payload") or {}))
            next_offset = result.get("next_page_offset") if isinstance(result, dict) else None
            if not batch or next_offset is None or next_offset == offset:
                break
            offset = next_offset
        return {
            "count": len(points),
            "metadata_hash": self._metadata_hash(points),
            "published_count": sum(bool(item.get("published")) for item in points),
        }

    async def delete_document(self, document_id: str) -> None:
        """删除 Qdrant 中属于指定文档的全部点。"""
        response = await self._client.post(
            f"/collections/{self.collection}/points/delete",
            params={"wait": "true"},
            json={"filter": {"must": [self._match("document_id", document_id)]}},
        )
        response.raise_for_status()

    async def delete_generation(self, document_id: str, index_generation_id: str) -> None:
        """删除 Qdrant 中指定非活动处理代次的向量点。"""
        response = await self._client.post(
            f"/collections/{self.collection}/points/delete",
            params={"wait": "true"},
            json={
                "filter": {
                    "must": [
                        self._match("document_id", document_id),
                        self._match("index_generation_id", index_generation_id),
                    ]
                }
            },
        )
        response.raise_for_status()

    async def retrieve(
        self, query: str, scope: AuthorizationScope, top_k: int
    ) -> list[RetrievalCandidate]:
        """执行带 ACL、活动版本和可选文档范围过滤的向量查询。"""
        self._validate_scope(scope)
        must: list[dict[str, object]] = [
            self._match("tenant_id", scope.tenant_id),
            {"key": "knowledge_space_id", "match": {"any": sorted(scope.knowledge_space_ids)}},
            {"key": "acl_tokens", "match": {"any": sorted(scope.principal_tokens)}},
            self._match("published", True),
        ]
        if scope.document_ids:
            must.append(
                {"key": "document_id", "match": {"any": sorted(scope.document_ids)}}
            )
        if scope.index_generation_ids:
            must.append(
                {
                    "key": "index_generation_id",
                    "match": {"any": sorted(scope.index_generation_ids)},
                }
            )
        vector = (await self.embedding.embed([query]))[0]
        response = await self._client.post(
            f"/collections/{self.collection}/points/query",
            json={
                "query": list(vector),
                "filter": {"must": must},
                "limit": top_k,
                "with_payload": True,
                "score_threshold": 0.0,
            },
        )
        response.raise_for_status()
        points = response.json().get("result", {}).get("points", [])
        return [
            RetrievalCandidate(
                str(point["payload"]["chunk_id"]),
                str(point["payload"]["text"]),
                float(point["score"]),
                {
                    key: value
                    for key, value in point["payload"].items()
                    if key not in {"chunk_id", "text", "published"}
                },
                self.source_name,
            )
            for point in points
        ]

    async def health(self) -> bool:
        """检查 Qdrant 集合是否可访问。"""
        try:
            response = await self._client.get(f"/collections/{self.collection}")
            return response.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        """关闭 Qdrant HTTP 客户端。"""
        await self._client.aclose()


class MilvusVectorSearch(_RemoteSearchBase):
    """Milvus 稠密检索适配器，保持与 Qdrant 相同的发布和 ACL 合约。"""

    source_name = "dense"

    def __init__(
        self,
        uri: str,
        collection: str,
        embedding: DeterministicEmbedding,
        dimensions: int = 64,
        token: str | None = None,
    ) -> None:
        """保存 Milvus 连接参数；同步 SDK 调用会在线程中执行，避免阻塞事件循环。"""
        super().__init__()
        self.uri = uri
        self.collection = collection
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", collection):
            raise ValueError(
                "Milvus collection names must contain only letters, numbers, and underscores"
            )
        self.embedding = embedding
        self.dimensions = dimensions
        self.token = token
        self._client: Any | None = None

    def _client_or_raise(self) -> Any:
        """延迟创建 MilvusClient，使 memory/Qdrant 测试不强制安装 Milvus SDK。"""
        if self._client is not None:
            return self._client
        try:
            from pymilvus import MilvusClient  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "Milvus backend requires the optional 'pymilvus' dependency"
            ) from exc
        kwargs: dict[str, str] = {"uri": self.uri}
        if self.token:
            kwargs["token"] = self.token
        self._client = MilvusClient(**kwargs)
        return self._client

    @staticmethod
    def _quote(value: str) -> str:
        """把字符串编码为 Milvus 过滤表达式中的安全字符串字面量。"""
        return json.dumps(value, ensure_ascii=False)

    @classmethod
    def _in_expression(cls, field: str, values: Sequence[str]) -> str:
        """生成 Milvus 的 VARCHAR IN 表达式。"""
        return f"{field} in [{', '.join(cls._quote(value) for value in values)}]"

    @classmethod
    def _scope_expression(
        cls,
        scope: AuthorizationScope,
        *,
        published: bool | None = None,
        document_ids: Collection[str] | None = None,
    ) -> str:
        """把租户、知识空间、ACL 和发布状态编译为 Milvus 过滤条件。"""
        expressions = [
            f"tenant_id == {cls._quote(scope.tenant_id)}",
            cls._in_expression("knowledge_space_id", sorted(scope.knowledge_space_ids)),
            f"ARRAY_CONTAINS_ANY(acl_tokens, {json.dumps(sorted(scope.principal_tokens))})",
        ]
        if published is not None:
            expressions.append(f"published == {'true' if published else 'false'}")
        if document_ids:
            expressions.append(cls._in_expression("document_id", sorted(document_ids)))
        if scope.index_generation_ids:
            expressions.append(
                cls._in_expression("index_generation_id", sorted(scope.index_generation_ids))
            )
        return " && ".join(expressions)

    def _schema(self) -> Any:
        """创建固定字段 schema，把权限字段显式化以支持服务端过滤。"""
        from pymilvus import DataType, MilvusClient

        schema = MilvusClient.create_schema(
            auto_id=False,
            enable_dynamic_field=False,
        )
        schema.add_field(
            field_name="id",
            datatype=DataType.VARCHAR,
            is_primary=True,
            max_length=512,
        )
        schema.add_field(
            field_name="vector",
            datatype=DataType.FLOAT_VECTOR,
            dim=self.dimensions,
        )
        schema.add_field(field_name="chunk_id", datatype=DataType.VARCHAR, max_length=512)
        schema.add_field(field_name="text", datatype=DataType.VARCHAR, max_length=65535)
        schema.add_field(field_name="tenant_id", datatype=DataType.VARCHAR, max_length=512)
        schema.add_field(
            field_name="knowledge_space_id",
            datatype=DataType.VARCHAR,
            max_length=512,
        )
        schema.add_field(field_name="document_id", datatype=DataType.VARCHAR, max_length=512)
        schema.add_field(
            field_name="document_version_id",
            datatype=DataType.VARCHAR,
            max_length=512,
        )
        schema.add_field(
            field_name="index_generation_id",
            datatype=DataType.VARCHAR,
            max_length=512,
        )
        schema.add_field(
            field_name="acl_tokens",
            datatype=DataType.ARRAY,
            element_type=DataType.VARCHAR,
            max_capacity=256,
            max_length=512,
        )
        schema.add_field(field_name="published", datatype=DataType.BOOL)
        schema.add_field(field_name="metadata", datatype=DataType.JSON)
        return schema

    def _start_sync(self) -> None:
        """连接 Milvus 并创建带 COSINE 索引的集合。"""
        client = self._client_or_raise()
        if client.has_collection(collection_name=self.collection):
            return
        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="vector",
            index_type="AUTOINDEX",
            metric_type="COSINE",
        )
        client.create_collection(
            collection_name=self.collection,
            schema=self._schema(),
            index_params=index_params,
        )

    @staticmethod
    def _row(document: IndexDocument) -> dict[str, Any]:
        """把领域索引文档映射为 Milvus 实体行。"""
        metadata = dict(document.metadata)
        acl_tokens = [str(item) for item in metadata.get("acl_tokens", [])]
        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, document.chunk_id)),
            "vector": list(document.embedding),
            "chunk_id": document.chunk_id,
            "text": document.text,
            "tenant_id": str(metadata["tenant_id"]),
            "knowledge_space_id": str(metadata["knowledge_space_id"]),
            "document_id": str(metadata["document_id"]),
            "document_version_id": str(metadata["document_version_id"]),
            "index_generation_id": str(metadata.get("index_generation_id", "")),
            "acl_tokens": acl_tokens,
            "published": False,
            "metadata": metadata,
        }

    def _flush_sync(self) -> None:
        """等待实体进入可查询状态，保证发布后立即检索的一致性。"""
        self._client_or_raise().flush(collection_name=self.collection)

    async def start(self) -> None:
        """创建 Milvus 集合并建立向量索引。"""
        await asyncio.to_thread(self._start_sync)

    async def stage(self, documents: Sequence[IndexDocument]) -> None:
        """以 published=false upsert 待发布实体，保持旧版本仍可检索。"""
        if not documents:
            return
        rows = [self._row(document) for document in documents]
        await asyncio.to_thread(
            self._client_or_raise().upsert,
            collection_name=self.collection,
            data=rows,
        )
        await asyncio.to_thread(self._flush_sync)

    def _query_all_sync(self, expression: str) -> list[dict[str, Any]]:
        """查询完整实体，供发布状态的读改写使用。"""
        rows = self._client_or_raise().query(
            collection_name=self.collection,
            filter=expression,
            output_fields=["*"],
        )
        return [dict(row) for row in rows]

    def _set_published_sync(self, expression: str, value: bool) -> int:
        """按过滤条件读改写完整实体，避免依赖特定版本的部分更新语法。"""
        rows = self._query_all_sync(expression)
        for row in rows:
            row["published"] = value
        if rows:
            self._client_or_raise().upsert(
                collection_name=self.collection,
                data=rows,
            )
            self._flush_sync()
        return len(rows)

    async def publish(self, document_version_id: str) -> None:
        """先隐藏同文档旧版本，再发布目标版本。"""
        version_expression = f"document_version_id == {self._quote(document_version_id)}"
        rows = await asyncio.to_thread(self._query_all_sync, version_expression)
        if not rows:
            raise RuntimeError("cannot publish an empty staged document version")
        document_id = str(rows[0]["document_id"])
        await asyncio.to_thread(
            self._set_published_sync,
            f"document_id == {self._quote(document_id)}",
            False,
        )
        await asyncio.to_thread(self._set_published_sync, version_expression, True)

    async def publish_generation(
        self, document_version_id: str, index_generation_id: str
    ) -> None:
        """只发布指定内容版本中的处理代次。"""
        version_expression = (
            f"document_version_id == {self._quote(document_version_id)} "
            f"&& index_generation_id == {self._quote(index_generation_id)}"
        )
        rows = await asyncio.to_thread(self._query_all_sync, version_expression)
        if not rows:
            raise RuntimeError("cannot publish an empty staged index generation")
        document_id = str(rows[0]["document_id"])
        await asyncio.to_thread(
            self._set_published_sync,
            f"document_id == {self._quote(document_id)}",
            False,
        )
        await asyncio.to_thread(self._set_published_sync, version_expression, True)

    async def inspect_generation(
        self, document_id: str, index_generation_id: str
    ) -> dict[str, object]:
        """Count a generation and fingerprint metadata without reading indexed text."""
        expression = (
            f"document_id == {self._quote(document_id)} "
            f"&& index_generation_id == {self._quote(index_generation_id)}"
        )
        rows = await asyncio.to_thread(self._query_all_sync, expression)
        return {
            "count": len(rows),
            "metadata_hash": self._metadata_hash(rows),
            "published_count": sum(bool(row.get("published")) for row in rows),
        }

    async def delete_document(self, document_id: str) -> None:
        """删除 Milvus 中属于指定文档的全部实体。"""
        expression = f"document_id == {self._quote(document_id)}"
        await asyncio.to_thread(
            self._client_or_raise().delete,
            collection_name=self.collection,
            filter=expression,
        )
        await asyncio.to_thread(self._flush_sync)

    async def delete_generation(self, document_id: str, index_generation_id: str) -> None:
        """删除 Milvus 中指定非活动处理代次的实体。"""
        expression = (
            f"document_id == {self._quote(document_id)} "
            f"&& index_generation_id == {self._quote(index_generation_id)}"
        )
        await asyncio.to_thread(
            self._client_or_raise().delete,
            collection_name=self.collection,
            filter=expression,
        )
        await asyncio.to_thread(self._flush_sync)

    async def retrieve(
        self, query: str, scope: AuthorizationScope, top_k: int
    ) -> list[RetrievalCandidate]:
        """执行带租户、知识空间、ACL 和发布状态过滤的 COSINE 向量搜索。"""
        self._validate_scope(scope)
        if not scope.principal_tokens:
            return []
        vector = (await self.embedding.embed([query]))[0]
        expression = self._scope_expression(
            scope,
            published=True,
            document_ids=scope.document_ids,
        )
        hits = await asyncio.to_thread(
            self._client_or_raise().search,
            collection_name=self.collection,
            data=[list(vector)],
            anns_field="vector",
            filter=expression,
            limit=top_k,
            output_fields=["chunk_id", "text", "metadata"],
            search_params={"metric_type": "COSINE", "params": {}},
        )
        candidates: list[RetrievalCandidate] = []
        for hit in hits[0] if hits else []:
            entity = hit.get("entity", hit)
            metadata = entity.get("metadata") or {}
            candidates.append(
                RetrievalCandidate(
                    str(entity["chunk_id"]),
                    str(entity["text"]),
                    float(hit.get("distance", hit.get("score", 0.0))),
                    dict(metadata),
                    self.source_name,
                )
            )
        return candidates

    async def health(self) -> bool:
        """检查 Milvus 集合是否可访问。"""
        try:
            return bool(
                await asyncio.to_thread(
                    self._client_or_raise().has_collection,
                    collection_name=self.collection,
                )
            )
        except Exception:
            return False

    async def close(self) -> None:
        """关闭 Milvus 客户端。"""
        client = self._client
        self._client = None
        if client is not None:
            closer = getattr(client, "close", None)
            if closer is not None:
                await asyncio.to_thread(closer)


class OpenSearchLexicalSearch(_RemoteSearchBase):
    """OpenSearch 词法检索适配器，查询同时约束租户和主体 ACL。"""

    source_name = "lexical"

    def __init__(self, url: str, index: str) -> None:
        """创建 OpenSearch HTTP 客户端并保存索引名称。"""
        super().__init__()
        self.url = url.rstrip("/")
        self.index = index
        self._client = httpx.AsyncClient(base_url=self.url, timeout=20.0)

    async def start(self) -> None:
        """创建带文本和 ACL 字段映射的 OpenSearch 索引。"""
        response = await self._client.put(
            f"/{self.index}",
            json={
                "mappings": {
                    "properties": {
                        "text": {"type": "text"},
                        "tenant_id": {"type": "keyword"},
                        "knowledge_space_id": {"type": "keyword"},
                        "document_id": {"type": "keyword"},
                        "document_version_id": {"type": "keyword"},
                        "index_generation_id": {"type": "keyword"},
                        "acl_tokens": {"type": "keyword"},
                        "published": {"type": "boolean"},
                    }
                }
            },
        )
        if response.status_code not in {200, 201, 400}:
            response.raise_for_status()
        if response.status_code == 400 and "resource_already_exists_exception" not in response.text:
            response.raise_for_status()

    async def stage(self, documents: Sequence[IndexDocument]) -> None:
        """用 bulk API 写入未发布的词法文档。"""
        if not documents:
            return
        lines: list[str] = []
        import json

        for document in documents:
            lines.append(json.dumps({"index": {"_index": self.index, "_id": document.chunk_id}}))
            lines.append(
                json.dumps(
                    {**document.metadata, "text": document.text, "published": False},
                    separators=(",", ":"),
                )
            )
        response = await self._client.post(
            "/_bulk",
            content=("\n".join(lines) + "\n").encode(),
            headers={"Content-Type": "application/x-ndjson"},
            params={"refresh": "true"},
        )
        response.raise_for_status()
        if response.json().get("errors"):
            raise RuntimeError("OpenSearch bulk staging failed")

    async def _update_published(self, query: dict[str, Any], value: bool) -> None:
        """通过 update_by_query 批量更新发布标记。"""
        response = await self._client.post(
            f"/{self.index}/_update_by_query",
            params={"refresh": "true", "conflicts": "proceed"},
            json={
                "query": query,
                "script": {
                    "source": "ctx._source.published = params.value",
                    "params": {"value": value},
                },
            },
        )
        response.raise_for_status()

    async def publish(self, document_version_id: str) -> None:
        """定位版本所属文档，撤销旧版本后发布新版本。"""
        lookup = await self._client.post(
            f"/{self.index}/_search",
            json={
                "size": 1,
                "query": {"term": {"document_version_id": document_version_id}},
                "_source": ["document_id"],
            },
        )
        lookup.raise_for_status()
        hits = lookup.json().get("hits", {}).get("hits", [])
        if not hits:
            raise RuntimeError("cannot publish an empty staged document version")
        document_id = str(hits[0]["_source"]["document_id"])
        await self._update_published({"term": {"document_id": document_id}}, False)
        await self._update_published(
            {"term": {"document_version_id": document_version_id}}, True
        )

    async def publish_generation(
        self, document_version_id: str, index_generation_id: str
    ) -> None:
        """只发布指定内容版本中的处理代次。"""
        lookup = await self._client.post(
            f"/{self.index}/_search",
            json={
                "size": 1,
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"document_version_id": document_version_id}},
                            {"term": {"index_generation_id": index_generation_id}},
                        ]
                    }
                },
                "_source": ["document_id"],
            },
        )
        lookup.raise_for_status()
        hits = lookup.json().get("hits", {}).get("hits", [])
        if not hits:
            raise RuntimeError("cannot publish an empty staged index generation")
        document_id = str(hits[0]["_source"]["document_id"])
        await self._update_published({"term": {"document_id": document_id}}, False)
        await self._update_published(
            {
                "bool": {
                    "filter": [
                        {"term": {"document_version_id": document_version_id}},
                        {"term": {"index_generation_id": index_generation_id}},
                    ]
                }
            },
            True,
        )

    async def inspect_generation(
        self, document_id: str, index_generation_id: str
    ) -> dict[str, object]:
        """Count a generation and fingerprint metadata without reading indexed text."""
        response = await self._client.post(
            f"/{self.index}/_search",
            json={
                "size": 10_000,
                "track_total_hits": True,
                "_source": [
                    "tenant_id",
                    "knowledge_space_id",
                    "document_id",
                    "document_version_id",
                    "index_generation_id",
                    "acl_epoch",
                    "acl_tokens",
                    "published",
                ],
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"document_id": document_id}},
                            {"term": {"index_generation_id": index_generation_id}},
                        ]
                    }
                },
            },
        )
        response.raise_for_status()
        payload = response.json()
        hits_payload = payload.get("hits", {})
        hits = hits_payload.get("hits", []) if isinstance(hits_payload, dict) else []
        rows = [
            dict(hit.get("_source") or {})
            for hit in hits
            if isinstance(hit, dict)
        ]
        total_payload = hits_payload.get("total") if isinstance(hits_payload, dict) else None
        if isinstance(total_payload, dict):
            total = int(total_payload.get("value", len(rows)))
        else:
            total = int(total_payload) if isinstance(total_payload, int) else len(rows)
        return {
            "count": total,
            "metadata_hash": self._metadata_hash(rows),
            "published_count": sum(bool(row.get("published")) for row in rows),
        }

    async def delete_document(self, document_id: str) -> None:
        """按文档 ID 删除 OpenSearch 中的全部索引记录。"""
        response = await self._client.post(
            f"/{self.index}/_delete_by_query",
            params={"refresh": "true", "conflicts": "proceed"},
            json={"query": {"term": {"document_id": document_id}}},
        )
        response.raise_for_status()

    async def delete_generation(self, document_id: str, index_generation_id: str) -> None:
        """删除 OpenSearch 中指定非活动处理代次的索引记录。"""
        response = await self._client.post(
            f"/{self.index}/_delete_by_query",
            params={"refresh": "true", "conflicts": "proceed"},
            json={
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"document_id": document_id}},
                            {"term": {"index_generation_id": index_generation_id}},
                        ]
                    }
                }
            },
        )
        response.raise_for_status()

    async def retrieve(
        self, query: str, scope: AuthorizationScope, top_k: int
    ) -> list[RetrievalCandidate]:
        """执行带租户、知识空间、主体和发布状态过滤的词法查询。"""
        self._validate_scope(scope)
        filters: list[dict[str, object]] = [
            {"term": {"tenant_id": scope.tenant_id}},
            {"terms": {"knowledge_space_id": sorted(scope.knowledge_space_ids)}},
            {"terms": {"acl_tokens": sorted(scope.principal_tokens)}},
            {"term": {"published": True}},
        ]
        if scope.document_ids:
            filters.append({"terms": {"document_id": sorted(scope.document_ids)}})
        if scope.index_generation_ids:
            filters.append(
                {"terms": {"index_generation_id": sorted(scope.index_generation_ids)}}
            )
        response = await self._client.post(
            f"/{self.index}/_search",
            json={
                "size": top_k,
                "query": {"bool": {"must": [{"match": {"text": query}}], "filter": filters}},
            },
        )
        response.raise_for_status()
        hits = response.json().get("hits", {}).get("hits", [])
        return [
            RetrievalCandidate(
                str(hit["_id"]),
                str(hit["_source"]["text"]),
                float(hit.get("_score") or 0.0),
                {
                    key: value
                    for key, value in hit["_source"].items()
                    if key not in {"text", "published"}
                },
                self.source_name,
            )
            for hit in hits
        ]

    async def health(self) -> bool:
        """检查 OpenSearch 集群健康端点是否可访问。"""
        try:
            response = await self._client.get("/_cluster/health")
            return response.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        """关闭 OpenSearch HTTP 客户端。"""
        await self._client.aclose()


class TokenOverlapReranker:
    """用查询 token 与候选文本重叠度对混合召回结果进行轻量重排。"""

    capabilities = frozenset({"reranking", "traceable-scores"})

    async def rerank(
        self, query: str, candidates: Sequence[RetrievalCandidate], top_k: int
    ) -> list[RetrievalCandidate]:
        """在原始得分上叠加词重叠奖励，返回前 K 个候选。"""
        query_tokens = _tokens(query)
        rescored = [
            RetrievalCandidate(
                item.chunk_id,
                item.text,
                item.score + len(query_tokens.intersection(_tokens(item.text))) * 0.05,
                {**item.metadata, "pre_rerank_score": item.score},
                item.source,
            )
            for item in candidates
        ]
        return sorted(rescored, key=lambda item: (-item.score, item.chunk_id))[:top_k]
