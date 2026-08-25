"""知识库接口：管理知识空间、成员、数据源及其启用状态。"""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Header, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_rag.api.auth import Identity
from enterprise_rag.api.errors import AppError
from enterprise_rag.application.knowledge import KnowledgeService
from enterprise_rag.domain.types import ErrorCategory, new_id, stable_json_hash
from enterprise_rag.infrastructure.orm import IdempotencyRecord, KnowledgeSpaceRecord

router = APIRouter(prefix="/api/v1", tags=["knowledge"])


class SpaceCreate(BaseModel):
    """创建知识空间时提交的名称和描述。"""

    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=4_000)


class SpaceUpdate(BaseModel):
    """更新知识空间基本信息和默认策略绑定。"""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4_000)
    default_policy_ids: dict[str, str] | None = None


class MembershipSet(BaseModel):
    """设置用户或群组在知识空间中的角色。"""

    principal_token: str = Field(pattern=r"^(user|group):.+", max_length=320)
    role: str | None = None


class SourceCreate(BaseModel):
    """创建数据源时提交连接类型、外部标识和配置。"""

    kind: str = Field(min_length=1, max_length=80)
    external_id: str = Field(min_length=1, max_length=512)
    config: dict[str, Any] = Field(default_factory=dict)


class SourceState(BaseModel):
    """表示数据源是否参与后续同步和检索。"""

    enabled: bool


def _space_view(space: KnowledgeSpaceRecord) -> dict[str, object]:
    """把 ORM 知识空间转换成不暴露内部字段的 API 响应。"""
    return {
        "id": space.id,
        "tenant_id": space.tenant_id,
        "name": space.name,
        "description": space.description,
        "state": space.state,
        "default_policy_ids": space.default_policy_ids,
        "created_at": space.created_at,
        "updated_at": space.updated_at,
    }


def _sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    """从应用容器取得知识库接口使用的异步会话工厂。"""
    sessions = request.app.state.container.sessions
    if sessions is None:
        raise AppError(ErrorCategory.DEPENDENCY_UNAVAILABLE, "Knowledge service unavailable.", 503)
    return cast(async_sessionmaker[AsyncSession], sessions)


@router.get("/knowledge-spaces")
async def list_spaces(request: Request, identity: Identity) -> dict[str, object]:
    """列出当前身份可访问的知识空间。"""
    async with _sessions(request)() as session:
        spaces = await KnowledgeService(session).list_spaces(identity)
        return {"items": [_space_view(space) for space in spaces]}


@router.post("/knowledge-spaces", status_code=201)
async def create_space(
    request: Request,
    payload: SpaceCreate,
    identity: Identity,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, object]:
    """创建知识空间，并用幂等键避免重复创建。"""
    async with _sessions(request)() as session:
        request_hash = stable_json_hash(payload.model_dump())
        if idempotency_key:
            existing = await session.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.tenant_id == identity.tenant_id,
                    IdempotencyRecord.operation == "knowledge.create",
                    IdempotencyRecord.key == idempotency_key,
                )
            )
            if existing:
                if existing.request_hash != request_hash:
                    raise AppError(ErrorCategory.CONFLICT, "Idempotency key was reused.", 409)
                space = await session.scalar(
                    select(KnowledgeSpaceRecord).where(
                        KnowledgeSpaceRecord.id
                        == (
                            existing.response_body.decode("utf-8")
                            if existing.response_body
                            else ""
                        ),
                        KnowledgeSpaceRecord.tenant_id == identity.tenant_id,
                    )
                )
                if space:
                    return _space_view(space)
        space = await KnowledgeService(session).create_space(
            identity, payload.name, payload.description, request.state.correlation_id
        )
        if idempotency_key:
            session.add(
                IdempotencyRecord(
                    id=new_id("idempotency"),
                    tenant_id=identity.tenant_id,
                    operation="knowledge.create",
                    key=idempotency_key,
                    request_hash=request_hash,
                    response_status=201,
                    response_body=space.id.encode("utf-8"),
                )
            )
        await session.commit()
        return _space_view(space)


@router.patch("/knowledge-spaces/{space_id}")
async def update_space(
    request: Request, space_id: str, payload: SpaceUpdate, identity: Identity
) -> dict[str, object]:
    """更新知识空间的名称、描述和默认策略绑定。"""
    async with _sessions(request)() as session:
        space = await KnowledgeService(session).update_space(
            identity,
            space_id,
            payload.name,
            payload.description,
            payload.default_policy_ids,
        )
        await session.commit()
        return _space_view(space)


@router.post("/knowledge-spaces/{space_id}/actions/{action}")
async def change_space_state(
    request: Request, space_id: str, action: str, identity: Identity
) -> dict[str, object]:
    """根据 archive/restore 动作切换知识空间生命周期状态。"""
    state = {"archive": "archived", "restore": "active"}.get(action)
    if state is None:
        raise AppError(ErrorCategory.INVALID_REQUEST, "Invalid state transition.", 422)
    async with _sessions(request)() as session:
        space = await KnowledgeService(session).set_state(
            identity, space_id, state, request.state.correlation_id
        )
        await session.commit()
        return _space_view(space)


@router.put("/knowledge-spaces/{space_id}/memberships", response_model=None)
async def set_membership(
    request: Request, space_id: str, payload: MembershipSet, identity: Identity
) -> Response | dict[str, object]:
    """新增或更新成员角色；传入空角色时移除该成员。"""
    async with _sessions(request)() as session:
        membership = await KnowledgeService(session).set_membership(
            identity, space_id, payload.principal_token, payload.role
        )
        await session.commit()
        if membership is None:
            return Response(status_code=204)
        return {
            "id": membership.id,
            "principal_token": membership.principal_token,
            "role": membership.role,
        }


@router.get("/knowledge-spaces/{space_id}/memberships")
async def list_memberships(
    request: Request, space_id: str, identity: Identity
) -> dict[str, object]:
    """列出知识空间的成员令牌及其角色。"""
    async with _sessions(request)() as session:
        memberships = await KnowledgeService(session).list_memberships(identity, space_id)
        return {
            "items": [
                {
                    "id": item.id,
                    "principal_token": item.principal_token,
                    "role": item.role,
                }
                for item in memberships
            ]
        }


@router.post("/knowledge-spaces/{space_id}/data-sources", status_code=201)
async def create_source(
    request: Request, space_id: str, payload: SourceCreate, identity: Identity
) -> dict[str, object]:
    """为知识空间登记一个外部数据源。"""
    async with _sessions(request)() as session:
        source = await KnowledgeService(session).add_source(
            identity, space_id, payload.kind, payload.external_id, payload.config
        )
        await session.commit()
        return {
            "id": source.id,
            "knowledge_space_id": source.knowledge_space_id,
            "kind": source.kind,
            "external_id": source.external_id,
            "enabled": source.enabled,
        }


@router.get("/knowledge-spaces/{space_id}/data-sources")
async def list_sources(
    request: Request, space_id: str, identity: Identity
) -> dict[str, object]:
    """列出知识空间绑定的数据源及启用状态。"""
    async with _sessions(request)() as session:
        sources = await KnowledgeService(session).list_sources(identity, space_id)
        return {
            "items": [
                {
                    "id": item.id,
                    "kind": item.kind,
                    "external_id": item.external_id,
                    "enabled": item.enabled,
                }
                for item in sources
            ]
        }


@router.patch("/data-sources/{source_id}")
async def set_source_state(
    request: Request, source_id: str, payload: SourceState, identity: Identity
) -> dict[str, object]:
    """启用或停用数据源，并让应用服务处理权限与审计规则。"""
    async with _sessions(request)() as session:
        source = await KnowledgeService(session).set_source_enabled(
            identity, source_id, payload.enabled
        )
        await session.commit()
        return {"id": source.id, "enabled": source.enabled}


@router.delete("/data-sources/{source_id}", status_code=204)
async def remove_source(request: Request, source_id: str, identity: Identity) -> Response:
    """删除数据源绑定，并触发相关索引清理逻辑。"""
    async with _sessions(request)() as session:
        await KnowledgeService(session).remove_source(
            identity, source_id, request.state.correlation_id
        )
        await session.commit()
    return Response(status_code=204)
