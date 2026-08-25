"""评测应用服务：执行样本评测，并管理质量门禁与人工反馈。"""

from __future__ import annotations

import operator
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from opentelemetry import trace
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.api.errors import AppError
from enterprise_rag.application.audit import AuditEventInput, AuditService
from enterprise_rag.domain.types import (
    ErrorCategory,
    RequestIdentity,
    Role,
    new_id,
    stable_hash,
)
from enterprise_rag.infrastructure.orm import (
    AnswerRecord,
    AssistantVersionRecord,
    CitationRecord,
    EvaluationDatasetRecord,
    EvaluationDatasetVersionRecord,
    EvaluationResultRecord,
    EvaluationRunRecord,
    FeedbackRecord,
    QueryExecutionRecord,
    ReleaseGateOverrideRecord,
    ReleaseGateRecord,
)


@dataclass(frozen=True, slots=True)
class EvaluationObservation:
    """一次评测样本的回答状态、引用范围、耗时和成本观测值。"""

    status: str
    answer: str
    grounded: bool
    cited_document_ids: frozenset[str]
    latency_ms: float
    cost: float | None


EvaluationRunner = Callable[[dict[str, object]], Awaitable[EvaluationObservation]]


def _tokens(text: str) -> set[str]:
    """把文本拆成大小写不敏感的词集合，用于确定性相似度计算。"""
    return set(text.casefold().split())


def evaluate_item(
    item: dict[str, object], observation: EvaluationObservation
) -> tuple[dict[str, float], str, dict[str, object]]:
    """根据期望来源和回答观测计算检索、引用、拒答及访问控制指标。"""
    raw_expected_sources = item.get("expected_source_scope", [])
    expected_sources = frozenset(
        str(value)
        for value in raw_expected_sources
    ) if isinstance(raw_expected_sources, (list, tuple, set, frozenset)) else frozenset()
    cited = observation.cited_document_ids
    overlap = expected_sources.intersection(cited)
    expected_answerable = bool(item.get("expected_answerability", True))
    relevance = len(overlap) / len(cited) if cited else (1.0 if not expected_answerable else 0.0)
    coverage = (
        len(overlap) / len(expected_sources)
        if expected_sources
        else (1.0 if not cited else 0.0)
    )
    reference = str(item.get("reference_answer") or "")
    if reference:
        reference_tokens = _tokens(reference)
        answer_tokens = _tokens(observation.answer)
        answer_relevance = len(reference_tokens.intersection(answer_tokens)) / max(
            len(reference_tokens), 1
        )
    else:
        answer_relevance = 1.0
    refused = observation.status == "refusal"
    refusal_correctness = float(refused != expected_answerable)
    access_control = float(not expected_sources or cited.issubset(expected_sources))
    metrics = {
        "retrieval_relevance": relevance,
        "retrieval_coverage": coverage,
        "groundedness": float(observation.grounded or refused),
        "citation_correctness": relevance,
        "answer_relevance": answer_relevance,
        "refusal_correctness": refusal_correctness,
        "latency_ms": observation.latency_ms,
        "access_control": access_control,
    }
    passed = (
        access_control == 1.0
        and refusal_correctness == 1.0
        and (not expected_answerable or relevance > 0)
    )
    return (
        metrics,
        "passed" if passed else "failed",
        {
            "expected_sources": sorted(expected_sources),
            "cited_sources": sorted(cited),
            "cost": observation.cost,
        },
    )


class EvaluationService:
    """负责评测数据、运行结果、质量门禁和反馈的持久化。"""

    def __init__(self, session: AsyncSession) -> None:
        """保存评测事务会话，评测结果由调用方统一提交。"""
        self.session = session

    @staticmethod
    def _require_quality(identity: RequestIdentity) -> None:
        """限制评测和质量门禁操作只能由质量角色或管理员执行。"""
        if not identity.roles.intersection({Role.QUALITY, Role.ADMINISTRATOR}):
            raise AppError(ErrorCategory.FORBIDDEN, "Operation is not permitted.", 403)

    async def create_dataset_version(
        self,
        identity: RequestIdentity,
        name: str,
        items: list[dict[str, object]],
        dataset_id: str | None = None,
    ) -> tuple[EvaluationDatasetRecord, EvaluationDatasetVersionRecord]:
        """创建或递增评测数据集版本，并记录稳定内容哈希。"""
        self._require_quality(identity)
        dataset = None
        if dataset_id:
            dataset = await self.session.scalar(
                select(EvaluationDatasetRecord).where(
                    EvaluationDatasetRecord.id == dataset_id,
                    EvaluationDatasetRecord.tenant_id == identity.tenant_id,
                )
            )
            if dataset is None:
                raise AppError(ErrorCategory.NOT_FOUND, "Evaluation dataset not found.", 404)
        if dataset is None:
            dataset = EvaluationDatasetRecord(
                id=new_id("dataset"), tenant_id=identity.tenant_id, name=name.strip()
            )
            self.session.add(dataset)
            await self.session.flush()
        version_number = (
            await self.session.scalar(
                select(func.max(EvaluationDatasetVersionRecord.version)).where(
                    EvaluationDatasetVersionRecord.dataset_id == dataset.id
                )
            )
            or 0
        ) + 1
        content_hash = stable_hash(
            "\n".join(sorted(str(sorted(item.items())) for item in items))
        )
        version = EvaluationDatasetVersionRecord(
            id=new_id("dataset_version"),
            tenant_id=identity.tenant_id,
            dataset_id=dataset.id,
            version=version_number,
            state="active",
            items=items,
            content_hash=content_hash,
        )
        self.session.add(version)
        await self.session.flush()
        dataset.active_version_id = version.id
        return dataset, version

    async def run(
        self,
        identity: RequestIdentity,
        dataset_version_id: str,
        assistant_version_id: str,
        runner: EvaluationRunner,
        parameters: dict[str, object] | None = None,
    ) -> EvaluationRunRecord:
        """冻结数据集与助手版本，逐项运行评估器并汇总指标。"""
        self._require_quality(identity)
        dataset_version = await self.session.scalar(
            select(EvaluationDatasetVersionRecord).where(
                EvaluationDatasetVersionRecord.id == dataset_version_id,
                EvaluationDatasetVersionRecord.tenant_id == identity.tenant_id,
                EvaluationDatasetVersionRecord.state == "active",
            )
        )
        assistant_version = await self.session.scalar(
            select(AssistantVersionRecord).where(
                AssistantVersionRecord.id == assistant_version_id,
                AssistantVersionRecord.tenant_id == identity.tenant_id,
                AssistantVersionRecord.state == "active",
            )
        )
        if dataset_version is None or assistant_version is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Evaluation snapshot not found.", 404)
        run = EvaluationRunRecord(
            id=new_id("evaluation_run"),
            tenant_id=identity.tenant_id,
            dataset_version_id=dataset_version.id,
            assistant_version_id=assistant_version.id,
            status="running",
            evaluator_versions={
                "retrieval_relevance": "deterministic-v1",
                "retrieval_coverage": "deterministic-v1",
                "groundedness": "deterministic-v1",
                "citation_correctness": "deterministic-v1",
                "answer_relevance": "token-overlap-v1",
                "refusal_correctness": "deterministic-v1",
                "latency": "wall-clock-v1",
                "access_control": "scope-subset-v1",
            },
            parameters={
                **(parameters or {}),
                "dataset_content_hash": dataset_version.content_hash,
                "assistant_config_hash": assistant_version.config_hash,
            },
        )
        self.session.add(run)
        await self.session.flush()
        totals: defaultdict[str, list[float]] = defaultdict(list)
        for ordinal, item in enumerate(dataset_version.items):
            with trace.get_tracer("enterprise_rag.evaluation").start_as_current_span(
                "evaluation.item"
            ) as span:
                span.set_attribute("evaluation.item_id", str(item.get("id", ordinal)))
                observation = await runner(item)
                metrics, outcome, details = evaluate_item(item, observation)
                span.set_attribute("evaluation.outcome", outcome)
            result = EvaluationResultRecord(
                id=new_id("evaluation_result"),
                run_id=run.id,
                item_id=str(item.get("id", ordinal)),
                metrics=metrics,
                outcome=outcome,
                details=details,
            )
            self.session.add(result)
            for key, value in metrics.items():
                totals[key].append(value)
        run.aggregates = {
            key: sum(values) / len(values) for key, values in totals.items() if values
        }
        run.status = "succeeded"
        await self.session.flush()
        return run

    async def create_gate(
        self,
        identity: RequestIdentity,
        name: str,
        metric: str,
        comparison: str,
        threshold: float,
        mandatory: bool,
    ) -> ReleaseGateRecord:
        """创建一个按指标和比较运算符判断的发布质量门禁。"""
        self._require_quality(identity)
        if comparison not in {">=", "<=", ">", "<"}:
            raise AppError(ErrorCategory.INVALID_REQUEST, "Invalid gate operator.", 422)
        gate = ReleaseGateRecord(
            id=new_id("gate"),
            tenant_id=identity.tenant_id,
            name=name,
            metric=metric,
            operator=comparison,
            threshold=threshold,
            mandatory=mandatory,
        )
        self.session.add(gate)
        await self.session.flush()
        return gate

    async def gate_status(
        self, identity: RequestIdentity, run_id: str
    ) -> dict[str, object]:
        """计算评测运行是否满足全部强制门禁，并返回逐项结果。"""
        self._require_quality(identity)
        run = await self.session.scalar(
            select(EvaluationRunRecord).where(
                EvaluationRunRecord.id == run_id,
                EvaluationRunRecord.tenant_id == identity.tenant_id,
            )
        )
        if run is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Evaluation run not found.", 404)
        gates = list(
            (
                await self.session.scalars(
                    select(ReleaseGateRecord).where(
                        ReleaseGateRecord.tenant_id == identity.tenant_id
                    )
                )
            ).all()
        )
        operations = {">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt}
        items = []
        eligible = True
        for gate in gates:
            actual = run.aggregates.get(gate.metric)
            passed = actual is not None and operations[gate.operator](actual, gate.threshold)
            override = await self.session.scalar(
                select(ReleaseGateOverrideRecord).where(
                    ReleaseGateOverrideRecord.gate_id == gate.id,
                    ReleaseGateOverrideRecord.run_id == run.id,
                )
            )
            effective = passed or override is not None
            if gate.mandatory and not effective:
                eligible = False
            items.append(
                {
                    "gate_id": gate.id,
                    "metric": gate.metric,
                    "actual": actual,
                    "threshold": gate.threshold,
                    "operator": gate.operator,
                    "passed": passed,
                    "overridden": override is not None,
                }
            )
        return {"eligible": eligible, "gates": items}

    async def override_gate(
        self,
        identity: RequestIdentity,
        gate_id: str,
        run_id: str,
        reason: str,
        correlation_id: str,
    ) -> ReleaseGateOverrideRecord:
        """由管理员带理由覆盖指定门禁，并写入可审计记录。"""
        if Role.ADMINISTRATOR not in identity.roles or not reason.strip():
            raise AppError(ErrorCategory.FORBIDDEN, "Operation is not permitted.", 403)
        gate = await self.session.scalar(
            select(ReleaseGateRecord).where(
                ReleaseGateRecord.id == gate_id,
                ReleaseGateRecord.tenant_id == identity.tenant_id,
            )
        )
        run = await self.session.scalar(
            select(EvaluationRunRecord).where(
                EvaluationRunRecord.id == run_id,
                EvaluationRunRecord.tenant_id == identity.tenant_id,
            )
        )
        if gate is None or run is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Release gate not found.", 404)
        record = ReleaseGateOverrideRecord(
            id=new_id("gate_override"),
            tenant_id=identity.tenant_id,
            gate_id=gate.id,
            run_id=run.id,
            principal_id=identity.user_id,
            reason=reason.strip(),
        )
        self.session.add(record)
        await AuditService(self.session).record(
            identity,
            AuditEventInput(
                "release_gate.override",
                "release_gate",
                gate.id,
                "allowed",
                "success",
                correlation_id,
                {"run_id": run.id, "reason": reason.strip()},
            ),
        )
        await self.session.flush()
        return record

    async def feedback(
        self,
        identity: RequestIdentity,
        answer_id: str,
        citation_id: str | None,
        category: str,
        rating: int | None,
        comment: str,
    ) -> FeedbackRecord:
        """校验回答和可选引用归属后保存用户反馈。"""
        answer = await self.session.scalar(
            select(AnswerRecord)
            .join(QueryExecutionRecord, QueryExecutionRecord.id == AnswerRecord.query_id)
            .where(
                AnswerRecord.id == answer_id,
                QueryExecutionRecord.tenant_id == identity.tenant_id,
            )
        )
        if answer is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Answer not found.", 404)
        if citation_id:
            citation = await self.session.scalar(
                select(CitationRecord).where(
                    CitationRecord.id == citation_id,
                    CitationRecord.answer_id == answer_id,
                )
            )
            if citation is None:
                raise AppError(ErrorCategory.NOT_FOUND, "Citation not found.", 404)
        record = FeedbackRecord(
            id=new_id("feedback"),
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            answer_id=answer_id,
            citation_id=citation_id,
            category=category,
            rating=rating,
            comment=comment,
        )
        self.session.add(record)
        await self.session.flush()
        return record
