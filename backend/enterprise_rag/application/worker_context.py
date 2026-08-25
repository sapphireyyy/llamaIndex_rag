"""Worker tenant snapshot validation and quarantine outcomes."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.domain.types import JobMessage
from enterprise_rag.infrastructure.orm import TenantRecord


@dataclass(frozen=True, slots=True)
class WorkerTenantSnapshot:
    tenant_id: str
    authorization_epoch: int
    configuration_version_id: str
    resource_version_id: str | None
    policy_version_ids: tuple[str, ...]


class WorkerContextRejected(RuntimeError):
    """Stable worker outcome for quarantined, stale or inactive tenant work."""

    def __init__(self, outcome: str) -> None:
        super().__init__(outcome)
        self.outcome = outcome


async def validate_worker_context(
    session: AsyncSession,
    message: JobMessage,
    *,
    require_resource_version: bool = False,
) -> WorkerTenantSnapshot:
    """Revalidate current lifecycle and immutable job snapshot before side effects."""
    if message.payload.get("tenant_id") != message.tenant_id:
        raise WorkerContextRejected("tenant_context_quarantined")
    tenant = await session.get(TenantRecord, message.tenant_id)
    if tenant is None:
        raise WorkerContextRejected("tenant_context_quarantined")
    if tenant.state != "active":
        raise WorkerContextRejected(f"tenant_{tenant.state}_rejected")
    authorization_epoch = message.payload.get("authorization_epoch")
    configuration_version_id = message.payload.get("configuration_version_id")
    if authorization_epoch is None or not configuration_version_id:
        raise WorkerContextRejected("tenant_context_quarantined")
    if int(authorization_epoch) != tenant.acl_epoch:
        raise WorkerContextRejected("authorization_snapshot_stale")
    if str(configuration_version_id) != tenant.active_config_version_id:
        raise WorkerContextRejected("configuration_snapshot_stale")
    resource_version_id = message.payload.get("resource_version_id")
    if require_resource_version and not resource_version_id:
        raise WorkerContextRejected("resource_snapshot_missing")
    raw_policy_versions = message.payload.get("policy_version_ids", [])
    if not isinstance(raw_policy_versions, list):
        raise WorkerContextRejected("policy_snapshot_invalid")
    return WorkerTenantSnapshot(
        tenant_id=tenant.id,
        authorization_epoch=tenant.acl_epoch,
        configuration_version_id=str(tenant.active_config_version_id),
        resource_version_id=str(resource_version_id) if resource_version_id else None,
        policy_version_ids=tuple(sorted(str(value) for value in raw_policy_versions)),
    )
