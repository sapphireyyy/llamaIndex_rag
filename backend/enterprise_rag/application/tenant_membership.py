"""Tenant membership, group, role, invitation and recovery services."""

from __future__ import annotations

import secrets
from datetime import UTC, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.api.errors import AppError
from enterprise_rag.application.audit import AuditEventInput, AuditService
from enterprise_rag.application.security import bump_acl_epoch
from enterprise_rag.application.tenant_configuration import TenantQuotaService
from enterprise_rag.application.tenant_context import (
    effective_roles,
    require_platform_permission,
    require_tenant_permission,
)
from enterprise_rag.domain.types import (
    AuthenticatedSubject,
    ErrorCategory,
    InvitationState,
    MembershipState,
    QuotaResource,
    RequestIdentity,
    Role,
    new_id,
    require_invitation_transition,
    require_membership_transition,
    stable_hash,
    utc_now,
)
from enterprise_rag.infrastructure.database import set_transaction_tenant
from enterprise_rag.infrastructure.orm import (
    GroupRecord,
    PrincipalGroupRecord,
    PrincipalRecord,
    TenantGroupRoleRecord,
    TenantInvitationRecord,
    TenantMembershipRecord,
    TenantRecord,
)
from enterprise_rag.infrastructure.queue import OutboxService


def normalize_destination(value: str) -> str:
    """Normalize an invitation destination without exposing whether it already exists."""
    destination = value.strip().casefold()
    if not destination or len(destination) > 320:
        raise AppError(ErrorCategory.INVALID_REQUEST, "Invitation destination is invalid.", 422)
    return destination


def validate_roles(values: list[str]) -> list[str]:
    """Return unique canonical tenant roles or reject the entire assignment."""
    try:
        roles = sorted({Role(value).value for value in values})
    except ValueError as exc:
        raise AppError(ErrorCategory.INVALID_REQUEST, "Tenant role is invalid.", 422) from exc
    if not roles:
        raise AppError(ErrorCategory.INVALID_REQUEST, "At least one tenant role is required.", 422)
    return roles


class TenantMembershipService:
    """Mutate tenant authorization state with revocation-safe invariants."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_members(
        self, identity: RequestIdentity
    ) -> list[tuple[PrincipalRecord, TenantMembershipRecord, frozenset[Role], tuple[str, ...]]]:
        require_tenant_permission(identity, "membership.read")
        rows = (
            await self.session.execute(
                select(PrincipalRecord, TenantMembershipRecord)
                .join(
                    TenantMembershipRecord,
                    (TenantMembershipRecord.tenant_id == PrincipalRecord.tenant_id)
                    & (TenantMembershipRecord.principal_id == PrincipalRecord.id),
                )
                .where(PrincipalRecord.tenant_id == identity.tenant_id)
                .order_by(PrincipalRecord.display_name, PrincipalRecord.id)
            )
        ).all()
        output = []
        for principal, membership in rows:
            roles, groups = await effective_roles(
                self.session,
                identity.tenant_id,
                principal.id,
                membership.direct_roles if membership.state == "active" else [],
            )
            output.append((principal, membership, roles, groups))
        return output

    async def assign_member(
        self,
        identity: RequestIdentity,
        *,
        external_id: str,
        display_name: str,
        roles: list[str],
        correlation_id: str,
        source: str = "local",
    ) -> tuple[PrincipalRecord, TenantMembershipRecord]:
        require_tenant_permission(identity, "membership.write")
        normalized_external_id = external_id.strip()
        if not normalized_external_id:
            raise AppError(ErrorCategory.INVALID_REQUEST, "External subject is required.", 422)
        canonical_roles = validate_roles(roles)
        principal = await self.session.scalar(
            select(PrincipalRecord).where(
                PrincipalRecord.tenant_id == identity.tenant_id,
                PrincipalRecord.external_id == normalized_external_id,
            )
        )
        if principal is None:
            principal = PrincipalRecord(
                id=new_id("principal"),
                tenant_id=identity.tenant_id,
                external_id=normalized_external_id,
                display_name=display_name.strip() or normalized_external_id,
                active=True,
                state="active",
                source=source,
                provenance={"actor": identity.user_id},
            )
            self.session.add(principal)
            await self.session.flush()
        membership = await self.session.scalar(
            select(TenantMembershipRecord).where(
                TenantMembershipRecord.tenant_id == identity.tenant_id,
                TenantMembershipRecord.principal_id == principal.id,
            )
        )
        if membership is None:
            reservation = (
                await TenantQuotaService(self.session).reserve(
                    identity, QuotaResource.MEMBERS, 1
                )
                if identity.configuration_version_id
                else None
            )
            membership = TenantMembershipRecord(
                id=new_id("tenant_membership"),
                tenant_id=identity.tenant_id,
                principal_id=principal.id,
                state="active",
                direct_roles=canonical_roles,
                source=source,
                provenance={"actor": identity.user_id},
                state_reason="assigned by tenant administrator",
            )
            self.session.add(membership)
            if reservation is not None:
                await TenantQuotaService(self.session).release(reservation, consume=True)
        else:
            membership.state = "active"
            membership.direct_roles = canonical_roles
            membership.state_reason = "reassigned by tenant administrator"
            membership.revision += 1
            principal.state = "active"
            principal.active = True
            principal.revision += 1
        await self._authorization_changed(
            identity,
            "membership.assign",
            "tenant_membership",
            membership.id,
            correlation_id,
            {"roles": canonical_roles, "external_id": normalized_external_id},
        )
        return principal, membership

    async def update_member(
        self,
        identity: RequestIdentity,
        membership_id: str,
        *,
        expected_revision: int,
        roles: list[str] | None,
        state: MembershipState | None,
        reason: str,
        correlation_id: str,
    ) -> TenantMembershipRecord:
        require_tenant_permission(identity, "membership.write")
        membership = await self.session.scalar(
            select(TenantMembershipRecord)
            .where(
                TenantMembershipRecord.id == membership_id,
                TenantMembershipRecord.tenant_id == identity.tenant_id,
            )
            .with_for_update()
        )
        if membership is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Resource not found.", 404)
        if membership.revision != expected_revision:
            raise AppError(ErrorCategory.CONFLICT, "Membership revision is stale.", 409)
        if roles is not None:
            membership.direct_roles = validate_roles(roles)
        if state is not None:
            if state not in {
                MembershipState.ACTIVE,
                MembershipState.SUSPENDED,
                MembershipState.REMOVED,
            }:
                raise AppError(ErrorCategory.INVALID_REQUEST, "Membership state is invalid.", 422)
            try:
                require_membership_transition(MembershipState(membership.state), state)
            except ValueError as exc:
                raise AppError(
                    ErrorCategory.CONFLICT, "Membership transition is invalid.", 409
                ) from exc
            membership.state = state.value
        membership.state_reason = reason.strip()
        membership.revision += 1
        await self.session.flush()
        await self._require_active_administrator()
        await self._authorization_changed(
            identity,
            "membership.update",
            "tenant_membership",
            membership.id,
            correlation_id,
            {"state": membership.state, "roles": membership.direct_roles, "reason": reason},
        )
        return membership

    async def list_groups(self, identity: RequestIdentity) -> list[GroupRecord]:
        require_tenant_permission(identity, "membership.read")
        return list(
            (
                await self.session.scalars(
                    select(GroupRecord)
                    .where(GroupRecord.tenant_id == identity.tenant_id)
                    .order_by(GroupRecord.display_name, GroupRecord.id)
                )
            ).all()
        )

    async def create_group(
        self,
        identity: RequestIdentity,
        *,
        external_id: str,
        display_name: str,
        correlation_id: str,
    ) -> GroupRecord:
        require_tenant_permission(identity, "membership.write")
        group = GroupRecord(
            id=new_id("group"),
            tenant_id=identity.tenant_id,
            external_id=external_id.strip(),
            display_name=display_name.strip() or external_id.strip(),
            source="local",
            provenance={"actor": identity.user_id},
        )
        reservation = (
            await TenantQuotaService(self.session).reserve(
                identity, QuotaResource.GROUPS, 1
            )
            if identity.configuration_version_id
            else None
        )
        self.session.add(group)
        if reservation is not None:
            await TenantQuotaService(self.session).release(reservation, consume=True)
        await self._authorization_changed(
            identity,
            "group.create",
            "tenant_group",
            group.id,
            correlation_id,
            {"external_id": group.external_id},
        )
        return group

    async def update_group(
        self,
        identity: RequestIdentity,
        group_id: str,
        *,
        expected_revision: int,
        display_name: str | None,
        state: str | None,
        correlation_id: str,
    ) -> GroupRecord:
        require_tenant_permission(identity, "membership.write")
        group = await self._group(identity.tenant_id, group_id, for_update=True)
        if group.revision != expected_revision:
            raise AppError(ErrorCategory.CONFLICT, "Group revision is stale.", 409)
        if display_name is not None:
            group.display_name = display_name.strip()
        if state is not None:
            if state not in {"active", "suspended", "removed"}:
                raise AppError(ErrorCategory.INVALID_REQUEST, "Group state is invalid.", 422)
            group.state = state
        group.revision += 1
        await self.session.flush()
        await self._require_active_administrator()
        await self._authorization_changed(
            identity,
            "group.update",
            "tenant_group",
            group.id,
            correlation_id,
            {"state": group.state},
        )
        return group

    async def set_group_member(
        self,
        identity: RequestIdentity,
        group_id: str,
        principal_id: str,
        *,
        active: bool,
        correlation_id: str,
    ) -> PrincipalGroupRecord:
        require_tenant_permission(identity, "membership.write")
        await self._group(identity.tenant_id, group_id)
        principal = await self.session.scalar(
            select(PrincipalRecord).where(
                PrincipalRecord.id == principal_id,
                PrincipalRecord.tenant_id == identity.tenant_id,
            )
        )
        if principal is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Resource not found.", 404)
        link = await self.session.get(
            PrincipalGroupRecord, (identity.tenant_id, principal_id, group_id)
        )
        if link is None:
            link = PrincipalGroupRecord(
                tenant_id=identity.tenant_id,
                principal_id=principal_id,
                group_id=group_id,
                state="active" if active else "removed",
                source="local",
                provenance={"actor": identity.user_id},
            )
            self.session.add(link)
        else:
            link.state = "active" if active else "removed"
            link.revision += 1
        await self.session.flush()
        await self._require_active_administrator()
        await self._authorization_changed(
            identity,
            "group.membership.update",
            "principal_group",
            f"{principal_id}:{group_id}",
            correlation_id,
            {"active": active},
        )
        return link

    async def set_group_role(
        self,
        identity: RequestIdentity,
        group_id: str,
        role: str,
        *,
        active: bool,
        correlation_id: str,
    ) -> TenantGroupRoleRecord:
        require_tenant_permission(identity, "membership.write")
        await self._group(identity.tenant_id, group_id)
        canonical = validate_roles([role])[0]
        record = await self.session.scalar(
            select(TenantGroupRoleRecord).where(
                TenantGroupRoleRecord.tenant_id == identity.tenant_id,
                TenantGroupRoleRecord.group_id == group_id,
                TenantGroupRoleRecord.role == canonical,
            )
        )
        if record is None:
            record = TenantGroupRoleRecord(
                id=new_id("group_role"),
                tenant_id=identity.tenant_id,
                group_id=group_id,
                role=canonical,
                state="active" if active else "removed",
                source="local",
                provenance={"actor": identity.user_id},
            )
            self.session.add(record)
        else:
            record.state = "active" if active else "removed"
            record.revision += 1
        await self.session.flush()
        await self._require_active_administrator()
        await self._authorization_changed(
            identity,
            "group.role.update",
            "tenant_group_role",
            record.id,
            correlation_id,
            {"role": canonical, "active": active},
        )
        return record

    async def issue_invitation(
        self,
        identity: RequestIdentity,
        *,
        destination: str,
        roles: list[str],
        expires_in_hours: int,
        correlation_id: str,
    ) -> tuple[TenantInvitationRecord, str]:
        require_tenant_permission(identity, "membership.write")
        normalized = normalize_destination(destination)
        canonical_roles = validate_roles(roles)
        token = secrets.token_urlsafe(32)
        invitation = TenantInvitationRecord(
            id=new_id("invitation"),
            tenant_id=identity.tenant_id,
            destination=normalized,
            token_hash=stable_hash(token),
            roles=canonical_roles,
            state=InvitationState.PENDING.value,
            expires_at=utc_now() + timedelta(hours=min(max(expires_in_hours, 1), 720)),
            created_by_subject=identity.user_id,
        )
        self.session.add(invitation)
        await self._audit(
            identity,
            "invitation.issue",
            "tenant_invitation",
            invitation.id,
            correlation_id,
            {"destination_hash": stable_hash(normalized), "roles": canonical_roles},
        )
        return invitation, token

    async def list_invitations(
        self, identity: RequestIdentity
    ) -> list[TenantInvitationRecord]:
        require_tenant_permission(identity, "membership.read")
        return list(
            (
                await self.session.scalars(
                    select(TenantInvitationRecord)
                    .where(TenantInvitationRecord.tenant_id == identity.tenant_id)
                    .order_by(TenantInvitationRecord.created_at.desc())
                )
            ).all()
        )

    async def revoke_invitation(
        self,
        identity: RequestIdentity,
        invitation_id: str,
        *,
        expected_revision: int,
        correlation_id: str,
    ) -> TenantInvitationRecord:
        require_tenant_permission(identity, "membership.write")
        invitation = await self.session.scalar(
            select(TenantInvitationRecord)
            .where(
                TenantInvitationRecord.id == invitation_id,
                TenantInvitationRecord.tenant_id == identity.tenant_id,
            )
            .with_for_update()
        )
        if invitation is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Resource not found.", 404)
        if invitation.revision != expected_revision:
            raise AppError(ErrorCategory.CONFLICT, "Invitation revision is stale.", 409)
        try:
            require_invitation_transition(
                InvitationState(invitation.state), InvitationState.REVOKED
            )
        except ValueError as exc:
            raise AppError(ErrorCategory.CONFLICT, "Invitation is unavailable.", 409) from exc
        invitation.state = InvitationState.REVOKED.value
        invitation.revision += 1
        await self._audit(
            identity,
            "invitation.revoke",
            "tenant_invitation",
            invitation.id,
            correlation_id,
            {},
        )
        return invitation

    async def accept_invitation(
        self,
        subject: AuthenticatedSubject,
        token: str,
        *,
        correlation_id: str,
    ) -> RequestIdentity:
        token_hash = stable_hash(token)
        invitation = await self.session.scalar(
            select(TenantInvitationRecord)
            .where(TenantInvitationRecord.token_hash == token_hash)
            .with_for_update()
        )
        if invitation is None or invitation.state != InvitationState.PENDING.value:
            raise AppError(ErrorCategory.NOT_FOUND, "Invitation is unavailable.", 404)
        await set_transaction_tenant(self.session, invitation.tenant_id)
        now = utc_now()
        expires_at = invitation.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= now:
            invitation.state = InvitationState.EXPIRED.value
            invitation.revision += 1
            raise AppError(ErrorCategory.NOT_FOUND, "Invitation is unavailable.", 404)
        destination_claim = str(subject.claims.get("email") or subject.subject_id).casefold()
        if destination_claim != invitation.destination:
            raise AppError(ErrorCategory.NOT_FOUND, "Invitation is unavailable.", 404)
        principal = await self.session.scalar(
            select(PrincipalRecord).where(
                PrincipalRecord.tenant_id == invitation.tenant_id,
                PrincipalRecord.external_id == subject.subject_id,
            )
        )
        if principal is None:
            principal = PrincipalRecord(
                id=new_id("principal"),
                tenant_id=invitation.tenant_id,
                external_id=subject.subject_id,
                display_name=str(subject.claims.get("name") or subject.subject_id),
                active=True,
                state="active",
                source="invitation",
                provenance={"invitation_id": invitation.id},
            )
            self.session.add(principal)
            await self.session.flush()
        membership = await self.session.scalar(
            select(TenantMembershipRecord).where(
                TenantMembershipRecord.tenant_id == invitation.tenant_id,
                TenantMembershipRecord.principal_id == principal.id,
            )
        )
        if membership is None:
            membership = TenantMembershipRecord(
                id=new_id("tenant_membership"),
                tenant_id=invitation.tenant_id,
                principal_id=principal.id,
                state="active",
                direct_roles=invitation.roles,
                source="invitation",
                provenance={"invitation_id": invitation.id},
                state_reason="invitation accepted",
            )
            self.session.add(membership)
        else:
            membership.state = "active"
            membership.direct_roles = invitation.roles
            membership.revision += 1
        invitation.state = InvitationState.ACCEPTED.value
        invitation.accepted_at = now
        invitation.accepted_by_principal_id = principal.id
        invitation.revision += 1
        epoch = await bump_acl_epoch(self.session, invitation.tenant_id)
        roles, groups = await effective_roles(
            self.session, invitation.tenant_id, principal.id, membership.direct_roles
        )
        identity = RequestIdentity(
            tenant_id=invitation.tenant_id,
            user_id=subject.subject_id,
            groups=groups,
            roles=roles,
            token_id=subject.token_id,
            principal_id=principal.id,
            membership_id=membership.id,
            authorization_epoch=epoch,
        )
        await self._audit(
            identity,
            "invitation.accept",
            "tenant_invitation",
            invitation.id,
            correlation_id,
            {},
        )
        return identity

    async def recover_administrator(
        self,
        subject: AuthenticatedSubject,
        tenant_id: str,
        *,
        external_id: str,
        display_name: str,
        correlation_id: str,
    ) -> TenantMembershipRecord:
        require_platform_permission(subject, "platform.tenant.recovery")
        tenant = await self.session.get(TenantRecord, tenant_id)
        if tenant is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Tenant not found.", 404)
        await set_transaction_tenant(self.session, tenant_id)
        actor = RequestIdentity(tenant_id=tenant_id, user_id=subject.subject_id)
        principal = await self.session.scalar(
            select(PrincipalRecord).where(
                PrincipalRecord.tenant_id == tenant_id,
                PrincipalRecord.external_id == external_id,
            )
        )
        if principal is None:
            principal = PrincipalRecord(
                id=new_id("principal"),
                tenant_id=tenant_id,
                external_id=external_id,
                display_name=display_name or external_id,
                active=True,
                state="active",
                source="platform_recovery",
                provenance={"actor": subject.subject_id},
            )
            self.session.add(principal)
            await self.session.flush()
        membership = await self.session.scalar(
            select(TenantMembershipRecord).where(
                TenantMembershipRecord.tenant_id == tenant_id,
                TenantMembershipRecord.principal_id == principal.id,
            )
        )
        if membership is None:
            membership = TenantMembershipRecord(
                id=new_id("tenant_membership"),
                tenant_id=tenant_id,
                principal_id=principal.id,
                state="active",
                direct_roles=[Role.ADMINISTRATOR.value],
                source="platform_recovery",
                provenance={"actor": subject.subject_id},
                state_reason="platform administrator recovery",
            )
            self.session.add(membership)
        else:
            membership.state = "active"
            membership.direct_roles = sorted(
                set(membership.direct_roles) | {Role.ADMINISTRATOR.value}
            )
            membership.revision += 1
        await self.session.flush()
        await bump_acl_epoch(self.session, tenant_id)
        await self._audit(
            actor,
            "membership.platform_recovery",
            "tenant_membership",
            membership.id,
            correlation_id,
            {"recovered_subject_hash": stable_hash(external_id)},
        )
        return membership

    async def _group(
        self, tenant_id: str, group_id: str, *, for_update: bool = False
    ) -> GroupRecord:
        statement = select(GroupRecord).where(
            GroupRecord.id == group_id, GroupRecord.tenant_id == tenant_id
        )
        if for_update:
            statement = statement.with_for_update()
        group = await self.session.scalar(statement)
        if group is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Resource not found.", 404)
        return group

    async def _active_administrator_count(self, tenant_id: str) -> int:
        memberships = list(
            (
                await self.session.scalars(
                    select(TenantMembershipRecord).where(
                        TenantMembershipRecord.tenant_id == tenant_id,
                        TenantMembershipRecord.state == "active",
                    )
                )
            ).all()
        )
        count = 0
        for membership in memberships:
            roles, _groups = await effective_roles(
                self.session, tenant_id, membership.principal_id, membership.direct_roles
            )
            if Role.ADMINISTRATOR in roles:
                count += 1
        return count

    async def _require_active_administrator(self) -> None:
        await self.session.flush()
        tenant_id = str(self.session.info.get("tenant_id") or "")
        if not tenant_id:
            changed = next(
                (
                    item
                    for item in self.session.dirty | self.session.new
                    if isinstance(
                        item,
                        (
                            TenantMembershipRecord,
                            GroupRecord,
                            PrincipalGroupRecord,
                            TenantGroupRoleRecord,
                        ),
                    )
                ),
                None,
            )
            tenant_id = str(getattr(changed, "tenant_id", ""))
        if tenant_id and await self._active_administrator_count(tenant_id) == 0:
            raise AppError(
                ErrorCategory.CONFLICT,
                "The last active tenant administrator cannot be removed.",
                409,
            )

    async def _authorization_changed(
        self,
        identity: RequestIdentity,
        action: str,
        resource_type: str,
        resource_id: str,
        correlation_id: str,
        details: dict[str, object],
    ) -> None:
        await self.session.flush()
        epoch = await bump_acl_epoch(self.session, identity.tenant_id)
        await self._audit(
            identity,
            action,
            resource_type,
            resource_id,
            correlation_id,
            {**details, "authorization_epoch": epoch},
        )
        await OutboxService(self.session).add(
            identity.tenant_id,
            "tenant.authorization.changed",
            correlation_id,
            {
                "tenant_id": identity.tenant_id,
                "authorization_epoch": epoch,
                "resource_type": resource_type,
                "resource_id": resource_id,
            },
            priority=100,
        )

    async def _audit(
        self,
        identity: RequestIdentity,
        action: str,
        resource_type: str,
        resource_id: str,
        correlation_id: str,
        details: dict[str, object],
    ) -> None:
        await AuditService(self.session).record(
            identity,
            AuditEventInput(
                action,
                resource_type,
                resource_id,
                "allowed",
                "success",
                correlation_id,
                details,
            ),
        )
