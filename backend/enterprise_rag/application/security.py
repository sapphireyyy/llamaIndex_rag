"""授权应用服务：校验租户权限，并生成供检索层使用的授权范围。"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_rag.api.errors import AppError
from enterprise_rag.application.tenant_context import require_tenant_permission
from enterprise_rag.domain.types import AuthorizationScope, ErrorCategory, RequestIdentity
from enterprise_rag.infrastructure.orm import (
    KnowledgeSpaceRecord,
    SpaceMembershipRecord,
    TenantRecord,
)


class DatabaseAuthorizationService:
    """从数据库编译资源操作权限和检索 ACL 范围。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """初始化用于跨会话权限查询的数据库会话工厂。"""
        self.session_factory = session_factory

    async def require(
        self,
        identity: RequestIdentity,
        operation: str,
        resource_tenant_id: str,
        resource_id: str,
    ) -> None:
        """验证当前身份能否对目标资源执行指定动作。"""
        del resource_id
        if identity.tenant_id != resource_tenant_id:
            raise AppError(ErrorCategory.NOT_FOUND, "Resource not found.", 404)
        require_tenant_permission(identity, operation)

    async def retrieval_scope(
        self, identity: RequestIdentity, knowledge_space_ids: Sequence[str]
    ) -> AuthorizationScope:
        """编译当前身份在知识空间集合内的检索权限范围。"""
        if not knowledge_space_ids:
            raise AppError(ErrorCategory.FORBIDDEN, "No authorized knowledge space.", 403)
        tokens = sorted(identity.principal_tokens)
        async with self.session_factory() as session:
            tenant = await session.get(TenantRecord, identity.tenant_id)
            if tenant is None or tenant.state != "active":
                raise AppError(
                    ErrorCategory.FORBIDDEN, "Authorization context is unavailable.", 403
                )
            statement = (
                select(KnowledgeSpaceRecord.id)
                .join(
                    SpaceMembershipRecord,
                    SpaceMembershipRecord.knowledge_space_id == KnowledgeSpaceRecord.id,
                )
                .where(
                    KnowledgeSpaceRecord.tenant_id == identity.tenant_id,
                    KnowledgeSpaceRecord.state == "active",
                    KnowledgeSpaceRecord.id.in_(list(knowledge_space_ids)),
                    SpaceMembershipRecord.tenant_id == identity.tenant_id,
                    SpaceMembershipRecord.principal_token.in_(tokens),
                    SpaceMembershipRecord.role.in_(["reader", "editor", "administrator"]),
                )
                .distinct()
            )
            allowed_spaces = frozenset((await session.scalars(statement)).all())
        if not allowed_spaces:
            raise AppError(ErrorCategory.FORBIDDEN, "No authorized knowledge space.", 403)
        return AuthorizationScope(
            tenant_id=identity.tenant_id,
            principal_tokens=identity.principal_tokens,
            knowledge_space_ids=allowed_spaces,
            acl_epoch=identity.authorization_epoch or tenant.acl_epoch,
            configuration_version_id=identity.configuration_version_id,
        )


async def bump_acl_epoch(session: AsyncSession, tenant_id: str) -> int:
    """锁定租户并递增 ACL 版本，令旧授权范围无法继续复用。"""
    tenant = await session.get(TenantRecord, tenant_id, with_for_update=True)
    if tenant is None:
        raise AppError(ErrorCategory.NOT_FOUND, "Resource not found.", 404)
    tenant.acl_epoch += 1
    await session.flush()
    return tenant.acl_epoch
