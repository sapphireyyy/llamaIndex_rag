"""搜索基础设施：实现内存、Qdrant、OpenSearch 检索及 token 重排。"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Sequence
from typing import Any

import httpx

from enterprise_rag.application.ports import (
    IndexDocument,
    RetrievalCandidate,
)
from enterprise_rag.domain.types import AuthorizationScope, stable_hash


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

    async def delete_document(self, document_id: str) -> None:
        """删除文档的活动指针和全部内存分块。"""
        self._active_versions_by_document.pop(document_id, None)
        self._documents = {
            key: value
            for key, value in self._documents.items()
            if value.metadata.get("document_id") != document_id
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
        return bool(
            metadata.get("tenant_id") == scope.tenant_id
            and metadata.get("knowledge_space_id") in scope.knowledge_space_ids
            and metadata.get("document_version_id")
            == self._active_versions_by_document.get(str(metadata.get("document_id")))
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
    """Qdrant/OpenSearch 远程索引共享 ACL 版本校验逻辑。"""

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

    async def _version_document_id(self, document_version_id: str) -> str:
        """从已暂存版本中找到文档 ID，防止发布空版本。"""
        response = await self._client.post(
            f"/collections/{self.collection}/points/scroll",
            json={
                "filter": {"must": [self._match("document_version_id", document_version_id)]},
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

    async def delete_document(self, document_id: str) -> None:
        """删除 Qdrant 中属于指定文档的全部点。"""
        response = await self._client.post(
            f"/collections/{self.collection}/points/delete",
            params={"wait": "true"},
            json={"filter": {"must": [self._match("document_id", document_id)]}},
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

    async def delete_document(self, document_id: str) -> None:
        """按文档 ID 删除 OpenSearch 中的全部索引记录。"""
        response = await self._client.post(
            f"/{self.index}/_delete_by_query",
            params={"refresh": "true", "conflicts": "proceed"},
            json={"query": {"term": {"document_id": document_id}}},
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
