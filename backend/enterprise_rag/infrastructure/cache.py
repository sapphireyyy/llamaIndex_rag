"""协调存储实现：提供内存替代品和 Redis 生产适配器。"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque

from redis.asyncio import Redis


def tenant_coordination_key(tenant_id: str, namespace: str, name: str) -> str:
    """Build one explicit tenant-prefixed coordination key."""
    if not tenant_id.strip() or not namespace.strip() or not name.strip():
        raise ValueError("tenant coordination key parts must not be empty")
    return f"rag:tenant:{tenant_id}:{namespace}:{name}"


class MemoryCoordinationStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._windows: defaultdict[str, deque[float]] = defaultdict(deque)
        self._concurrency: defaultdict[str, int] = defaultdict(int)
        self._generation: defaultdict[str, int] = defaultdict(int)

    """开发测试用的无状态协调存储，始终报告健康。"""

    async def start(self) -> None:
        """内存实现无需建立外部连接。"""
        return None

    async def health(self) -> bool:
        """返回内存协调器始终可用。"""
        return True

    async def close(self) -> None:
        """内存实现无需释放资源。"""
        return None

    async def check_rate(
        self, tenant_id: str, operation: str, limit: int, window_seconds: int = 60
    ) -> bool:
        key = tenant_coordination_key(tenant_id, "rate", operation)
        now = time.monotonic()
        async with self._lock:
            window = self._windows[key]
            while window and now - window[0] >= window_seconds:
                window.popleft()
            if len(window) >= limit:
                return False
            window.append(now)
            return True

    async def acquire_concurrency(
        self, tenant_id: str, operation: str, limit: int
    ) -> bool:
        key = tenant_coordination_key(tenant_id, "concurrency", operation)
        async with self._lock:
            if self._concurrency[key] >= limit:
                return False
            self._concurrency[key] += 1
            return True

    async def release_concurrency(self, tenant_id: str, operation: str) -> None:
        key = tenant_coordination_key(tenant_id, "concurrency", operation)
        async with self._lock:
            self._concurrency[key] = max(0, self._concurrency[key] - 1)

    async def invalidate_tenant(self, tenant_id: str) -> None:
        async with self._lock:
            self._generation[tenant_id] += 1
            prefix = f"rag:tenant:{tenant_id}:"
            for key in [item for item in self._windows if item.startswith(prefix)]:
                self._windows.pop(key, None)


class RedisCoordinationStore:
    """Redis 协调存储：负责连接生命周期和健康探测。"""

    def __init__(self, url: str) -> None:
        """根据 Redis URL 创建异步客户端。"""
        self._client = Redis.from_url(url, decode_responses=True)

    async def start(self) -> None:
        """通过 ping 验证 Redis 连接可用。"""
        if not await self._client.ping():
            raise RuntimeError("Redis ping failed")

    async def health(self) -> bool:
        """执行 Redis 健康探测，异常时返回 False。"""
        try:
            return bool(await self._client.ping())
        except Exception:
            return False

    async def close(self) -> None:
        """关闭 Redis 异步客户端。"""
        await self._client.aclose()

    async def check_rate(
        self, tenant_id: str, operation: str, limit: int, window_seconds: int = 60
    ) -> bool:
        bucket = int(time.time()) // window_seconds
        key = tenant_coordination_key(tenant_id, "rate", f"{operation}:{bucket}")
        count = int(await self._client.incr(key))
        if count == 1:
            await self._client.expire(key, window_seconds + 1)
        return count <= limit

    async def acquire_concurrency(
        self, tenant_id: str, operation: str, limit: int
    ) -> bool:
        key = tenant_coordination_key(tenant_id, "concurrency", operation)
        count = int(await self._client.incr(key))
        await self._client.expire(key, 300)
        if count > limit:
            await self._client.decr(key)
            return False
        return True

    async def release_concurrency(self, tenant_id: str, operation: str) -> None:
        key = tenant_coordination_key(tenant_id, "concurrency", operation)
        count = int(await self._client.decr(key))
        if count <= 0:
            await self._client.delete(key)

    async def invalidate_tenant(self, tenant_id: str) -> None:
        pattern = f"rag:tenant:{tenant_id}:*"
        keys = [key async for key in self._client.scan_iter(match=pattern, count=200)]
        if keys:
            await self._client.delete(*keys)
