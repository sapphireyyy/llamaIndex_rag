"""HTTP 错误适配：把领域异常统一转换为可消费的 JSON 响应。"""

from __future__ import annotations

import uuid

from fastapi import Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    """可预期的业务异常，携带安全消息和对应 HTTP 状态。"""

    def __init__(self, category: str, message: str, status_code: int = 400) -> None:
        """保存安全错误分类、用户可见消息和 HTTP 状态码。"""
        super().__init__(message)
        self.category = category
        self.safe_message = message
        self.status_code = status_code


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """把可预期的业务异常转换为带关联 ID 的统一 JSON 响应。"""
    correlation_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {"category": exc.category, "message": exc.safe_message},
            "correlation_id": correlation_id,
        },
        headers={"X-Correlation-ID": correlation_id},
    )


async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """隐藏未预期异常细节，仅返回通用错误和关联 ID。"""
    del exc
    correlation_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    return JSONResponse(
        status_code=500,
        content={
            "error": {"category": "internal_error", "message": "The operation failed."},
            "correlation_id": correlation_id,
        },
        headers={"X-Correlation-ID": correlation_id},
    )
