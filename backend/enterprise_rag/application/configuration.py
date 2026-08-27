"""配置应用服务：维护策略版本和模型供应商配置。"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.api.errors import AppError
from enterprise_rag.application.providers import AdapterRegistry
from enterprise_rag.domain.types import (
    ErrorCategory,
    RequestIdentity,
    Role,
    new_id,
    stable_json_hash,
)
from enterprise_rag.infrastructure.orm import (
    PolicyVersionRecord,
    ProviderProfileRecord,
    SecretBindingRecord,
)


class ConfigurationService:
    """负责策略版本和供应商配置的创建、校验与查询。"""

    def __init__(self, session: AsyncSession, registry: AdapterRegistry) -> None:
        """保存事务会话和适配器注册表，供配置创建和校验使用。"""
        self.session = session
        self.registry = registry

    @staticmethod
    def _require_admin(identity: RequestIdentity) -> None:
        """限制模型与策略配置变更只能由租户管理员执行。"""
        if Role.ADMINISTRATOR not in identity.roles:
            raise AppError(ErrorCategory.FORBIDDEN, "Operation is not permitted.", 403)

    @staticmethod
    def _contains_raw_secret(value: object, sensitive_keys: set[str]) -> bool:
        """递归拒绝嵌套配置中的明文密钥，避免仅检查顶层字段。"""
        if isinstance(value, Mapping):
            for key, nested in value.items():
                if str(key).strip().lower() in sensitive_keys:
                    return True
                if ConfigurationService._contains_raw_secret(nested, sensitive_keys):
                    return True
            return False
        if isinstance(value, (list, tuple)):
            return any(
                ConfigurationService._contains_raw_secret(item, sensitive_keys)
                for item in value
            )
        return False

    async def create_policy(
        self,
        identity: RequestIdentity,
        policy_id: str | None,
        kind: str,
        config: dict[str, object],
        activate: bool = False,
    ) -> PolicyVersionRecord:
        """按策略 ID 递增版本，计算内容哈希并保存草稿或活动状态。"""
        self._require_admin(identity)
        stable_id = policy_id or new_id("policy")
        version = (
            await self.session.scalar(
                select(func.max(PolicyVersionRecord.version)).where(
                    PolicyVersionRecord.tenant_id == identity.tenant_id,
                    PolicyVersionRecord.policy_id == stable_id,
                )
            )
            or 0
        ) + 1
        record = PolicyVersionRecord(
            id=new_id("policy_version"),
            tenant_id=identity.tenant_id,
            policy_id=stable_id,
            kind=kind,
            version=version,
            state="active" if activate else "draft",
            config=config,
            content_hash=stable_json_hash(config),
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def create_provider_profile(
        self,
        identity: RequestIdentity,
        name: str,
        kind: str,
        adapter: str,
        config: dict[str, object],
        secret_binding_ids: list[str],
    ) -> ProviderProfileRecord:
        """校验适配器和密钥绑定后，保存供应商能力快照。"""
        self._require_admin(identity)
        sensitive_keys = {
            "password",
            "secret",
            "client_secret",
            "api_key",
            "access_token",
            "refresh_token",
            "private_key",
        }
        if self._contains_raw_secret(config, sensitive_keys):
            raise AppError(
                ErrorCategory.INVALID_REQUEST,
                "Provider config must reference a secret binding, not contain a raw secret.",
                422,
            )
        if adapter == "openai-compatible":
            api_key_reference = str(config.get("api_key_reference") or "")
            if api_key_reference and not api_key_reference.startswith("env://"):
                raise AppError(
                    ErrorCategory.INVALID_REQUEST,
                    "Provider API keys must use an environment secret reference.",
                    422,
                )
        descriptor = self.registry.describe(kind, adapter)
        if descriptor is None:
            raise AppError(ErrorCategory.INVALID_REQUEST, "Provider adapter is unavailable.", 422)
        if secret_binding_ids:
            count = (
                await self.session.scalar(
                    select(func.count(SecretBindingRecord.id)).where(
                        SecretBindingRecord.tenant_id == identity.tenant_id,
                        SecretBindingRecord.id.in_(secret_binding_ids),
                        SecretBindingRecord.valid.is_(True),
                    )
                )
                or 0
            )
            if count != len(set(secret_binding_ids)):
                raise AppError(
                    ErrorCategory.INVALID_REQUEST, "Secret binding is unavailable.", 422
                )
        profile = ProviderProfileRecord(
            id=new_id("provider"),
            tenant_id=identity.tenant_id,
            name=name.strip(),
            kind=kind,
            adapter=adapter,
            capabilities=sorted(descriptor.capabilities),
            config=config,
            secret_binding_ids=secret_binding_ids,
            profile_hash=stable_json_hash(
                {
                    "name": name.strip(),
                    "kind": kind,
                    "adapter": adapter,
                    "capabilities": sorted(descriptor.capabilities),
                    "config": config,
                    "secret_binding_ids": sorted(set(secret_binding_ids)),
                }
            ),
        )
        self.session.add(profile)
        await self.session.flush()
        return profile

    async def list_provider_profiles(
        self, identity: RequestIdentity
    ) -> list[tuple[ProviderProfileRecord, list[dict[str, object]]]]:
        """列出供应商配置，并附带脱敏后的密钥绑定状态。"""
        self._require_admin(identity)
        profiles = list(
            (
                await self.session.scalars(
                    select(ProviderProfileRecord).where(
                        ProviderProfileRecord.tenant_id == identity.tenant_id
                    )
                )
            ).all()
        )
        output: list[tuple[ProviderProfileRecord, list[dict[str, object]]]] = []
        for profile in profiles:
            bindings: list[dict[str, object]] = []
            if profile.secret_binding_ids:
                records = list(
                    (
                        await self.session.scalars(
                            select(SecretBindingRecord).where(
                                SecretBindingRecord.tenant_id == identity.tenant_id,
                                SecretBindingRecord.id.in_(profile.secret_binding_ids),
                            )
                        )
                    ).all()
                )
                bindings = [
                    {"id": item.id, "name": item.name, "valid": item.valid}
                    for item in records
                ]
            output.append((profile, bindings))
        return output

    async def list_policies(self, identity: RequestIdentity) -> list[PolicyVersionRecord]:
        """返回当前租户的全部策略版本。"""
        self._require_admin(identity)
        return list(
            (
                await self.session.scalars(
                    select(PolicyVersionRecord).where(
                        PolicyVersionRecord.tenant_id == identity.tenant_id
                    )
                )
            ).all()
        )
