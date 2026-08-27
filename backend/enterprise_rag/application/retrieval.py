"""检索应用服务：应用 ACL 范围，执行混合召回、融合、重排和冲突检测。"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from opentelemetry import trace
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.api.errors import AppError
from enterprise_rag.application.ports import RerankerAdapter, RetrievalCandidate, SearchAdapter
from enterprise_rag.application.security import DatabaseAuthorizationService
from enterprise_rag.domain.types import (
    ErrorCategory,
    RequestIdentity,
    new_id,
    stable_json_hash,
    utc_now,
)
from enterprise_rag.infrastructure.orm import (
    ChunkRecord,
    DocumentRecord,
    PolicyVersionRecord,
    TelemetryEventRecord,
)

DEFAULT_RETRIEVAL_POLICY_ID = "default-retrieval"
DEFAULT_RETRIEVAL_POLICY_VERSION = 1
DEFAULT_RETRIEVAL_POLICY_CONFIG: dict[str, object] = {
    "dense_top_k": 20,
    "lexical_top_k": 20,
    "fusion_k": 60,
    "rerank_top_k": 12,
    "evidence_top_k": 6,
    "relevance_threshold": 0.01,
    "context_character_budget": 24_000,
    "allow_single_search_degradation": True,
}


def _as_int(value: object) -> int:
    """将策略配置中的有限标量转换为整数并保留错误边界。"""
    if isinstance(value, (bool, int, float, str)):
        return int(value)
    raise ValueError("retrieval policy integer value is invalid")


def _as_float(value: object) -> float:
    """将策略配置中的有限标量转换为浮点数并保留错误边界。"""
    if isinstance(value, (bool, int, float, str)):
        return float(value)
    raise ValueError("retrieval policy float value is invalid")


def normalize_query(question: str) -> str:
    """规范化问题中的空白字符，生成稳定的检索查询。"""
    return " ".join(question.replace("\x00", " ").split()).strip()


def rewrite_follow_up(question: str, prior_user_questions: Sequence[str]) -> str:
    """为简短追问补充最近一次用户问题的上下文。"""
    normalized = normalize_query(question)
    if not prior_user_questions:
        return normalized
    follow_up_markers = ("它", "这个", "该", "that", "this", "it", "继续", "还有")
    if any(marker in normalized.casefold() for marker in follow_up_markers):
        return f"{normalize_query(prior_user_questions[-1])} / follow-up: {normalized}"
    return normalized


@dataclass(frozen=True, slots=True)
class RetrievalPolicy:
    """控制双路召回、融合、重排、证据数量和上下文预算的策略。"""

    version: str = "retrieval-v1"
    dense_top_k: int = 20
    lexical_top_k: int = 20
    fusion_k: int = 60
    rerank_top_k: int = 12
    evidence_top_k: int = 6
    relevance_threshold: float = 0.01
    context_character_budget: int = 24_000
    allow_single_search_degradation: bool = True
    policy_id: str = DEFAULT_RETRIEVAL_POLICY_ID
    record_version: int = DEFAULT_RETRIEVAL_POLICY_VERSION
    content_hash: str = ""
    inherited_from: str | None = "versioned-default"

    def __post_init__(self) -> None:
        """为直接构造的兼容对象补齐可审计的有效哈希。"""
        if not self.content_hash:
            object.__setattr__(self, "content_hash", stable_json_hash(self.values()))
        if self.dense_top_k <= 0 or self.lexical_top_k <= 0:
            raise ValueError("retrieval top-k values must be positive")
        if self.fusion_k <= 0 or self.rerank_top_k <= 0 or self.evidence_top_k <= 0:
            raise ValueError("retrieval ranking values must be positive")
        if self.context_character_budget <= 0:
            raise ValueError("retrieval context budget must be positive")
        if self.relevance_threshold < 0:
            raise ValueError("retrieval relevance threshold must not be negative")

    def values(self) -> dict[str, object]:
        """返回所有检索阶段实际使用的参数，不包含身份元数据。"""
        return {
            "dense_top_k": self.dense_top_k,
            "lexical_top_k": self.lexical_top_k,
            "fusion_k": self.fusion_k,
            "rerank_top_k": self.rerank_top_k,
            "evidence_top_k": self.evidence_top_k,
            "relevance_threshold": self.relevance_threshold,
            "context_character_budget": self.context_character_budget,
            "allow_single_search_degradation": self.allow_single_search_degradation,
        }

    def snapshot(self) -> dict[str, object]:
        """返回查询记录中冻结的策略 ID、版本、哈希、来源和最终值。"""
        return {
            "schema_version": 1,
            "policy_id": self.policy_id,
            "version": self.record_version,
            "version_label": self.version,
            "content_hash": self.content_hash,
            "effective_hash": stable_json_hash(self.values()),
            "inherited_from": self.inherited_from,
            "values": self.values(),
        }

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, object],
        *,
        policy_id: str,
        record_version: int,
        content_hash: str,
    ) -> RetrievalPolicy:
        """从持久化配置解析完整策略，并为缺失字段记录版本化继承来源。"""
        values = dict(DEFAULT_RETRIEVAL_POLICY_CONFIG)
        if "top_k" in config:
            values["dense_top_k"] = config["top_k"]
            values["lexical_top_k"] = config["top_k"]
        for key in values:
            if key in config:
                values[key] = config[key]
        inherited = (
            "versioned-default"
            if any(key not in config for key in DEFAULT_RETRIEVAL_POLICY_CONFIG)
            else None
        )
        try:
            dense_top_k = _as_int(values["dense_top_k"])
            lexical_top_k = _as_int(values["lexical_top_k"])
            fusion_k = _as_int(values["fusion_k"])
            rerank_top_k = _as_int(values["rerank_top_k"])
            evidence_top_k = _as_int(values["evidence_top_k"])
            relevance_threshold = _as_float(values["relevance_threshold"])
            context_character_budget = _as_int(values["context_character_budget"])
            return cls(
                version=f"{policy_id}:v{record_version}",
                dense_top_k=dense_top_k,
                lexical_top_k=lexical_top_k,
                fusion_k=fusion_k,
                rerank_top_k=rerank_top_k,
                evidence_top_k=evidence_top_k,
                relevance_threshold=relevance_threshold,
                context_character_budget=context_character_budget,
                allow_single_search_degradation=bool(
                    values["allow_single_search_degradation"]
                ),
                policy_id=policy_id,
                record_version=record_version,
                content_hash=content_hash,
                inherited_from=inherited,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("retrieval policy contains invalid values") from exc

    @classmethod
    def default(cls) -> RetrievalPolicy:
        """构造与持久化 default-retrieval/v1 相同的兼容策略。"""
        return cls.from_config(
            DEFAULT_RETRIEVAL_POLICY_CONFIG,
            policy_id=DEFAULT_RETRIEVAL_POLICY_ID,
            record_version=DEFAULT_RETRIEVAL_POLICY_VERSION,
            content_hash=stable_json_hash(DEFAULT_RETRIEVAL_POLICY_CONFIG),
        )


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """保留检索候选、最终证据、冲突标记和可观测追踪信息。"""

    query: str
    candidates: tuple[RetrievalCandidate, ...]
    evidence: tuple[RetrievalCandidate, ...]
    conflict: bool
    degraded: bool
    trace: dict[str, object]


def reciprocal_rank_fusion(
    result_sets: Sequence[Sequence[RetrievalCandidate]], fusion_k: int
) -> list[RetrievalCandidate]:
    """使用倒数排名融合合并多路检索候选。"""
    scores: defaultdict[str, float] = defaultdict(float)
    by_id: dict[str, RetrievalCandidate] = {}
    sources: defaultdict[str, set[str]] = defaultdict(set)
    for result_set in result_sets:
        for rank, candidate in enumerate(result_set, 1):
            scores[candidate.chunk_id] += 1.0 / (fusion_k + rank)
            by_id[candidate.chunk_id] = candidate
            sources[candidate.chunk_id].add(candidate.source)
    return [
        RetrievalCandidate(
            by_id[chunk_id].chunk_id,
            by_id[chunk_id].text,
            score,
            {**by_id[chunk_id].metadata, "retrieval_sources": sorted(sources[chunk_id])},
            "fusion",
        )
        for chunk_id, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    ]


def _conflicting(candidates: Sequence[RetrievalCandidate]) -> bool:
    """识别来自相同主题但结论不一致的候选证据。"""
    if len({item.metadata.get("document_id") for item in candidates}) < 2:
        return False
    number_sets = [set(re.findall(r"\b\d+(?:\.\d+)?%?\b", item.text)) for item in candidates]
    non_empty_numbers = [numbers for numbers in number_sets if numbers]
    if len(non_empty_numbers) >= 2 and len({tuple(sorted(item)) for item in non_empty_numbers}) > 1:
        return True
    negation = [
        bool(re.search(r"\b(?:not|never|no)\b|不|禁止|不得", item.text.casefold()))
        for item in candidates
    ]
    return any(negation) and not all(negation)


class RetrievalService:
    """负责授权范围内的并行召回、融合、重排和证据裁剪。"""

    def __init__(
        self,
        session: AsyncSession,
        authorization: DatabaseAuthorizationService,
        dense: SearchAdapter,
        lexical: SearchAdapter,
        reranker: RerankerAdapter,
    ) -> None:
        """初始化权限服务、双检索器和可选重排器。"""
        self.session = session
        self.authorization = authorization
        self.dense = dense
        self.lexical = lexical
        self.reranker = reranker

    async def retrieve(
        self,
        identity: RequestIdentity,
        knowledge_space_ids: Sequence[str],
        question: str,
        prior_user_questions: Sequence[str],
        policy: RetrievalPolicy,
    ) -> RetrievalResult:
        """在授权范围内执行混合检索，并保留可审计的检索轨迹。"""
        tracer = trace.get_tracer("enterprise_rag.retrieval")
        with tracer.start_as_current_span("retrieval.authorized_hybrid") as span:
            span.set_attribute("retrieval.space_count", len(knowledge_space_ids))
            result = await self._retrieve(
                identity, knowledge_space_ids, question, prior_user_questions, policy
            )
            span.set_attribute("retrieval.evidence_count", len(result.evidence))
            span.set_attribute("retrieval.degraded", result.degraded)
            return result

    async def _retrieve(
        self,
        identity: RequestIdentity,
        knowledge_space_ids: Sequence[str],
        question: str,
        prior_user_questions: Sequence[str],
        policy: RetrievalPolicy,
    ) -> RetrievalResult:
        """并发执行向量和词法检索，融合后按需重排。"""
        query = rewrite_follow_up(question, prior_user_questions)
        if not query:
            raise AppError(ErrorCategory.INVALID_REQUEST, "Question is required.", 422)
        scope = await self.authorization.retrieval_scope(identity, knowledge_space_ids)
        results = await asyncio.gather(
            self.dense.retrieve(query, scope, policy.dense_top_k),
            self.lexical.retrieve(query, scope, policy.lexical_top_k),
            return_exceptions=True,
        )
        successful = [item for item in results if isinstance(item, list)]
        degraded = len(successful) != 2
        if not successful or (degraded and not policy.allow_single_search_degradation):
            raise AppError(ErrorCategory.DEPENDENCY_UNAVAILABLE, "Retrieval unavailable.", 503)
        candidate_ids = {
            candidate.chunk_id for result_set in successful for candidate in result_set
        }
        active_rows = {}
        if candidate_ids:
            rows = (
                await self.session.execute(
                    select(ChunkRecord, DocumentRecord)
                    .join(DocumentRecord, DocumentRecord.id == ChunkRecord.document_id)
                    .where(
                        ChunkRecord.id.in_(candidate_ids),
                        ChunkRecord.tenant_id == identity.tenant_id,
                        DocumentRecord.tenant_id == identity.tenant_id,
                        ChunkRecord.active.is_(True),
                        DocumentRecord.state == "active",
                        ChunkRecord.document_version_id == DocumentRecord.active_version_id,
                        or_(
                            DocumentRecord.active_generation_id.is_(None),
                            ChunkRecord.index_generation_id
                            == DocumentRecord.active_generation_id,
                        ),
                    )
                )
            ).all()
            active_rows = {chunk.id: (chunk, document) for chunk, document in rows}

        rejected = 0
        rejection_reasons: defaultdict[str, int] = defaultdict(int)
        validated_sets: list[list[RetrievalCandidate]] = []
        for result_set in successful:
            validated: list[RetrievalCandidate] = []
            for candidate in result_set:
                metadata = candidate.metadata
                row = active_rows.get(candidate.chunk_id)
                reason = ""
                allowed = bool(
                    row
                    and metadata.get("tenant_id") == identity.tenant_id
                    and metadata.get("knowledge_space_id") in scope.knowledge_space_ids
                    and metadata.get("document_id") == row[0].document_id
                    and metadata.get("document_version_id") == row[0].document_version_id
                    and set(metadata.get("acl_tokens", [])).intersection(
                        scope.principal_tokens
                    )
                    and set(row[0].acl_tokens).intersection(scope.principal_tokens)
                    and (
                        not scope.document_ids
                        or row[0].document_id in scope.document_ids
                    )
                    and (
                        row[0].index_generation_id is None
                        or str(metadata.get("index_generation_id"))
                        == str(row[0].index_generation_id)
                    )
                    and (
                        not scope.index_generation_ids
                        or str(row[0].index_generation_id) in scope.index_generation_ids
                    )
                )
                if allowed:
                    validated.append(candidate)
                else:
                    rejected += 1
                    if row is None:
                        reason = "missing_or_inactive_authority_row"
                    elif metadata.get("tenant_id") != identity.tenant_id:
                        reason = "tenant_mismatch"
                    elif metadata.get("document_version_id") != row[0].document_version_id:
                        reason = "stale_document_version"
                    elif (
                        row[0].index_generation_id is not None
                        and str(metadata.get("index_generation_id"))
                        != str(row[0].index_generation_id)
                    ):
                        reason = "stale_index_generation"
                    elif (
                        scope.index_generation_ids
                        and str(row[0].index_generation_id)
                        not in scope.index_generation_ids
                    ):
                        reason = "unauthorized_index_generation"
                    elif not set(metadata.get("acl_tokens", [])).intersection(
                        scope.principal_tokens
                    ):
                        reason = "candidate_acl_mismatch"
                    else:
                        reason = "authority_or_scope_mismatch"
                    rejection_reasons[reason] += 1
            validated_sets.append(validated)
        if rejected:
            span_context = trace.get_current_span().get_span_context()
            self.session.add(
                TelemetryEventRecord(
                    id=new_id("telemetry"),
                    tenant_id=identity.tenant_id,
                    correlation_id=identity.token_id or new_id("retrieval"),
                    trace_id=f"{span_context.trace_id:032x}",
                    span_id=f"{span_context.span_id:016x}",
                    kind="security",
                    stage="retrieval.foreign_candidate_rejected",
                    status="rejected",
                    attributes={"candidate_count": rejected},
                    content={},
                    created_at=utc_now(),
                )
            )
        fused = reciprocal_rank_fusion(validated_sets, policy.fusion_k)
        bounded = fused[: max(policy.rerank_top_k * 4, policy.rerank_top_k)]
        try:
            reranked = await self.reranker.rerank(query, bounded, policy.rerank_top_k)
        except Exception as exc:
            if not policy.allow_single_search_degradation:
                raise AppError(
                    ErrorCategory.DEPENDENCY_UNAVAILABLE, "Reranking unavailable.", 503
                ) from exc
            degraded = True
            reranked = bounded[: policy.rerank_top_k]
        active_ids = set(active_rows)
        evidence: list[RetrievalCandidate] = []
        used_characters = 0
        for candidate in reranked:
            if candidate.chunk_id not in active_ids or candidate.score < policy.relevance_threshold:
                continue
            if used_characters + len(candidate.text) > policy.context_character_budget:
                continue
            evidence.append(candidate)
            used_characters += len(candidate.text)
            if len(evidence) >= policy.evidence_top_k:
                break
        return RetrievalResult(
            query,
            tuple(reranked),
            tuple(evidence),
            _conflicting(evidence),
            degraded,
            {
                "policy_version": policy.version,
                "dense_count": len(results[0]) if isinstance(results[0], list) else 0,
                "lexical_count": len(results[1]) if isinstance(results[1], list) else 0,
                "fused_count": len(fused),
                "evidence_count": len(evidence),
                "scope_acl_epoch": scope.acl_epoch,
                "foreign_candidates_rejected": rejected,
                "stale_candidate_reasons": dict(rejection_reasons),
                "policy_snapshot": policy.snapshot(),
            },
        )

    async def resolve_policy(
        self,
        tenant_id: str,
        policy_id: str | None,
    ) -> RetrievalPolicy:
        """解析租户活动策略；缺失或跨租户引用不会静默回退。"""
        statement = select(PolicyVersionRecord).where(
            PolicyVersionRecord.tenant_id == tenant_id,
            PolicyVersionRecord.kind == "retrieval",
            PolicyVersionRecord.state == "active",
        )
        if policy_id:
            statement = statement.where(
                PolicyVersionRecord.id == policy_id
            )
        else:
            statement = statement.where(
                PolicyVersionRecord.policy_id == DEFAULT_RETRIEVAL_POLICY_ID
            ).order_by(PolicyVersionRecord.version.desc())
        record = await self.session.scalar(statement)
        if record is None:
            raise AppError(
                ErrorCategory.INVALID_REQUEST,
                "Active retrieval policy is unavailable.",
                422,
            )
        try:
            return RetrievalPolicy.from_config(
                record.config,
                policy_id=record.policy_id,
                record_version=record.version,
                content_hash=record.content_hash,
            )
        except ValueError as exc:
            raise AppError(
                ErrorCategory.INVALID_REQUEST,
                "Active retrieval policy is invalid.",
                422,
            ) from exc
