"""知识库应用服务：执行知识空间、成员和数据源的业务规则。"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.api.errors import AppError
from enterprise_rag.application.audit import AuditEventInput, AuditService
from enterprise_rag.application.security import bump_acl_epoch
from enterprise_rag.application.tenant_configuration import TenantQuotaService
from enterprise_rag.domain.types import (
    ErrorCategory,
    QuotaResource,
    RequestIdentity,
    Role,
    new_id,
)
from enterprise_rag.infrastructure.orm import (
    DataSourceRecord,
    KnowledgeSpaceRecord,
    PolicyVersionRecord,
    SpaceMembershipRecord,
)
from enterprise_rag.infrastructure.queue import OutboxService


class KnowledgeService:
    """负责知识空间、成员和数据源的权限化业务操作。"""

    def __init__(self, session: AsyncSession) -> None:
        """保存当前事务会话，所有知识库变更在调用方提交。"""
        self.session = session

    @staticmethod
    def _require_admin(identity: RequestIdentity) -> None:
        """限制空间和成员管理操作只能由管理员执行。"""
        if Role.ADMINISTRATOR not in identity.roles:
            raise AppError(ErrorCategory.FORBIDDEN, "Operation is not permitted.", 403)

    @staticmethod
    def _require_editor(identity: RequestIdentity) -> None:
        """限制数据源变更操作只能由管理员或编辑者执行。"""
        if not identity.roles.intersection({Role.ADMINISTRATOR, Role.EDITOR}):
            raise AppError(ErrorCategory.FORBIDDEN, "Operation is not permitted.", 403)

    async def create_space(
        self,
        identity: RequestIdentity,
        name: str,
        description: str,
        correlation_id: str,
    ) -> KnowledgeSpaceRecord:
        """创建空间和创建者管理员成员，并记录审计事件。"""
        self._require_admin(identity)
        existing = await self.session.scalar(
            select(KnowledgeSpaceRecord).where(
                KnowledgeSpaceRecord.tenant_id == identity.tenant_id,
                KnowledgeSpaceRecord.name == name,
            )
        )
        if existing:
            raise AppError(ErrorCategory.CONFLICT, "A knowledge space with this name exists.", 409)
        reservation = (
            await TenantQuotaService(self.session).reserve(
                identity, QuotaResource.KNOWLEDGE_SPACES, 1
            )
            if identity.configuration_version_id
            else None
        )
        space = KnowledgeSpaceRecord(
            id=new_id("space"),
            tenant_id=identity.tenant_id,
            name=name.strip(),
            description=description.strip(),
        )
        self.session.add(space)
        self.session.add(
            SpaceMembershipRecord(
                id=new_id("member"),
                tenant_id=identity.tenant_id,
                knowledge_space_id=space.id,
                principal_token=f"user:{identity.user_id}",
                role="administrator",
            )
        )
        if reservation is not None:
            await TenantQuotaService(self.session).release(reservation, consume=True)
        await AuditService(self.session).record(
            identity,
            AuditEventInput(
                "knowledge.create",
                "knowledge_space",
                space.id,
                "allowed",
                "success",
                correlation_id,
                {"name": space.name},
            ),
        )
        await self.session.flush()
        return space

    async def list_spaces(self, identity: RequestIdentity) -> list[KnowledgeSpaceRecord]:
        """管理员读取租户全部空间，其他角色只读取有成员关系的活动空间。"""
        if Role.ADMINISTRATOR in identity.roles:
            statement = select(KnowledgeSpaceRecord).where(
                KnowledgeSpaceRecord.tenant_id == identity.tenant_id
            )
        else:
            statement = (
                select(KnowledgeSpaceRecord)
                .join(
                    SpaceMembershipRecord,
                    SpaceMembershipRecord.knowledge_space_id == KnowledgeSpaceRecord.id,
                )
                .where(
                    KnowledgeSpaceRecord.tenant_id == identity.tenant_id,
                    KnowledgeSpaceRecord.state == "active",
                    SpaceMembershipRecord.principal_token.in_(identity.principal_tokens),
                )
                .distinct()
            )
        return list((await self.session.scalars(statement)).all())

    async def get_space(
        self, identity: RequestIdentity, space_id: str, for_update: bool = False
    ) -> KnowledgeSpaceRecord:
        """按租户读取空间，可选加行锁以保护后续更新。"""
        statement = select(KnowledgeSpaceRecord).where(
            KnowledgeSpaceRecord.id == space_id,
            KnowledgeSpaceRecord.tenant_id == identity.tenant_id,
        )
        if for_update:
            statement = statement.with_for_update()
        space = await self.session.scalar(statement)
        if space is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Resource not found.", 404)
        return space

    async def update_space(
        self,
        identity: RequestIdentity,
        space_id: str,
        name: str | None,
        description: str | None,
        default_policy_ids: dict[str, str] | None,
    ) -> KnowledgeSpaceRecord:
        """修改空间信息，并校验默认策略全部属于租户且处于活动状态。"""
        self._require_admin(identity)
        space = await self.get_space(identity, space_id, for_update=True)
        if name is not None:
            space.name = name.strip()
        if description is not None:
            space.description = description.strip()
        if default_policy_ids is not None:
            policy_ids = set(default_policy_ids.values())
            count = 0
            if policy_ids:
                count = (
                    await self.session.scalar(
                        select(func.count(PolicyVersionRecord.id)).where(
                            PolicyVersionRecord.tenant_id == identity.tenant_id,
                            PolicyVersionRecord.id.in_(policy_ids),
                            PolicyVersionRecord.state == "active",
                        )
                    )
                    or 0
                )
            if count != len(policy_ids):
                raise AppError(
                    ErrorCategory.INVALID_REQUEST,
                    "Default policies must be active and tenant-owned.",
                    422,
                )
            space.default_policy_ids = default_policy_ids
        await self.session.flush()
        return space

    async def set_state(
        self,
        identity: RequestIdentity,
        space_id: str,
        state: str,
        correlation_id: str,
    ) -> KnowledgeSpaceRecord:
        """切换空间活动状态，同时递增 ACL 版本使检索缓存失效。"""
        self._require_admin(identity)
        if state not in {"active", "archived"}:
            raise AppError(ErrorCategory.INVALID_REQUEST, "Invalid knowledge-space state.", 422)
        space = await self.get_space(identity, space_id, for_update=True)
        space.state = state
        await bump_acl_epoch(self.session, identity.tenant_id)
        await AuditService(self.session).record(
            identity,
            AuditEventInput(
                f"knowledge.{state}",
                "knowledge_space",
                space.id,
                "allowed",
                "success",
                correlation_id,
                {},
            ),
        )
        return space

    async def set_membership(
        self,
        identity: RequestIdentity,
        space_id: str,
        principal_token: str,
        role: str | None,
    ) -> SpaceMembershipRecord | None:
        """设置成员角色或删除成员，并递增 ACL 版本。"""
        self._require_admin(identity)
        await self.get_space(identity, space_id)
        membership = await self.session.scalar(
            select(SpaceMembershipRecord).where(
                SpaceMembershipRecord.knowledge_space_id == space_id,
                SpaceMembershipRecord.principal_token == principal_token,
            )
        )
        if role is None:
            if membership:
                await self.session.delete(membership)
                await bump_acl_epoch(self.session, identity.tenant_id)
            return None
        if role not in {"administrator", "editor", "reader"}:
            raise AppError(ErrorCategory.INVALID_REQUEST, "Invalid membership role.", 422)
        if membership is None:
            membership = SpaceMembershipRecord(
                id=new_id("member"),
                tenant_id=identity.tenant_id,
                knowledge_space_id=space_id,
                principal_token=principal_token,
                role=role,
            )
            self.session.add(membership)
        else:
            membership.role = role
        await bump_acl_epoch(self.session, identity.tenant_id)
        await self.session.flush()
        return membership

    async def add_source(
        self,
        identity: RequestIdentity,
        space_id: str,
        kind: str,
        external_id: str,
        config: dict[str, object],
    ) -> DataSourceRecord:
        """在授权空间中登记数据源，后续由摄取流程处理其内容。"""
        self._require_editor(identity)
        await self.get_space(identity, space_id)
        source = DataSourceRecord(
            id=new_id("source"),
            tenant_id=identity.tenant_id,
            knowledge_space_id=space_id,
            kind=kind,
            external_id=external_id,
            config=config,
        )
        self.session.add(source)
        await self.session.flush()
        return source

    async def list_memberships(
        self, identity: RequestIdentity, space_id: str
    ) -> list[SpaceMembershipRecord]:
        """返回空间成员列表；该操作需要管理员权限。"""
        self._require_admin(identity)
        await self.get_space(identity, space_id)
        return list(
            (
                await self.session.scalars(
                    select(SpaceMembershipRecord).where(
                        SpaceMembershipRecord.knowledge_space_id == space_id,
                        SpaceMembershipRecord.tenant_id == identity.tenant_id,
                    )
                )
            ).all()
        )

    async def list_sources(
        self, identity: RequestIdentity, space_id: str
    ) -> list[DataSourceRecord]:
        """返回指定空间的数据源，先验证当前身份可访问该空间。"""
        await self.get_space(identity, space_id)
        return list(
            (
                await self.session.scalars(
                    select(DataSourceRecord).where(
                        DataSourceRecord.knowledge_space_id == space_id,
                        DataSourceRecord.tenant_id == identity.tenant_id,
                    )
                )
            ).all()
        )

    async def set_source_enabled(
        self, identity: RequestIdentity, source_id: str, enabled: bool
    ) -> DataSourceRecord:
        """切换数据源是否启用，但不直接删除其历史记录。"""
        self._require_editor(identity)
        source = await self.session.scalar(
            select(DataSourceRecord).where(
                DataSourceRecord.id == source_id,
                DataSourceRecord.tenant_id == identity.tenant_id,
            )
        )
        if source is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Resource not found.", 404)
        source.enabled = enabled
        await self.session.flush()
        return source

    async def remove_source(
        self, identity: RequestIdentity, source_id: str, correlation_id: str
    ) -> None:
        """软停用数据源，并通过 outbox 发布索引清理任务。"""
        self._require_editor(identity)
        source = await self.session.scalar(
            select(DataSourceRecord).where(
                DataSourceRecord.id == source_id,
                DataSourceRecord.tenant_id == identity.tenant_id,
            )
        )
        if source is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Resource not found.", 404)
        source.enabled = False
        await OutboxService(self.session).add(
            identity.tenant_id,
            "source.remove",
            correlation_id,
            {
                "source_id": source.id,
                "knowledge_space_id": source.knowledge_space_id,
                "authorization_epoch": identity.authorization_epoch,
                "configuration_version_id": identity.configuration_version_id,
                "resource_version_id": source.id,
                "policy_version_ids": [],
            },
            priority=100,
        )
