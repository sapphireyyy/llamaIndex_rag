"""FastAPI 应用入口：创建路由、生命周期、跨域、中间件和指标端点。"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from opentelemetry import trace
from prometheus_client import make_asgi_app
from starlette.responses import Response

from enterprise_rag.api.audit import router as audit_router
from enterprise_rag.api.chat import router as chat_router
from enterprise_rag.api.configuration import router as configuration_router
from enterprise_rag.api.errors import AppError, app_error_handler, unexpected_error_handler
from enterprise_rag.api.evaluation import router as evaluation_router
from enterprise_rag.api.health import router as health_router
from enterprise_rag.api.ingestion import router as ingestion_router
from enterprise_rag.api.knowledge import router as knowledge_router
from enterprise_rag.api.telemetry import router as telemetry_router
from enterprise_rag.api.tenant_configuration import router as tenant_configuration_router
from enterprise_rag.api.tenant_membership import router as tenant_membership_router
from enterprise_rag.api.tenants import router as tenants_router
from enterprise_rag.config import get_settings
from enterprise_rag.infrastructure.container import Container
from enterprise_rag.infrastructure.database import (
    begin_request_tenant_boundary,
    end_request_tenant_boundary,
    tenant_session_scope,
)
from enterprise_rag.infrastructure.telemetry import (
    REQUEST_LATENCY,
    REQUESTS,
    TelemetryInput,
    TelemetryService,
)

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """在应用启动时装配依赖容器，退出时按相反顺序释放外部资源。"""
    settings = get_settings()
    app.state.settings = settings
    app.state.container = Container(settings)
    await app.state.container.start()
    try:
        yield
    finally:
        await app.state.container.stop()


def create_app() -> FastAPI:
    """创建 HTTP 应用，并集中注册跨域、关联 ID、路由、错误处理和指标端点。"""
    settings = get_settings()
    application = FastAPI(
        title="Enterprise RAG Assistant",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.environment != "production" else None,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "If-Match",
            "X-Correlation-ID",
            "X-Tenant-ID",
        ],
    )

    @application.middleware("http")
    async def tenant_boundary_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Prevent selected tenant state from leaking between request tasks."""
        token = begin_request_tenant_boundary()
        try:
            return await call_next(request)
        finally:
            end_request_tenant_boundary(token)

    @application.middleware("http")
    async def correlation_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """为每个请求贯穿关联 ID、Tracing、Prometheus 指标和抽样遥测。"""
        started = time.perf_counter()
        correlation_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        request.state.correlation_id = correlation_id
        with trace.get_tracer("enterprise_rag.api").start_as_current_span(
            "http.request"
        ) as span:
            span.set_attribute("http.request.method", request.method)
            span.set_attribute("enterprise.correlation_id", correlation_id)
            response = await call_next(request)
            span.set_attribute("http.response.status_code", response.status_code)
        response.headers["X-Correlation-ID"] = correlation_id
        duration = time.perf_counter() - started
        route = getattr(request.scope.get("route"), "path", request.url.path)
        REQUESTS.labels(
            method=request.method, route=route, status=str(response.status_code)
        ).inc()
        REQUEST_LATENCY.labels(method=request.method, route=route).observe(duration)
        identity = getattr(request.state, "identity", None)
        container = request.app.state.container
        if identity is not None and container.sessions is not None:
            try:
                async with tenant_session_scope(
                    container.sessions, identity.tenant_id
                ) as session:
                    await TelemetryService(
                        session,
                        request.app.state.settings.content_capture_enabled,
                        request.app.state.settings.telemetry_sample_rate,
                    ).record(
                        TelemetryInput(
                            identity.tenant_id,
                            correlation_id,
                            "api",
                            route,
                            str(response.status_code),
                            duration * 1000,
                            {"method": request.method},
                        )
                    )
                    await session.commit()
            except Exception:
                log.warning("telemetry_record_failed", correlation_id=correlation_id)
        return response

    application.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    application.add_exception_handler(Exception, unexpected_error_handler)
    application.include_router(health_router)
    application.include_router(tenants_router)
    application.include_router(tenant_membership_router)
    application.include_router(tenant_configuration_router)
    application.include_router(audit_router)
    application.include_router(knowledge_router)
    application.include_router(configuration_router)
    application.include_router(ingestion_router)
    application.include_router(chat_router)
    application.include_router(telemetry_router)
    application.include_router(evaluation_router)
    application.mount("/metrics", make_asgi_app())
    return application


app = create_app()
