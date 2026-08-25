"""评测接口：提供数据集、评测运行、质量门禁和反馈操作。"""

from __future__ import annotations

import time
from typing import Any, cast

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_rag.api.auth import Identity
from enterprise_rag.api.chat import _query_service
from enterprise_rag.api.errors import AppError
from enterprise_rag.application.evaluation import (
    EvaluationObservation,
    EvaluationService,
)
from enterprise_rag.domain.types import ErrorCategory, RequestIdentity, Role
from enterprise_rag.infrastructure.orm import (
    AssistantVersionRecord,
    ChunkRecord,
    EvaluationDatasetRecord,
    EvaluationDatasetVersionRecord,
    EvaluationResultRecord,
    EvaluationRunRecord,
    FeedbackRecord,
)
from enterprise_rag.infrastructure.redaction import redact

router = APIRouter(prefix="/api/v1", tags=["evaluation"])


class DatasetCreate(BaseModel):
    """评测数据集及其首个版本的创建参数。"""

    name: str = Field(min_length=1, max_length=255)
    dataset_id: str | None = None
    items: list[dict[str, Any]] = Field(min_length=1)


class RunCreate(BaseModel):
    """评测运行需要冻结的数据集版本和助手版本。"""

    dataset_version_id: str
    assistant_version_id: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class GateCreate(BaseModel):
    """发布质量门禁的指标、运算符、阈值和强制性。"""

    name: str
    metric: str
    operator: str
    threshold: float
    mandatory: bool = True


class GateOverride(BaseModel):
    """门禁覆盖请求，只允许提交运行标识和覆盖理由。"""

    run_id: str
    reason: str = Field(min_length=1, max_length=2_000)


class FeedbackCreate(BaseModel):
    """用户对回答或引用提交的分类、评分和文字反馈。"""

    answer_id: str
    citation_id: str | None = None
    category: str = Field(min_length=1, max_length=80)
    rating: int | None = Field(default=None, ge=1, le=5)
    comment: str = Field(default="", max_length=4_000)


def _sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    """取得评测接口使用的异步数据库会话工厂。"""
    sessions = request.app.state.container.sessions
    if sessions is None:
        raise AppError(ErrorCategory.DEPENDENCY_UNAVAILABLE, "Evaluation unavailable.", 503)
    return cast(async_sessionmaker[AsyncSession], sessions)


@router.get("/evaluation/datasets")
async def list_datasets(request: Request, identity: Identity) -> dict[str, object]:
    """列出当前租户评测数据集及活动版本。"""
    async with _sessions(request)() as session:
        datasets = list(
            (
                await session.scalars(
                    select(EvaluationDatasetRecord).where(
                        EvaluationDatasetRecord.tenant_id == identity.tenant_id
                    )
                )
            ).all()
        )
        return {
            "items": [
                {
                    "id": item.id,
                    "name": item.name,
                    "active_version_id": item.active_version_id,
                }
                for item in datasets
            ]
        }


@router.post("/evaluation/datasets", status_code=201)
async def create_dataset(
    request: Request, payload: DatasetCreate, identity: Identity
) -> dict[str, object]:
    """创建或递增评测数据集版本，并返回内容哈希。"""
    async with _sessions(request)() as session:
        dataset, version = await EvaluationService(session).create_dataset_version(
            identity, payload.name, payload.items, payload.dataset_id
        )
        await session.commit()
        return {
            "id": dataset.id,
            "version_id": version.id,
            "version": version.version,
            "content_hash": version.content_hash,
        }


@router.get("/evaluation/datasets/{dataset_id}/versions")
async def dataset_versions(
    request: Request, dataset_id: str, identity: Identity
) -> dict[str, object]:
    """返回指定评测数据集的版本历史。"""
    async with _sessions(request)() as session:
        records = list(
            (
                await session.scalars(
                    select(EvaluationDatasetVersionRecord)
                    .where(
                        EvaluationDatasetVersionRecord.dataset_id == dataset_id,
                        EvaluationDatasetVersionRecord.tenant_id == identity.tenant_id,
                    )
                    .order_by(EvaluationDatasetVersionRecord.version.desc())
                )
            ).all()
        )
        return {
            "items": [
                {
                    "id": item.id,
                    "version": item.version,
                    "state": item.state,
                    "content_hash": item.content_hash,
                    "item_count": len(item.items),
                }
                for item in records
            ]
        }


@router.post("/evaluation/runs", status_code=201)
async def create_run(
    request: Request, payload: RunCreate, identity: Identity
) -> dict[str, object]:
    """用指定助手版本运行数据集评测并保存聚合指标。"""
    async with _sessions(request)() as session:
        assistant_version = await session.scalar(
            select(AssistantVersionRecord).where(
                AssistantVersionRecord.id == payload.assistant_version_id,
                AssistantVersionRecord.tenant_id == identity.tenant_id,
            )
        )
        if assistant_version is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Assistant version not found.", 404)

        async def runner(item: dict[str, object]) -> EvaluationObservation:
            """把样本访问上下文转换为身份，并执行一次真实问答观测。"""
            access = item.get("access_context", {})
            access_context = access if isinstance(access, dict) else {}
            groups = tuple(str(value) for value in access_context.get("groups", identity.groups))
            roles: set[Role] = set()
            for value in access_context.get("roles", [role.value for role in identity.roles]):
                try:
                    roles.add(Role(str(value)))
                except ValueError:
                    continue
            evaluation_identity = RequestIdentity(
                identity.tenant_id,
                str(access_context.get("user_id", identity.user_id)),
                groups,
                frozenset(roles),
            )
            started = time.perf_counter()
            _, execution, answer, citations = await _query_service(request, session).execute(
                evaluation_identity,
                assistant_version.assistant_id,
                str(item["question"]),
                request.state.correlation_id,
                assistant_version_id=assistant_version.id,
            )
            document_ids = frozenset(
                str(value)
                for value in (
                    await session.scalars(
                        select(ChunkRecord.document_id).where(
                            ChunkRecord.id.in_([citation.chunk_id for citation in citations])
                        )
                    )
                ).all()
            )
            cost_value = execution.usage.get("cost")
            return EvaluationObservation(
                answer.status,
                answer.text,
                answer.grounded,
                document_ids,
                (time.perf_counter() - started) * 1000,
                float(cost_value) if isinstance(cost_value, (int, float)) else None,
            )

        run = await EvaluationService(session).run(
            identity,
            payload.dataset_version_id,
            payload.assistant_version_id,
            runner,
            payload.parameters,
        )
        await session.commit()
        return {"id": run.id, "status": run.status, "aggregates": run.aggregates}


@router.get("/evaluation/runs")
async def list_runs(request: Request, identity: Identity) -> dict[str, object]:
    """列出当前租户最近的评测运行及其汇总状态。"""
    async with _sessions(request)() as session:
        records = list(
            (
                await session.scalars(
                    select(EvaluationRunRecord)
                    .where(EvaluationRunRecord.tenant_id == identity.tenant_id)
                    .order_by(EvaluationRunRecord.created_at.desc())
                )
            ).all()
        )
        return {
            "items": [
                {
                    "id": item.id,
                    "status": item.status,
                    "dataset_version_id": item.dataset_version_id,
                    "assistant_version_id": item.assistant_version_id,
                    "aggregates": item.aggregates,
                }
                for item in records
            ]
        }


@router.get("/evaluation/runs/{run_id}")
async def run_results(
    request: Request, run_id: str, identity: Identity, baseline_run_id: str | None = None
) -> dict[str, object]:
    """返回指定评测运行的逐样本指标和结果详情。"""
    async with _sessions(request)() as session:
        run = await session.scalar(
            select(EvaluationRunRecord).where(
                EvaluationRunRecord.id == run_id,
                EvaluationRunRecord.tenant_id == identity.tenant_id,
            )
        )
        if run is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Evaluation run not found.", 404)
        results = list(
            (
                await session.scalars(
                    select(EvaluationResultRecord).where(
                        EvaluationResultRecord.run_id == run.id
                    )
                )
            ).all()
        )
        baseline = (
            await session.scalar(
                select(EvaluationRunRecord).where(
                    EvaluationRunRecord.id == baseline_run_id,
                    EvaluationRunRecord.tenant_id == identity.tenant_id,
                )
            )
            if baseline_run_id
            else None
        )
        deltas = {
            key: value - baseline.aggregates.get(key, 0.0)
            for key, value in run.aggregates.items()
        } if baseline else {}
        gates = await EvaluationService(session).gate_status(identity, run.id)
        return {
            "id": run.id,
            "status": run.status,
            "aggregates": run.aggregates,
            "baseline_deltas": deltas,
            "gates": gates,
            "items": [
                {
                    "item_id": item.item_id,
                    "metrics": item.metrics,
                    "outcome": item.outcome,
                    "details": item.details,
                }
                for item in results
            ],
        }


@router.post("/evaluation/gates", status_code=201)
async def create_gate(
    request: Request, payload: GateCreate, identity: Identity
) -> dict[str, object]:
    """创建用于发布前质量判断的门禁规则。"""
    async with _sessions(request)() as session:
        gate = await EvaluationService(session).create_gate(
            identity,
            payload.name,
            payload.metric,
            payload.operator,
            payload.threshold,
            payload.mandatory,
        )
        await session.commit()
        return {"id": gate.id}


@router.get("/evaluation/runs/{run_id}/export")
async def export_run(
    request: Request, run_id: str, identity: Identity
) -> dict[str, object]:
    """导出评测运行的脱敏结果，供离线分析使用。"""
    if not identity.roles.intersection({Role.QUALITY, Role.ADMINISTRATOR}):
        raise AppError(ErrorCategory.FORBIDDEN, "Operation is not permitted.", 403)
    async with _sessions(request)() as session:
        run = await session.scalar(
            select(EvaluationRunRecord).where(
                EvaluationRunRecord.id == run_id,
                EvaluationRunRecord.tenant_id == identity.tenant_id,
            )
        )
        if run is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Evaluation run not found.", 404)
        results = list(
            (
                await session.scalars(
                    select(EvaluationResultRecord).where(
                        EvaluationResultRecord.run_id == run.id
                    )
                )
            ).all()
        )
        return cast(
            dict[str, object],
            redact(
                {
                    "run_id": run.id,
                    "dataset_version_id": run.dataset_version_id,
                    "assistant_version_id": run.assistant_version_id,
                    "evaluator_versions": run.evaluator_versions,
                    "parameters": run.parameters,
                    "aggregates": run.aggregates,
                    "items": [
                        {
                            "item_id": item.item_id,
                            "metrics": item.metrics,
                            "outcome": item.outcome,
                            "details": item.details,
                        }
                        for item in results
                    ],
                }
            ),
        )


@router.post("/evaluation/gates/{gate_id}/override", status_code=201)
async def override_gate(
    request: Request, gate_id: str, payload: GateOverride, identity: Identity
) -> dict[str, object]:
    """记录管理员对指定质量门禁的有理由覆盖。"""
    async with _sessions(request)() as session:
        record = await EvaluationService(session).override_gate(
            identity,
            gate_id,
            payload.run_id,
            payload.reason,
            request.state.correlation_id,
        )
        await session.commit()
        return {"id": record.id}


@router.post("/feedback", status_code=201)
async def create_feedback(
    request: Request, payload: FeedbackCreate, identity: Identity
) -> dict[str, object]:
    """校验回答和引用归属后保存用户反馈。"""
    async with _sessions(request)() as session:
        record = await EvaluationService(session).feedback(
            identity,
            payload.answer_id,
            payload.citation_id,
            payload.category,
            payload.rating,
            payload.comment,
        )
        await session.commit()
        return {"id": record.id}


@router.get("/feedback")
async def list_feedback(request: Request, identity: Identity) -> dict[str, object]:
    """列出当前租户的回答反馈记录。"""
    if not identity.roles.intersection({Role.QUALITY, Role.ADMINISTRATOR}):
        raise AppError(ErrorCategory.FORBIDDEN, "Operation is not permitted.", 403)
    async with _sessions(request)() as session:
        records = list(
            (
                await session.scalars(
                    select(FeedbackRecord)
                    .where(FeedbackRecord.tenant_id == identity.tenant_id)
                    .order_by(FeedbackRecord.created_at.desc())
                    .limit(500)
                )
            ).all()
        )
        return {
            "items": [
                {
                    "id": item.id,
                    "answer_id": item.answer_id,
                    "citation_id": item.citation_id,
                    "category": item.category,
                    "rating": item.rating,
                    "comment": item.comment,
                    "created_at": item.created_at,
                }
                for item in records
            ]
        }
