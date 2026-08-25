"""限流基础设施：用滑动窗口限制单个身份的请求速率。"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from enterprise_rag.api.errors import AppError
from enterprise_rag.domain.types import ErrorCategory


class SlidingWindowRateLimiter:
    """按键维护请求时间戳，并用滑动窗口执行限流。"""

    def __init__(self) -> None:
        """创建每个调用键独立的时间窗口集合。"""
        self._windows: defaultdict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int, window_seconds: float = 60.0) -> None:
        """删除窗口外记录，超限抛出 429，否则登记本次请求。"""
        now = time.monotonic()
        window = self._windows[key]
        while window and now - window[0] >= window_seconds:
            window.popleft()
        if len(window) >= limit:
            raise AppError(ErrorCategory.RATE_LIMITED, "Rate limit exceeded.", 429)
        window.append(now)
