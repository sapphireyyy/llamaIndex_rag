"""消息队列基础设施：提供进程内队列、RabbitMQ 和可靠 outbox 分发。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, cast

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractQueue, AbstractRobustConnection
from opentelemetry import trace
from sqlalchemy import Select, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_rag.domain.types import JobMessage, new_id, utc_now
from enterprise_rag.infrastructure.orm import OutboxRecord
from enterprise_rag.infrastructure.telemetry import record_outbox_event, record_queue_delivery


class QueueDelivery:
    """统一进程内和 RabbitMQ 消息的显式确认生命周期。"""

    def __init__(
        self,
        message: JobMessage,
        *,
        ack_callback: Callable[[], Awaitable[None]] | None = None,
        nack_callback: Callable[[bool], Awaitable[None]] | None = None,
        done_callback: Callable[[], None] | None = None,
    ) -> None:
        """保存消息和底层队列的确认/拒绝回调。"""
        self.message = message
        self._ack_callback = ack_callback
        self._nack_callback = nack_callback
        self._done_callback = done_callback
        self._settled = False
        self._started_at = time.perf_counter()

    def __getattr__(self, name: str) -> Any:
        """向后兼容旧的直接读取 delivery.id 等消息字段。"""
        return getattr(self.message, name)

    async def ack(self) -> None:
        """在业务事务提交后确认消息，并只结算一次队列任务。"""
        if self._settled:
            return
        if self._ack_callback is not None:
            await self._ack_callback()
        self._settled = True
        if self._done_callback is not None:
            self._done_callback()
        record_queue_delivery(self.message.kind, "ack", time.perf_counter() - self._started_at)

    async def nack(self, *, requeue: bool = True) -> None:
        """拒绝消息；底层队列决定是否重新投递。"""
        if self._settled:
            return
        if self._nack_callback is not None:
            await self._nack_callback(requeue)
        self._settled = True
        if self._done_callback is not None:
            self._done_callback()
        record_queue_delivery(self.message.kind, "nack", time.perf_counter() - self._started_at)


class InProcessQueue:
    """开发环境优先级队列，按消息 ID 做进程内幂等去重。"""

    def __init__(self, max_size: int = 1_000) -> None:
        """初始化进程内队列或发件箱分发器。"""
        self._queue: asyncio.PriorityQueue[tuple[int, int, JobMessage]] = asyncio.PriorityQueue(
            maxsize=max_size
        )
        self._sequence = 0
        self._published_ids: set[str] = set()

    async def publish(self, message: JobMessage) -> None:
        """发布一条队列消息。"""
        if message.id in self._published_ids:
            return
        self._published_ids.add(message.id)
        self._sequence += 1
        await self._queue.put((-message.priority, self._sequence, message))

    async def get(self) -> QueueDelivery:
        """获取下一条待处理消息，但不自动确认。"""
        _, _, message = await self._queue.get()
        async def requeue(should_requeue: bool) -> None:
            """按统一 delivery 语义重新投递未超限消息。"""
            if should_requeue and message.attempt + 1 < message.max_attempts:
                await self.publish(
                    replace(message, id=new_id("retry"), attempt=message.attempt + 1)
                )

        return QueueDelivery(
            message,
            nack_callback=requeue,
            done_callback=self.task_done,
        )

    def task_done(self) -> None:
        """标记当前队列任务已完成。"""
        self._queue.task_done()

    def consume(self) -> AsyncIterator[QueueDelivery]:
        """持续生成待处理的 delivery，调用方必须显式 ACK/NACK。"""
        async def iterator() -> AsyncIterator[QueueDelivery]:
            """持续等待并产出进程内队列消息。"""
            while True:
                yield await self.get()

        return iterator()

    @property
    def size(self) -> int:
        """返回当前积压消息数量。"""
        return self._queue.qsize()

    async def health(self) -> bool:
        """进程内队列无需外部探测，始终报告可用。"""
        return True


class RabbitMQQueue:
    """生产环境持久化 AMQP 队列，支持优先级和鲁棒连接。"""

    def __init__(self, url: str, queue_name: str, max_priority: int = 100) -> None:
        """初始化进程内队列或发件箱分发器。"""
        self.url = url
        self.queue_name = queue_name
        self.max_priority = max_priority
        self._connection: AbstractRobustConnection | None = None
        self._channel: AbstractChannel | None = None
        self._queue: AbstractQueue | None = None

    async def start(self) -> None:
        """建立 RabbitMQ 连接、设置预取并声明持久化优先级队列。"""
        self._connection = await aio_pika.connect_robust(self.url)
        channel = await self._connection.channel(publisher_confirms=True)
        self._channel = channel
        await channel.set_qos(prefetch_count=8)
        self._queue = await channel.declare_queue(
            self.queue_name,
            durable=True,
            arguments={"x-max-priority": self.max_priority},
        )

    @staticmethod
    def _body(message: JobMessage) -> bytes:
        """把任务消息编码为可持久化传输的 JSON 字节。"""
        return json.dumps(
            {
                "id": message.id,
                "kind": message.kind,
                "tenant_id": message.tenant_id,
                "correlation_id": message.correlation_id,
                "payload": message.payload,
                "priority": message.priority,
                "attempt": message.attempt,
                "max_attempts": message.max_attempts,
                "created_at": message.created_at.isoformat(),
            },
            separators=(",", ":"),
        ).encode("utf-8")

    @staticmethod
    def _decode(body: bytes) -> JobMessage:
        """把 RabbitMQ 消息体解码为带时间和重试信息的任务对象。"""
        value: dict[str, Any] = json.loads(body)
        return JobMessage(
            id=str(value["id"]),
            kind=str(value["kind"]),
            tenant_id=str(value["tenant_id"]),
            correlation_id=str(value["correlation_id"]),
            payload=dict(value["payload"]),
            priority=int(value.get("priority", 0)),
            attempt=int(value.get("attempt", 0)),
            max_attempts=int(value.get("max_attempts", 3)),
            created_at=datetime.fromisoformat(str(value["created_at"])),
        )

    async def publish(self, message: JobMessage) -> None:
        """发布一条队列消息。"""
        if self._channel is None:
            raise RuntimeError("RabbitMQ queue is not started")
        outgoing = aio_pika.Message(
            body=self._body(message),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=message.id,
            correlation_id=message.correlation_id,
            priority=max(0, min(message.priority, self.max_priority)),
            timestamp=message.created_at,
        )
        await self._channel.default_exchange.publish(
            outgoing, routing_key=self.queue_name, mandatory=True
        )

    async def get(self, timeout: float = 10.0) -> QueueDelivery:
        """获取一条尚未确认的 RabbitMQ delivery。"""
        if self._queue is None:
            raise RuntimeError("RabbitMQ queue is not started")
        incoming = await self._queue.get(timeout=timeout, fail=False)
        if incoming is None:
            raise TimeoutError("RabbitMQ queue receive timed out")
        message = self._decode(incoming.body)

        async def acknowledge() -> None:
            """在业务提交之后才向 RabbitMQ 发送 ACK。"""
            await incoming.ack()

        async def reject(requeue: bool) -> None:
            """把失败任务交回 RabbitMQ，由 broker 决定再次投递。"""
            await incoming.nack(requeue=requeue)

        return QueueDelivery(message, ack_callback=acknowledge, nack_callback=reject)

    def consume(self) -> AsyncIterator[QueueDelivery]:
        """持续生成尚未确认的 RabbitMQ delivery。"""
        async def iterator() -> AsyncIterator[QueueDelivery]:
            """持续等待并产出 RabbitMQ 队列消息。"""
            while True:
                yield await self.get()

        return iterator()

    async def health(self) -> bool:
        """报告 RabbitMQ 连接已建立且未关闭。"""
        return self._connection is not None and not self._connection.is_closed

    async def close(self) -> None:
        """关闭 RabbitMQ 连接。"""
        if self._connection is not None and not self._connection.is_closed:
            await self._connection.close()


class OutboxService:
    """事务 outbox 写入服务，保证业务提交与待投递事件同事务。"""

    def __init__(self, session: AsyncSession) -> None:
        """保存当前数据库会话。"""
        self.session = session

    async def add(
        self,
        tenant_id: str,
        kind: str,
        correlation_id: str,
        payload: dict[str, object],
        priority: int = 0,
        max_attempts: int = 3,
    ) -> OutboxRecord:
        """向事务发件箱写入待投递事件。"""
        payload_tenant = payload.get("tenant_id")
        if payload_tenant is not None and str(payload_tenant) != tenant_id:
            raise ValueError("outbox payload tenant does not match its envelope")
        record = OutboxRecord(
            id=new_id("evt"),
            tenant_id=tenant_id,
            kind=kind,
            correlation_id=correlation_id,
            payload={"tenant_id": tenant_id, **payload},
            priority=priority,
            max_attempts=max_attempts,
        )
        self.session.add(record)
        await self.session.flush()
        return record


class OutboxDispatcher:
    """扫描未发布 outbox 记录，投递并处理退避和毒消息。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        publish: Callable[[JobMessage], Awaitable[None]],
    ) -> None:
        """保存会话工厂和实际消息发布函数。"""
        self.session_factory = session_factory
        self.publish = publish
        self.dispatcher_id = new_id("outbox-dispatcher")
        self.claim_seconds = 60

    def _pending_statement(
        self, limit: int, tenant_id: str | None = None
    ) -> Select[tuple[OutboxRecord]]:
        """构建查询待投递事件的语句。"""
        conditions = [
            OutboxRecord.published_at.is_(None),
            OutboxRecord.poisoned_at.is_(None),
            OutboxRecord.available_at <= utc_now(),
            or_(
                OutboxRecord.claimed_by.is_(None),
                OutboxRecord.claim_expires_at <= utc_now(),
            ),
        ]
        if tenant_id is not None:
            conditions.append(OutboxRecord.tenant_id == tenant_id)
        return (
            select(OutboxRecord)
            .where(*conditions)
            .order_by(OutboxRecord.priority.desc(), OutboxRecord.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )

    async def dispatch_batch(
        self, limit: int = 100, *, tenant_id: str | None = None
    ) -> tuple[int, int]:
        """批量投递发件箱事件并记录结果。"""
        published = 0
        poisoned = 0
        claimed: list[dict[str, Any]] = []
        async with self.session_factory() as session:
            if tenant_id is not None:
                from enterprise_rag.infrastructure.database import set_transaction_tenant

                await set_transaction_tenant(session, tenant_id)
            records = list(
                (await session.scalars(self._pending_statement(limit, tenant_id))).all()
            )
            claim_until = utc_now() + timedelta(seconds=self.claim_seconds)
            for record in records:
                record.claimed_by = self.dispatcher_id
                record.claim_expires_at = claim_until
                claimed.append(
                    {
                        "id": record.id,
                        "kind": record.kind,
                        "tenant_id": record.tenant_id,
                        "correlation_id": record.correlation_id,
                        "payload": dict(record.payload),
                        "priority": record.priority,
                        "attempts": record.attempts,
                        "max_attempts": record.max_attempts,
                    }
                )
            await session.commit()
        for claimed_record in claimed:
            message = JobMessage(
                id=str(claimed_record["id"]),
                kind=str(claimed_record["kind"]),
                tenant_id=str(claimed_record["tenant_id"]),
                correlation_id=str(claimed_record["correlation_id"]),
                payload=dict(claimed_record["payload"]),
                priority=int(claimed_record["priority"]),
                attempt=int(claimed_record["attempts"]),
                max_attempts=int(claimed_record["max_attempts"]),
            )
            with trace.get_tracer("enterprise_rag.queue").start_as_current_span(
                "queue.dispatch"
            ) as span:
                span.set_attribute("messaging.message.id", message.id)
                span.set_attribute("messaging.correlation_id", message.correlation_id)
                try:
                    await self.publish(message)
                    record_outbox_event("published")
                    async with self.session_factory() as session:
                        if tenant_id is not None:
                            from enterprise_rag.infrastructure.database import (
                                set_transaction_tenant,
                            )

                            await set_transaction_tenant(session, message.tenant_id)
                        result = await session.execute(
                            update(OutboxRecord)
                            .where(
                                OutboxRecord.id == message.id,
                                OutboxRecord.claimed_by == self.dispatcher_id,
                                OutboxRecord.published_at.is_(None),
                            )
                            .values(
                                published_at=utc_now(),
                                claimed_by=None,
                                claim_expires_at=None,
                            )
                        )
                        await session.commit()
                        published += int(cast(Any, result).rowcount or 0)
                except Exception:
                    record_outbox_event(
                        "retry" if message.attempt + 1 < message.max_attempts else "poisoned"
                    )
                    next_attempt = message.attempt + 1
                    values: dict[str, Any] = {
                        "attempts": next_attempt,
                        "claimed_by": None,
                        "claim_expires_at": None,
                        "last_error": "queue publish failed",
                    }
                    if next_attempt >= message.max_attempts:
                        values["poisoned_at"] = utc_now()
                        poisoned += 1
                    else:
                        values["available_at"] = utc_now() + timedelta(
                            seconds=2**next_attempt
                        )
                    async with self.session_factory() as session:
                        if tenant_id is not None:
                            from enterprise_rag.infrastructure.database import (
                                set_transaction_tenant,
                            )

                            await set_transaction_tenant(session, message.tenant_id)
                        await session.execute(
                            update(OutboxRecord)
                            .where(
                                OutboxRecord.id == message.id,
                                OutboxRecord.claimed_by == self.dispatcher_id,
                            )
                            .values(**values)
                        )
                        await session.commit()
        return published, poisoned


async def consume_with_retry(
    queue: InProcessQueue,
    handler: Callable[[JobMessage], Awaitable[None]],
) -> None:
    """消费进程内队列，失败时按最大尝试次数重新入队。"""
    async for delivery in queue.consume():
        message = delivery.message
        try:
            await handler(message)
        except Exception:
            await delivery.nack(requeue=False)
            if message.attempt + 1 < message.max_attempts:
                await queue.publish(
                    replace(message, id=new_id("retry"), attempt=message.attempt + 1)
                )
        except BaseException:
            await delivery.nack(requeue=False)
            raise
        else:
            await delivery.ack()
