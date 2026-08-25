"""消息队列基础设施：提供进程内队列、RabbitMQ 和可靠 outbox 分发。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractQueue, AbstractRobustConnection
from opentelemetry import trace
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_rag.domain.types import JobMessage, new_id, utc_now
from enterprise_rag.infrastructure.orm import OutboxRecord


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

    async def get(self) -> JobMessage:
        """获取下一条待处理消息。"""
        _, _, message = await self._queue.get()
        return message

    def task_done(self) -> None:
        """标记当前队列任务已完成。"""
        self._queue.task_done()

    def consume(self) -> AsyncIterator[JobMessage]:
        """持续生成待处理的队列消息。"""
        async def iterator() -> AsyncIterator[JobMessage]:
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

    async def get(self, timeout: float = 10.0) -> JobMessage:
        """获取下一条待处理消息。"""
        if self._queue is None:
            raise RuntimeError("RabbitMQ queue is not started")
        incoming = await self._queue.get(timeout=timeout, fail=False)
        if incoming is None:
            raise TimeoutError("RabbitMQ queue receive timed out")
        async with incoming.process(requeue=True):
            return self._decode(incoming.body)

    def consume(self) -> AsyncIterator[JobMessage]:
        """持续生成待处理的队列消息。"""
        async def iterator() -> AsyncIterator[JobMessage]:
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

    def _pending_statement(self, limit: int) -> Select[tuple[OutboxRecord]]:
        """构建查询待投递事件的语句。"""
        return (
            select(OutboxRecord)
            .where(
                OutboxRecord.published_at.is_(None),
                OutboxRecord.poisoned_at.is_(None),
                OutboxRecord.available_at <= utc_now(),
            )
            .order_by(OutboxRecord.priority.desc(), OutboxRecord.created_at)
            .limit(limit)
        )

    async def dispatch_batch(self, limit: int = 100) -> tuple[int, int]:
        """批量投递发件箱事件并记录结果。"""
        published = 0
        poisoned = 0
        async with self.session_factory() as session:
            records = list((await session.scalars(self._pending_statement(limit))).all())
            for record in records:
                message = JobMessage(
                    id=record.id,
                    kind=record.kind,
                    tenant_id=record.tenant_id,
                    correlation_id=record.correlation_id,
                    payload=record.payload,
                    priority=record.priority,
                    attempt=record.attempts,
                    max_attempts=record.max_attempts,
                )
                with trace.get_tracer("enterprise_rag.queue").start_as_current_span(
                    "queue.dispatch"
                ) as span:
                    span.set_attribute("messaging.message.id", record.id)
                    span.set_attribute("messaging.correlation_id", record.correlation_id)
                    try:
                        await self.publish(message)
                        record.published_at = utc_now()
                        published += 1
                    except Exception:
                        record.attempts += 1
                        if record.attempts >= record.max_attempts:
                            record.poisoned_at = utc_now()
                            poisoned += 1
                        else:
                            record.available_at = utc_now() + timedelta(
                                seconds=2**record.attempts
                            )
                await session.flush()
            await session.commit()
        return published, poisoned


async def consume_with_retry(
    queue: InProcessQueue,
    handler: Callable[[JobMessage], Awaitable[None]],
) -> None:
    """消费进程内队列，失败时按最大尝试次数重新入队。"""
    async for message in queue.consume():
        try:
            await handler(message)
        except Exception:
            if message.attempt + 1 < message.max_attempts:
                await queue.publish(
                    replace(message, id=new_id("retry"), attempt=message.attempt + 1)
                )
        finally:
            queue.task_done()
