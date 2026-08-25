"""助手应用服务：创建、校验、发布和读取助手配置快照。"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.api.errors import AppError
from enterprise_rag.application.audit import AuditEventInput, AuditService
from enterprise_rag.application.providers import AdapterRegistry
from enterprise_rag.domain.types import (
    ErrorCategory,
    RequestIdentity,
    Role,
    new_id,
    stable_json_hash,
    utc_now,
)
from enterprise_rag.infrastructure.orm import (
    AssistantRecord,
    AssistantVersionRecord,
    KnowledgeSpaceRecord,
    PolicyVersionRecord,
    ProviderProfileRecord,
    SecretBindingRecord,
)


@dataclass(frozen=True, slots=True)
class AssistantDraft:
    """助手草稿中引用的知识空间、策略版本和供应商版本。"""

    knowledge_space_ids: list[str]
    policy_version_ids: dict[str, str]
    provider_profile_ids: dict[str, str]


class AssistantService:
    """负责助手草稿校验、版本历史和活动版本切换。"""

    def __init__(self, session: AsyncSession, registry: AdapterRegistry) -> None:
        """保存数据库会话和适配器注册表，供版本校验与发布使用。"""
        self.session = session
        self.registry = registry

    @staticmethod
    def _require_admin(identity: RequestIdentity) -> None:
        """限制助手配置变更只能由租户管理员执行。"""
        if Role.ADMINISTRATOR not in identity.roles:
            raise AppError(ErrorCategory.FORBIDDEN, "Operation is not permitted.", 403)

    async def create_assistant(
        self, identity: RequestIdentity, name: str, draft: AssistantDraft
    ) -> tuple[AssistantRecord, AssistantVersionRecord]:
        """创建助手实体，并为它生成第一版草稿配置。"""
        self._require_admin(identity)
        assistant = AssistantRecord(
            id=new_id("assistant"), tenant_id=identity.tenant_id, name=name.strip()
        )
        self.session.add(assistant)
        await self.session.flush()
        version = await self.create_draft(identity, assistant.id, draft)
        return assistant, version

    async def create_draft(
        self, identity: RequestIdentity, assistant_id: str, draft: AssistantDraft
    ) -> AssistantVersionRecord:
        """递增版本号，保存助手草稿及配置哈希，保持版本内容可追踪。"""
        self._require_admin(identity)
        assistant = await self.session.scalar(
            select(AssistantRecord).where(
                AssistantRecord.id == assistant_id,
                AssistantRecord.tenant_id == identity.tenant_id,
            )
        )
        if assistant is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Resource not found.", 404)
        version_number = (
            await self.session.scalar(
                select(func.max(AssistantVersionRecord.version)).where(
                    AssistantVersionRecord.assistant_id == assistant_id
                )
            )
            or 0
        ) + 1
        config = {
            "spaces": draft.knowledge_space_ids,
            "policies": draft.policy_version_ids,
            "providers": draft.provider_profile_ids,
        }
        version = AssistantVersionRecord(
            id=new_id("assistant_version"),
            tenant_id=identity.tenant_id,
            assistant_id=assistant_id,
            version=version_number,
            knowledge_space_ids=draft.knowledge_space_ids,
            policy_version_ids=draft.policy_version_ids,
            provider_profile_ids=draft.provider_profile_ids,
            config_hash=stable_json_hash(config),
        )
        self.session.add(version)
        await self.session.flush()
        return version

    async def validate_version(
        self, identity: RequestIdentity, version: AssistantVersionRecord
    ) -> list[str]:
        """检查空间、策略、供应商能力和密钥绑定是否可用于发布。"""
        errors: list[str] = []
        spaces = list(
            (
                await self.session.scalars(
                    select(KnowledgeSpaceRecord).where(
                        KnowledgeSpaceRecord.tenant_id == identity.tenant_id,
                        KnowledgeSpaceRecord.id.in_(version.knowledge_space_ids),
                        KnowledgeSpaceRecord.state == "active",
                    )
                )
            ).all()
        )
        if len(spaces) != len(set(version.knowledge_space_ids)) or not spaces:
            errors.append("all knowledge spaces must be active and tenant-owned")
        for kind, profile_id in version.provider_profile_ids.items():
            profile = await self.session.scalar(
                select(ProviderProfileRecord).where(
                    ProviderProfileRecord.id == profile_id,
                    ProviderProfileRecord.tenant_id == identity.tenant_id,
                    ProviderProfileRecord.enabled.is_(True),
                )
            )
            if profile is None:
                errors.append(f"provider profile {kind} is unavailable")
                continue
            descriptor = self.registry.describe(profile.kind, profile.adapter)
            if descriptor is None:
                errors.append(f"provider adapter {profile.kind}/{profile.adapter} is unavailable")
                continue
            required = set(profile.config.get("required_capabilities", []))
            missing = required.difference(descriptor.capabilities)
            if missing:
                errors.append(f"provider {kind} lacks capabilities: {sorted(missing)}")
            if profile.secret_binding_ids:
                count = await self.session.scalar(
                    select(func.count(SecretBindingRecord.id)).where(
                        SecretBindingRecord.tenant_id == identity.tenant_id,
                        SecretBindingRecord.id.in_(profile.secret_binding_ids),
                        SecretBindingRecord.valid.is_(True),
                    )
                )
                if count != len(profile.secret_binding_ids):
                    errors.append(f"provider {kind} has an invalid secret binding")
        for expected_kind, policy_version_id in version.policy_version_ids.items():
            policy = await self.session.scalar(
                select(PolicyVersionRecord).where(
                    PolicyVersionRecord.tenant_id == identity.tenant_id,
                    PolicyVersionRecord.id == policy_version_id,
                    PolicyVersionRecord.state == "active",
                )
            )
            if policy is None or policy.kind != expected_kind:
                errors.append(f"policy {expected_kind} is unavailable or incompatible")
        if version.state == "draft":
            version.validation_errors = errors
        await self.session.flush()
        return errors

    async def list_assistants(self, identity: RequestIdentity) -> list[AssistantRecord]:
        """返回当前租户处于活动状态的助手。"""
        return list(
            (
                await self.session.scalars(
                    select(AssistantRecord).where(
                        AssistantRecord.tenant_id == identity.tenant_id,
                        AssistantRecord.state == "active",
                    )
                )
            ).all()
        )

    async def history(
        self, identity: RequestIdentity, assistant_id: str
    ) -> list[AssistantVersionRecord]:
        """按版本号倒序返回助手历史，并先校验租户归属。"""
        assistant = await self.session.scalar(
            select(AssistantRecord).where(
                AssistantRecord.id == assistant_id,
                AssistantRecord.tenant_id == identity.tenant_id,
            )
        )
        if assistant is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Resource not found.", 404)
        return list(
            (
                await self.session.scalars(
                    select(AssistantVersionRecord)
                    .where(AssistantVersionRecord.assistant_id == assistant_id)
                    .order_by(AssistantVersionRecord.version.desc())
                )
            ).all()
        )

    async def activate(
        self,
        identity: RequestIdentity,
        assistant_id: str,
        version_id: str,
        correlation_id: str,
    ) -> AssistantVersionRecord:
        """锁定助手、再次校验草稿，然后原子切换活动版本并写审计事件。"""
        self._require_admin(identity)
        assistant = await self.session.scalar(
            select(AssistantRecord)
            .where(
                AssistantRecord.id == assistant_id,
                AssistantRecord.tenant_id == identity.tenant_id,
            )
            .with_for_update()
        )
        version = await self.session.scalar(
            select(AssistantVersionRecord).where(
                AssistantVersionRecord.id == version_id,
                AssistantVersionRecord.assistant_id == assistant_id,
                AssistantVersionRecord.tenant_id == identity.tenant_id,
            )
        )
        if assistant is None or version is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Resource not found.", 404)
        errors = await self.validate_version(identity, version)
        if errors:
            raise AppError(ErrorCategory.INVALID_REQUEST, "; ".join(errors), 422)
        previous = assistant.active_version_id
        version.state = "active"
        version.activated_at = utc_now()
        assistant.active_version_id = version.id
        await AuditService(self.session).record(
            identity,
            AuditEventInput(
                "assistant.activate",
                "assistant",
                assistant.id,
                "allowed",
                "success",
                correlation_id,
                {"previous_version_id": previous, "new_version_id": version.id},
            ),
        )
        return version

    async def get_active_snapshot(
        self, identity: RequestIdentity, assistant_id: str
    ) -> AssistantVersionRecord:
        """读取助手当前活动版本，作为问答流程的不可变配置快照。"""
        assistant = await self.session.scalar(
            select(AssistantRecord).where(
                AssistantRecord.id == assistant_id,
                AssistantRecord.tenant_id == identity.tenant_id,
            )
        )
        if assistant is None or assistant.active_version_id is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Active assistant not found.", 404)
        version = await self.session.scalar(
            select(AssistantVersionRecord).where(
                AssistantVersionRecord.id == assistant.active_version_id,
                AssistantVersionRecord.tenant_id == identity.tenant_id,
            )
        )
        if version is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Active assistant not found.", 404)
        return version
