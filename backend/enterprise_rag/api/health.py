"""健康检查接口：区分进程存活、依赖就绪和版本信息。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from enterprise_rag import __version__

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live() -> dict[str, object]:
    """只检查 API 进程能否响应，不依赖外部服务。"""
    return {"status": "ok", "version": __version__}


@router.get("/health/ready")
async def ready(request: Request) -> dict[str, object]:
    """汇总配置校验和容器依赖健康状态，判断服务是否可接收请求。"""
    settings = request.app.state.settings
    problems = settings.validate_runtime()
    container = getattr(request.app.state, "container", None)
    checks = await container.readiness() if container is not None else {"container": False}
    bootstrap_problems = (
        await container.bootstrap_problems() if container is not None else ["container unavailable"]
    )
    ready_state = not problems and all(checks.values())
    return {
        "status": "ready" if ready_state else "degraded",
        "ready": ready_state,
        "checks": checks,
        "configuration_errors": problems,
        "bootstrap_errors": bootstrap_problems,
        "version": __version__,
    }
