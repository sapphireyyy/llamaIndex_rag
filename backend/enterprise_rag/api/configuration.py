"""配置接口：管理策略、模型供应商和助手版本的生命周期。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_rag.api.auth import Identity
from enterprise_rag.api.errors import AppError
from enterprise_rag.application.assistants import AssistantDraft, AssistantService
from enterprise_rag.application.configuration import ConfigurationService
from enterprise_rag.application.providers import AdapterRegistry
from enterprise_rag.domain.types import ErrorCategory
from enterprise_rag.infrastructure.orm import AssistantVersionRecord

router = APIRouter(prefix="/api/v1", tags=["configuration"])


class PolicyCreate(BaseModel):
    """策略版本创建参数，可选择立即激活。"""

    policy_id: str | None = None
    kind: str = Field(min_length=1, max_length=80)
    config: dict[str, Any] = Field(default_factory=dict)
    activate: bool = False


class ProviderCreate(BaseModel):
    """模型或能力供应商配置及其密钥绑定。"""

    name: str = Field(min_length=1, max_length=255)
    kind: str = Field(min_length=1, max_length=80)
    adapter: str = Field(min_length=1, max_length=120)
    config: dict[str, Any] = Field(default_factory=dict)
    secret_binding_ids: list[str] = Field(default_factory=list)


class AssistantDraftInput(BaseModel):
    """助手草稿引用的知识空间、策略和供应商版本。"""

    knowledge_space_ids: list[str] = Field(min_length=1)
    policy_version_ids: dict[str, str] = Field(default_factory=dict)
    provider_profile_ids: dict[str, str] = Field(default_factory=dict)


class AssistantCreate(BaseModel):
    """新助手的名称及初始草稿。"""

    name: str = Field(min_length=1, max_length=255)
    draft: AssistantDraftInput


def _resources(request: Request) -> tuple[async_sessionmaker[AsyncSession], AdapterRegistry]:
    """取得配置接口所需的数据库会话工厂和适配器注册表。"""
    container = request.app.state.container
    if container.sessions is None or container.registry is None:
        raise AppError(ErrorCategory.DEPENDENCY_UNAVAILABLE, "Configuration unavailable.", 503)
    return container.sessions, container.registry


def _draft(payload: AssistantDraftInput) -> AssistantDraft:
    """把 Pydantic 请求模型转换为应用层助手草稿对象。"""
    return AssistantDraft(
        payload.knowledge_space_ids,
        payload.policy_version_ids,
        payload.provider_profile_ids,
    )


def _version_view(version: AssistantVersionRecord) -> dict[str, object]:
    """把助手版本 ORM 对象转换为前端使用的版本摘要。"""
    return {
        "id": version.id,
        "version": version.version,
        "state": version.state,
        "knowledge_space_ids": version.knowledge_space_ids,
        "policy_version_ids": version.policy_version_ids,
        "provider_profile_ids": version.provider_profile_ids,
        "config_hash": version.config_hash,
        "validation_errors": version.validation_errors,
        "activated_at": version.activated_at,
    }


@router.get("/policies")
async def list_policies(request: Request, identity: Identity) -> dict[str, object]:
    """列出当前租户可用的策略版本。"""
    sessions, registry = _resources(request)
    async with sessions() as session:
        records = await ConfigurationService(session, registry).list_policies(identity)
        return {
            "items": [
                {
                    "id": item.id,
                    "policy_id": item.policy_id,
                    "kind": item.kind,
                    "version": item.version,
                    "state": item.state,
                    "config": item.config,
                    "content_hash": item.content_hash,
                }
                for item in records
            ]
        }


@router.post("/policies", status_code=201)
async def create_policy(
    request: Request, payload: PolicyCreate, identity: Identity
) -> dict[str, object]:
    """创建策略版本，并按请求决定是否激活。"""
    sessions, registry = _resources(request)
    async with sessions() as session:
        item = await ConfigurationService(session, registry).create_policy(
            identity, payload.policy_id, payload.kind, payload.config, payload.activate
        )
        await session.commit()
        return {"id": item.id, "policy_id": item.policy_id, "version": item.version}


@router.get("/provider-profiles")
async def list_provider_profiles(request: Request, identity: Identity) -> dict[str, object]:
    """列出模型供应商配置及其可用能力和密钥绑定。"""
    sessions, registry = _resources(request)
    async with sessions() as session:
        records = await ConfigurationService(session, registry).list_provider_profiles(identity)
        return {
            "items": [
                {
                    "id": profile.id,
                    "name": profile.name,
                    "kind": profile.kind,
                    "adapter": profile.adapter,
                    "capabilities": profile.capabilities,
                    "config": profile.config,
                    "secret_bindings": bindings,
                    "enabled": profile.enabled,
                }
                for profile, bindings in records
            ]
        }


@router.post("/provider-profiles", status_code=201)
async def create_provider_profile(
    request: Request, payload: ProviderCreate, identity: Identity
) -> dict[str, object]:
    """创建供应商配置，并由注册表校验适配器能力。"""
    sessions, registry = _resources(request)
    async with sessions() as session:
        item = await ConfigurationService(session, registry).create_provider_profile(
            identity,
            payload.name,
            payload.kind,
            payload.adapter,
            payload.config,
            payload.secret_binding_ids,
        )
        await session.commit()
        return {"id": item.id, "capabilities": item.capabilities}


@router.get("/assistants")
async def list_assistants(request: Request, identity: Identity) -> dict[str, object]:
    """列出当前租户的助手及其活动版本。"""
    sessions, registry = _resources(request)
    async with sessions() as session:
        records = await AssistantService(session, registry).list_assistants(identity)
        return {
            "items": [
                {
                    "id": item.id,
                    "name": item.name,
                    "active_version_id": item.active_version_id,
                }
                for item in records
            ]
        }


@router.post("/assistants", status_code=201)
async def create_assistant(
    request: Request, payload: AssistantCreate, identity: Identity
) -> dict[str, object]:
    """创建助手和第一版草稿配置。"""
    sessions, registry = _resources(request)
    async with sessions() as session:
        assistant, version = await AssistantService(session, registry).create_assistant(
            identity, payload.name, _draft(payload.draft)
        )
        await session.commit()
        return {"id": assistant.id, "name": assistant.name, "draft": _version_view(version)}


@router.post("/assistants/{assistant_id}/versions", status_code=201)
async def create_assistant_version(
    request: Request, assistant_id: str, payload: AssistantDraftInput, identity: Identity
) -> dict[str, object]:
    """为既有助手创建新的不可变草稿版本。"""
    sessions, registry = _resources(request)
    async with sessions() as session:
        version = await AssistantService(session, registry).create_draft(
            identity, assistant_id, _draft(payload)
        )
        await session.commit()
        return _version_view(version)


@router.get("/assistants/{assistant_id}/versions")
async def assistant_history(
    request: Request, assistant_id: str, identity: Identity
) -> dict[str, object]:
    """返回助手的版本历史，供校验和发布流程选择版本。"""
    sessions, registry = _resources(request)
    async with sessions() as session:
        versions = await AssistantService(session, registry).history(identity, assistant_id)
        return {"items": [_version_view(item) for item in versions]}


@router.post("/assistants/{assistant_id}/versions/{version_id}/validate")
async def validate_assistant_version(
    request: Request, assistant_id: str, version_id: str, identity: Identity
) -> dict[str, object]:
    """校验助手版本引用的空间、策略和供应商是否完整且同租户。"""
    sessions, registry = _resources(request)
    async with sessions() as session:
        versions = await AssistantService(session, registry).history(identity, assistant_id)
        version = next((item for item in versions if item.id == version_id), None)
        if version is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Resource not found.", 404)
        errors = await AssistantService(session, registry).validate_version(identity, version)
        await session.commit()
        return {"valid": not errors, "errors": errors}


@router.post("/assistants/{assistant_id}/versions/{version_id}/activate")
async def activate_assistant_version(
    request: Request, assistant_id: str, version_id: str, identity: Identity
) -> dict[str, object]:
    """激活通过校验的助手版本，并记录发布关联标识。"""
    sessions, registry = _resources(request)
    async with sessions() as session:
        version = await AssistantService(session, registry).activate(
            identity, assistant_id, version_id, request.state.correlation_id
        )
        await session.commit()
        return _version_view(version)
