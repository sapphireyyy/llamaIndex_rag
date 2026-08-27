"""Durable ingestion task leases and conditional worker state transitions."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.domain.types import utc_now
from enterprise_rag.infrastructure.orm import IngestionJobRecord
from enterprise_rag.infrastructure.queue import OutboxService


class TaskLeaseService:
    """实现任务认领、心跳、过期恢复和持久化重试调度。"""

    def __init__(
        self,
        session: AsyncSession,
        worker_id: str,
        *,
        lease_seconds: int = 120,
        retry_backoff_seconds: int = 2,
    ) -> None:
        """绑定会话和 worker 身份，所有状态变化都在调用方事务中完成。"""
        self.session = session
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.retry_backoff_seconds = retry_backoff_seconds

    async def claim(
        self,
        job_id: str,
        tenant_id: str,
        *,
        now: datetime | None = None,
    ) -> IngestionJobRecord | None:
        """条件认领一个未完成任务，重复 delivery 只会得到一次有效认领。"""
        current = now or utc_now()
        job = await self.session.scalar(
            select(IngestionJobRecord)
            .where(
                IngestionJobRecord.id == job_id,
                IngestionJobRecord.tenant_id == tenant_id,
                IngestionJobRecord.cancelled.is_(False),
                IngestionJobRecord.status.in_(("queued", "running")),
                or_(
                    IngestionJobRecord.next_attempt_at.is_(None),
                    IngestionJobRecord.next_attempt_at <= current,
                ),
                or_(
                    IngestionJobRecord.claimed_by.is_(None),
                    IngestionJobRecord.claimed_by == self.worker_id,
                    IngestionJobRecord.lease_expires_at <= current,
                ),
            )
            .with_for_update(skip_locked=True)
        )
        if job is None:
            return None
        job.claimed_by = self.worker_id
        job.lease_expires_at = current + timedelta(seconds=self.lease_seconds)
        job.heartbeat_at = current
        job.next_attempt_at = None
        job.status = "running"
        await self.session.flush()
        return job

    async def assert_owned(self, job_id: str, tenant_id: str) -> bool:
        """确认长时间处理仍由当前 worker 持有有效租约。"""
        current = utc_now()
        job = await self.session.scalar(
            select(IngestionJobRecord).where(
                IngestionJobRecord.id == job_id,
                IngestionJobRecord.tenant_id == tenant_id,
                IngestionJobRecord.status == "running",
                IngestionJobRecord.claimed_by == self.worker_id,
                IngestionJobRecord.lease_expires_at > current,
                IngestionJobRecord.cancelled.is_(False),
            )
        )
        return job is not None

    async def heartbeat(self, job_id: str, tenant_id: str) -> bool:
        """仅允许当前认领者续租，避免旧 worker 延长他人的任务。"""
        current = utc_now()
        job = await self.session.scalar(
            select(IngestionJobRecord)
            .where(
                IngestionJobRecord.id == job_id,
                IngestionJobRecord.tenant_id == tenant_id,
                IngestionJobRecord.claimed_by == self.worker_id,
                IngestionJobRecord.status == "running",
            )
            .with_for_update()
        )
        if job is None:
            return False
        job.heartbeat_at = current
        job.lease_expires_at = current + timedelta(seconds=self.lease_seconds)
        await self.session.flush()
        return True

    async def recover_expired(
        self,
        *,
        tenant_id: str | None = None,
        now: datetime | None = None,
    ) -> list[str]:
        """把租约过期的运行中任务恢复为 queued，超过上限则进入 failed。"""
        current = now or utc_now()
        conditions = [
            IngestionJobRecord.status == "running",
            IngestionJobRecord.lease_expires_at.is_not(None),
            IngestionJobRecord.lease_expires_at <= current,
        ]
        if tenant_id is not None:
            conditions.append(IngestionJobRecord.tenant_id == tenant_id)
        jobs = list(
            (
                await self.session.scalars(
                    select(IngestionJobRecord)
                    .where(*conditions)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        recovered: list[str] = []
        for job in jobs:
            job.claimed_by = None
            job.lease_expires_at = None
            job.heartbeat_at = None
            if job.cancelled:
                job.status = "cancelled"
                job.stage = "cancelled"
                continue
            if job.attempt_count >= job.max_attempts:
                job.status = "failed"
                job.stage = "retry_exhausted"
                job.error_category = "transient"
                job.error_message = "Worker lease expired after the retry limit."
                continue
            job.status = "queued"
            job.stage = "retry_scheduled"
            job.next_attempt_at = current
            recovered.append(job.id)
        await self.session.flush()
        return recovered

    async def schedule_retry(self, job: IngestionJobRecord) -> None:
        """将 transient 失败写成带退避时间的 outbox 重试事件。"""
        if job.error_category != "transient" or job.attempt_count >= job.max_attempts:
            return
        available_at = utc_now() + timedelta(
            seconds=self.retry_backoff_seconds * (2 ** max(job.attempt_count - 1, 0))
        )
        job.status = "queued"
        job.stage = "retry_scheduled"
        job.claimed_by = None
        job.lease_expires_at = None
        job.heartbeat_at = None
        job.next_attempt_at = available_at
        event = await OutboxService(self.session).add(
            job.tenant_id,
            "ingestion.process",
            job.correlation_id,
            {
                "job_id": job.id,
                "document_id": job.document_id,
                "document_version_id": job.document_version_id,
                "processing_config_version_id": job.processing_config_version_id,
                "processing_snapshot": job.processing_snapshot,
                "authorization_epoch": job.payload.get("authorization_epoch"),
                "resource_version_id": job.document_version_id,
                "policy_version_ids": job.payload.get("policy_version_ids", []),
            },
            priority=job.priority,
            max_attempts=job.max_attempts,
        )
        event.available_at = available_at
        await self.session.flush()
