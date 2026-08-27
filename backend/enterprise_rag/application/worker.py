"""Structured-concurrency worker for outbox dispatch and ingestion delivery."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import TYPE_CHECKING, cast

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_rag.application.generation_cleanup import IndexGenerationCleanupService
from enterprise_rag.application.ingestion import IngestionService
from enterprise_rag.application.orphans import OrphanObjectSweeper
from enterprise_rag.application.ports import (
    EmbeddingAdapter,
    ObjectStore,
    OCRAdapter,
    ParserAdapter,
    QueueDelivery,
    SearchAdapter,
)
from enterprise_rag.application.task_leases import TaskLeaseService
from enterprise_rag.domain.types import JobMessage, new_id
from enterprise_rag.infrastructure.database import set_transaction_tenant
from enterprise_rag.infrastructure.orm import DocumentRecord, IngestionJobRecord
from enterprise_rag.infrastructure.queue import OutboxDispatcher, OutboxService

if TYPE_CHECKING:
    from enterprise_rag.infrastructure.container import Container

log = structlog.get_logger()


class IngestionWorker:
    """消费持久化任务；ACK 仅发生在对应事务提交之后。"""

    SUPPORTED_KINDS = frozenset(
        {
            "ingestion.process",
            "document.cleanup",
            "object.orphan_sweep",
            "index-generation.cleanup",
        }
    )

    def __init__(self, container: Container, *, worker_id: str | None = None) -> None:
        """从运行时容器读取基础设施并建立 worker 身份。"""
        self.container = container
        self.worker_id = worker_id or new_id("worker")
        settings = container.settings
        self.lease_seconds = settings.worker_lease_seconds
        self.heartbeat_seconds = settings.worker_heartbeat_seconds
        self.retry_backoff_seconds = settings.job_retry_backoff_seconds

    def _sessions(self) -> async_sessionmaker[AsyncSession]:
        """返回已启动的数据库会话工厂。"""
        sessions = self.container.sessions
        if sessions is None:
            raise RuntimeError("worker database sessions are not initialized")
        return sessions

    def _tenant_ids(self) -> tuple[str, ...]:
        """Return the explicit tenant set used to scope background database polling."""
        return tuple(
            dict.fromkeys(
                item.strip()
                for item in self.container.settings.worker_tenant_ids
                if item.strip()
            )
        )

    def _require_tenant_polling_scope(self) -> tuple[str, ...]:
        """Fail closed instead of issuing an unscoped PostgreSQL outbox query."""
        tenant_ids = self._tenant_ids()
        engine = self.container.engine
        if engine is not None and engine.dialect.name == "postgresql" and not tenant_ids:
            raise RuntimeError(
                "PostgreSQL workers require RAG_WORKER_TENANT_IDS for scoped polling"
            )
        return tenant_ids

    def _service(
        self,
        session: AsyncSession,
        snapshot: dict[str, object],
        lease_check: Callable[[], Awaitable[bool]] | None = None,
    ) -> IngestionService:
        """为单个任务组装完全复用容器 Provider 的摄取服务。"""
        if not all(
            (
                self.container.object_store,
                self.container.parser,
                self.container.embedding,
                self.container.dense_index,
                self.container.lexical_index,
            )
        ):
            raise RuntimeError("worker ingestion dependencies are not initialized")
        return IngestionService(
            session,
            cast(ObjectStore, self.container.object_store),
            cast(ParserAdapter, self.container.parser),
            cast(EmbeddingAdapter, self.container.embedding),
            cast(SearchAdapter, self.container.dense_index),
            cast(SearchAdapter, self.container.lexical_index),
            set(self.container.settings.allowed_extensions),
            self.container.settings.max_upload_bytes,
            processing_snapshot=snapshot,
            ocr_adapter=cast(OCRAdapter, self.container.ocr),
            lease_check=lease_check,
        )

    @staticmethod
    async def _enqueue_job(session: AsyncSession, job: IngestionJobRecord) -> None:
        """为租约恢复的任务补写一次可重试的 outbox 事件。"""
        await OutboxService(session).add(
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

    async def handle_delivery(self, delivery: QueueDelivery) -> None:
        """处理一条 delivery，并按事务结果显式 ACK/NACK。"""
        message = delivery.message
        if message.kind not in self.SUPPORTED_KINDS:
            log.warning("worker_unknown_message_kind", kind=message.kind, message_id=message.id)
            await delivery.ack()
            return
        if message.payload.get("tenant_id") != message.tenant_id:
            log.error("worker_tenant_mismatch", message_id=message.id)
            await delivery.ack()
            return
        try:
            if message.kind == "ingestion.process":
                await self._handle_ingestion(delivery, message)
            elif message.kind == "document.cleanup":
                await self._handle_cleanup(delivery, message)
            elif message.kind == "index-generation.cleanup":
                await self._handle_generation_cleanup(delivery, message)
            else:
                await self._handle_orphan_sweep(delivery, message)
        except asyncio.CancelledError:
            with suppress(Exception):
                await delivery.nack(requeue=True)
            raise
        except Exception:
            log.exception("worker_delivery_failed", message_id=message.id, kind=message.kind)
            with suppress(Exception):
                await delivery.nack(requeue=True)

    async def _handle_ingestion(self, delivery: QueueDelivery, message: JobMessage) -> None:
        """认领、处理并提交一个摄取任务。"""
        job_id = str(message.payload.get("job_id") or "")
        if not job_id:
            await delivery.ack()
            return
        async with self._sessions()() as session:
            await set_transaction_tenant(session, message.tenant_id)
            lease = TaskLeaseService(
                session,
                self.worker_id,
                lease_seconds=self.lease_seconds,
                retry_backoff_seconds=self.retry_backoff_seconds,
            )
            job = await lease.claim(job_id, message.tenant_id)
            if job is None:
                await session.commit()
                await delivery.ack()
                return
            await session.commit()
            snapshot = dict(
                job.processing_snapshot
                or message.payload.get("processing_snapshot", {})
            )
            heartbeat_stop = asyncio.Event()
            heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(message.tenant_id, job.id, heartbeat_stop),
                name=f"worker-heartbeat-{job.id}",
            )
            try:
                async def lease_check() -> bool:
                    return await lease.assert_owned(job.id, message.tenant_id)

                service = self._service(session, snapshot, lease_check)
                processed = await service.process(job.id)
                if processed.status == "failed":
                    await lease.schedule_retry(processed)
                await session.commit()
            finally:
                heartbeat_stop.set()
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
        await delivery.ack()

    async def _heartbeat_loop(self, tenant_id: str, job_id: str, stop: asyncio.Event) -> None:
        """使用独立会话续租，避免长时间解析持有任务行锁。"""
        while not stop.is_set():
            async with self._sessions()() as session:
                await set_transaction_tenant(session, tenant_id)
                alive = await TaskLeaseService(
                    session,
                    self.worker_id,
                    lease_seconds=self.lease_seconds,
                    retry_backoff_seconds=self.retry_backoff_seconds,
                ).heartbeat(job_id, tenant_id)
                await session.commit()
            if not alive:
                log.warning("worker_lease_lost", job_id=job_id, worker_id=self.worker_id)
                return
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.heartbeat_seconds)

    async def _handle_cleanup(self, delivery: QueueDelivery, message: JobMessage) -> None:
        """执行删除事件的外部索引清理并在完成后确认。"""
        document_id = str(message.payload.get("document_id") or "")
        async with self._sessions()() as session:
            await set_transaction_tenant(session, message.tenant_id)
            document = await session.scalar(
                select(DocumentRecord).where(
                    DocumentRecord.id == document_id,
                    DocumentRecord.tenant_id == message.tenant_id,
                )
            )
            if document is not None:
                for index in (self.container.dense_index, self.container.lexical_index):
                    if index is not None:
                        await index.delete_document(document.id)
            await session.commit()
        await delivery.ack()

    async def _handle_orphan_sweep(self, delivery: QueueDelivery, message: JobMessage) -> None:
        """在租户边界内清理超过保留期的未引用对象。"""
        if self.container.object_store is None:
            await delivery.ack()
            return
        async with self._sessions()() as session:
            await set_transaction_tenant(session, message.tenant_id)
            result = await OrphanObjectSweeper(
                session,
                self.container.object_store,
                self.container.settings.orphan_object_retention_seconds,
            ).sweep(message.tenant_id)
            await session.commit()
        log.info(
            "worker_orphan_sweep",
            tenant_id=message.tenant_id,
            scanned=result.scanned,
            deleted=result.deleted,
            unsupported=result.unsupported,
        )
        await delivery.ack()

    async def _handle_generation_cleanup(
        self, delivery: QueueDelivery, message: JobMessage
    ) -> None:
        """在保留期和审计引用检查通过后删除旧处理代次。"""
        generation_id = str(message.payload.get("generation_id") or "")
        if (
            not generation_id
            or self.container.dense_index is None
            or self.container.lexical_index is None
        ):
            await delivery.ack()
            return
        async with self._sessions()() as session:
            await set_transaction_tenant(session, message.tenant_id)
            result = await IndexGenerationCleanupService(
                session,
                self.container.dense_index,
                self.container.lexical_index,
                self.container.settings.index_generation_retention_seconds,
            ).sweep(message.tenant_id, generation_id=generation_id)
            await session.commit()
        log.info(
            "worker_generation_cleanup",
            tenant_id=message.tenant_id,
            generation_id=generation_id,
            scanned=result.scanned,
            deleted=result.deleted,
            retained=result.retained,
            unsupported=result.unsupported,
        )
        await delivery.ack()

    async def _consume_loop(self, stop: asyncio.Event) -> None:
        """持续消费 delivery，停止时不主动确认未完成消息。"""
        queue = self.container.queue
        if queue is None:
            raise RuntimeError("worker queue is not initialized")
        async for delivery in queue.consume():
            if stop.is_set():
                await delivery.nack(requeue=True)
                return
            await self.handle_delivery(delivery)

    async def _outbox_loop(self, stop: asyncio.Event) -> None:
        """持续认领并发布事务 outbox。"""
        queue = self.container.queue
        if queue is None:
            raise RuntimeError("worker queue is not initialized")
        tenant_ids = self._require_tenant_polling_scope()
        dispatcher = OutboxDispatcher(self._sessions(), queue.publish)
        while not stop.is_set():
            if tenant_ids:
                for tenant_id in tenant_ids:
                    await dispatcher.dispatch_batch(tenant_id=tenant_id)
            else:
                await dispatcher.dispatch_batch()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    stop.wait(), timeout=self.container.settings.outbox_poll_seconds
                )

    async def _lease_loop(self, stop: asyncio.Event) -> None:
        """恢复过期租约并把恢复结果重新写入 outbox。"""
        tenant_ids = self._require_tenant_polling_scope()
        while not stop.is_set():
            scopes = tenant_ids or (None,)
            for tenant_id in scopes:
                async with self._sessions()() as session:
                    if tenant_id is not None:
                        await set_transaction_tenant(session, tenant_id)
                    recovered = await TaskLeaseService(
                        session,
                        self.worker_id,
                        lease_seconds=self.lease_seconds,
                        retry_backoff_seconds=self.retry_backoff_seconds,
                    ).recover_expired(tenant_id=tenant_id)
                    if recovered:
                        jobs = list(
                            (
                                await session.scalars(
                                    select(IngestionJobRecord).where(
                                        IngestionJobRecord.id.in_(recovered)
                                    )
                                )
                            ).all()
                        )
                        for job in jobs:
                            await self._enqueue_job(session, job)
                    await session.commit()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    stop.wait(), timeout=self.container.settings.worker_poll_seconds
                )

    async def run(self, stop: asyncio.Event) -> None:
        """并发运行消费、outbox 和租约恢复，任一循环退出即优雅停止。"""
        tasks = [
            asyncio.create_task(self._consume_loop(stop), name="worker-consume"),
            asyncio.create_task(self._outbox_loop(stop), name="worker-outbox"),
            asyncio.create_task(self._lease_loop(stop), name="worker-lease"),
        ]
        stopper = asyncio.create_task(stop.wait(), name="worker-stop")
        done, pending = await asyncio.wait(
            [*tasks, stopper], return_when=asyncio.FIRST_COMPLETED
        )
        del done
        stop.set()
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.gather(*tasks, return_exceptions=True)
