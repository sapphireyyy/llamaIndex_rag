"""检索应用服务：应用 ACL 范围，执行混合召回、融合、重排和冲突检测。"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from opentelemetry import trace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.api.errors import AppError
from enterprise_rag.application.ports import RerankerAdapter, RetrievalCandidate, SearchAdapter
from enterprise_rag.application.security import DatabaseAuthorizationService
from enterprise_rag.domain.types import ErrorCategory, RequestIdentity, new_id, utc_now
from enterprise_rag.infrastructure.orm import (
    ChunkRecord,
    DocumentRecord,
    TelemetryEventRecord,
)


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
                    )
                )
            ).all()
            active_rows = {chunk.id: (chunk, document) for chunk, document in rows}

        rejected = 0
        validated_sets: list[list[RetrievalCandidate]] = []
        for result_set in successful:
            validated: list[RetrievalCandidate] = []
            for candidate in result_set:
                metadata = candidate.metadata
                row = active_rows.get(candidate.chunk_id)
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
                )
                if allowed:
                    validated.append(candidate)
                else:
                    rejected += 1
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
            },
        )
