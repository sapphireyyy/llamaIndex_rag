"""Tenant member, group, role, invitation and platform recovery APIs."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Header, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_rag.api.auth import Identity, PlatformSubject, Subject
from enterprise_rag.api.errors import AppError
from enterprise_rag.application.tenant_membership import TenantMembershipService
from enterprise_rag.domain.types import ErrorCategory, MembershipState, new_id, stable_json_hash
from enterprise_rag.infrastructure.orm import (
    GroupRecord,
    IdempotencyRecord,
    TenantMembershipRecord,
)

router = APIRouter(prefix="/api/v1", tags=["tenant-membership"])


class MemberAssignInput(BaseModel):
    external_id: str = Field(min_length=1, max_length=255)
    display_name: str = Field(default="", max_length=255)
    roles: list[str] = Field(min_length=1, max_length=5)


class MemberUpdateInput(BaseModel):
    roles: list[str] | None = Field(default=None, min_length=1, max_length=5)
    state: MembershipState | None = None
    reason: str = Field(default="", max_length=2_000)


class GroupCreateInput(BaseModel):
    external_id: str = Field(min_length=1, max_length=255)
    display_name: str = Field(default="", max_length=255)


class GroupUpdateInput(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    state: str | None = Field(default=None, pattern=r"^(active|suspended|removed)$")


class ActiveInput(BaseModel):
    active: bool = True


class InvitationCreateInput(BaseModel):
    destination: str = Field(min_length=1, max_length=320)
    roles: list[str] = Field(min_length=1, max_length=5)
    expires_in_hours: int = Field(default=72, ge=1, le=720)


class InvitationAcceptInput(BaseModel):
    token: str = Field(min_length=16, max_length=512)


class RecoveryInput(BaseModel):
    external_id: str = Field(min_length=1, max_length=255)
    display_name: str = Field(default="", max_length=255)


def _sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    sessions = request.app.state.container.sessions
    if sessions is None:
        raise AppError(ErrorCategory.DEPENDENCY_UNAVAILABLE, "Membership service unavailable.", 503)
    return cast(async_sessionmaker[AsyncSession], sessions)


def _revision(value: str | None) -> int:
    if value is None:
        raise AppError("precondition_required", "If-Match revision is required.", 428)
    try:
        revision = int(value.strip().strip('"'))
    except ValueError as exc:
        raise AppError(ErrorCategory.INVALID_REQUEST, "If-Match revision is invalid.", 422) from exc
    if revision < 1:
        raise AppError(ErrorCategory.INVALID_REQUEST, "If-Match revision is invalid.", 422)
    return revision


async def _idempotent_id(
    session: AsyncSession,
    tenant_id: str,
    operation: str,
    key: str | None,
    payload: dict[str, object],
) -> str | None:
    if not key:
        return None
    request_hash = stable_json_hash(payload)
    record = await session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.tenant_id == tenant_id,
            IdempotencyRecord.operation == operation,
            IdempotencyRecord.key == key,
        )
    )
    if record is None:
        return None
    if record.request_hash != request_hash:
        raise AppError(ErrorCategory.CONFLICT, "Idempotency key was reused.", 409)
    return record.response_body.decode("utf-8") if record.response_body else ""


def _remember_idempotency(
    session: AsyncSession,
    tenant_id: str,
    operation: str,
    key: str | None,
    payload: dict[str, object],
    entity_id: str,
) -> None:
    if key:
        session.add(
            IdempotencyRecord(
                id=new_id("idempotency"),
                tenant_id=tenant_id,
                operation=operation,
                key=key,
                request_hash=stable_json_hash(payload),
                response_status=201,
                response_body=entity_id.encode("utf-8"),
            )
        )


def _membership_view(record: TenantMembershipRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "principal_id": record.principal_id,
        "state": record.state,
        "direct_roles": record.direct_roles,
        "revision": record.revision,
        "source": record.source,
        "state_reason": record.state_reason,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _group_view(record: GroupRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "external_id": record.external_id,
        "display_name": record.display_name,
        "state": record.state,
        "revision": record.revision,
        "source": record.source,
    }


@router.get("/tenant/members")
async def list_members(request: Request, identity: Identity) -> dict[str, object]:
    async with _sessions(request)() as session:
        records = await TenantMembershipService(session).list_members(identity)
        return {
            "items": [
                {
                    **_membership_view(membership),
                    "external_id": principal.external_id,
                    "display_name": principal.display_name,
                    "principal_state": principal.state,
                    "effective_roles": sorted(role.value for role in roles),
                    "groups": groups,
                }
                for principal, membership, roles, groups in records
            ]
        }


@router.post("/tenant/members", status_code=201)
async def assign_member(
    request: Request,
    payload: MemberAssignInput,
    identity: Identity,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, object]:
    payload_dict = payload.model_dump(mode="json")
    async with _sessions(request)() as session:
        previous_id = await _idempotent_id(
            session,
            identity.tenant_id,
            "tenant.member.assign",
            idempotency_key,
            payload_dict,
        )
        if previous_id:
            existing = await session.scalar(
                select(TenantMembershipRecord).where(
                    TenantMembershipRecord.id == previous_id,
                    TenantMembershipRecord.tenant_id == identity.tenant_id,
                )
            )
            if existing is not None and existing.tenant_id == identity.tenant_id:
                return _membership_view(existing)
        _principal, membership = await TenantMembershipService(session).assign_member(
            identity,
            external_id=payload.external_id,
            display_name=payload.display_name,
            roles=payload.roles,
            correlation_id=request.state.correlation_id,
        )
        _remember_idempotency(
            session,
            identity.tenant_id,
            "tenant.member.assign",
            idempotency_key,
            payload_dict,
            membership.id,
        )
        await session.commit()
        return _membership_view(membership)


@router.patch("/tenant/members/{membership_id}")
async def update_member(
    request: Request,
    membership_id: str,
    payload: MemberUpdateInput,
    identity: Identity,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, object]:
    async with _sessions(request)() as session:
        membership = await TenantMembershipService(session).update_member(
            identity,
            membership_id,
            expected_revision=_revision(if_match),
            roles=payload.roles,
            state=payload.state,
            reason=payload.reason,
            correlation_id=request.state.correlation_id,
        )
        await session.commit()
        return _membership_view(membership)


@router.get("/tenant/groups")
async def list_groups(request: Request, identity: Identity) -> dict[str, object]:
    async with _sessions(request)() as session:
        records = await TenantMembershipService(session).list_groups(identity)
        return {"items": [_group_view(record) for record in records]}


@router.post("/tenant/groups", status_code=201)
async def create_group(
    request: Request,
    payload: GroupCreateInput,
    identity: Identity,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, object]:
    payload_dict = payload.model_dump(mode="json")
    async with _sessions(request)() as session:
        previous_id = await _idempotent_id(
            session,
            identity.tenant_id,
            "tenant.group.create",
            idempotency_key,
            payload_dict,
        )
        if previous_id:
            existing = await session.scalar(
                select(GroupRecord).where(
                    GroupRecord.id == previous_id,
                    GroupRecord.tenant_id == identity.tenant_id,
                )
            )
            if existing is not None and existing.tenant_id == identity.tenant_id:
                return _group_view(existing)
        group = await TenantMembershipService(session).create_group(
            identity,
            external_id=payload.external_id,
            display_name=payload.display_name,
            correlation_id=request.state.correlation_id,
        )
        _remember_idempotency(
            session,
            identity.tenant_id,
            "tenant.group.create",
            idempotency_key,
            payload_dict,
            group.id,
        )
        await session.commit()
        return _group_view(group)


@router.patch("/tenant/groups/{group_id}")
async def update_group(
    request: Request,
    group_id: str,
    payload: GroupUpdateInput,
    identity: Identity,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, object]:
    async with _sessions(request)() as session:
        group = await TenantMembershipService(session).update_group(
            identity,
            group_id,
            expected_revision=_revision(if_match),
            display_name=payload.display_name,
            state=payload.state,
            correlation_id=request.state.correlation_id,
        )
        await session.commit()
        return _group_view(group)


@router.delete("/tenant/groups/{group_id}", status_code=204)
async def delete_group(
    request: Request,
    group_id: str,
    identity: Identity,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Response:
    async with _sessions(request)() as session:
        await TenantMembershipService(session).update_group(
            identity,
            group_id,
            expected_revision=_revision(if_match),
            display_name=None,
            state="removed",
            correlation_id=request.state.correlation_id,
        )
        await session.commit()
    return Response(status_code=204)


@router.put("/tenant/groups/{group_id}/members/{principal_id}")
async def set_group_member(
    request: Request,
    group_id: str,
    principal_id: str,
    payload: ActiveInput,
    identity: Identity,
) -> dict[str, object]:
    async with _sessions(request)() as session:
        record = await TenantMembershipService(session).set_group_member(
            identity,
            group_id,
            principal_id,
            active=payload.active,
            correlation_id=request.state.correlation_id,
        )
        await session.commit()
        return {
            "tenant_id": record.tenant_id,
            "group_id": record.group_id,
            "principal_id": record.principal_id,
            "state": record.state,
            "revision": record.revision,
        }


@router.put("/tenant/groups/{group_id}/roles/{role}")
async def set_group_role(
    request: Request,
    group_id: str,
    role: str,
    payload: ActiveInput,
    identity: Identity,
) -> dict[str, object]:
    async with _sessions(request)() as session:
        record = await TenantMembershipService(session).set_group_role(
            identity,
            group_id,
            role,
            active=payload.active,
            correlation_id=request.state.correlation_id,
        )
        await session.commit()
        return {
            "id": record.id,
            "group_id": record.group_id,
            "role": record.role,
            "state": record.state,
            "revision": record.revision,
        }


@router.get("/tenant/invitations")
async def list_invitations(request: Request, identity: Identity) -> dict[str, object]:
    async with _sessions(request)() as session:
        records = await TenantMembershipService(session).list_invitations(identity)
        return {
            "items": [
                {
                    "id": item.id,
                    "destination": item.destination,
                    "roles": item.roles,
                    "state": item.state,
                    "revision": item.revision,
                    "expires_at": item.expires_at,
                    "accepted_at": item.accepted_at,
                }
                for item in records
            ]
        }


@router.post("/tenant/invitations", status_code=201)
async def issue_invitation(
    request: Request, payload: InvitationCreateInput, identity: Identity
) -> dict[str, object]:
    async with _sessions(request)() as session:
        invitation, token = await TenantMembershipService(session).issue_invitation(
            identity,
            destination=payload.destination,
            roles=payload.roles,
            expires_in_hours=payload.expires_in_hours,
            correlation_id=request.state.correlation_id,
        )
        await session.commit()
        return {
            "id": invitation.id,
            "destination": invitation.destination,
            "roles": invitation.roles,
            "state": invitation.state,
            "revision": invitation.revision,
            "expires_at": invitation.expires_at,
            "token": token,
        }


@router.post("/tenant/invitations/{invitation_id}/revoke")
async def revoke_invitation(
    request: Request,
    invitation_id: str,
    identity: Identity,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, object]:
    async with _sessions(request)() as session:
        invitation = await TenantMembershipService(session).revoke_invitation(
            identity,
            invitation_id,
            expected_revision=_revision(if_match),
            correlation_id=request.state.correlation_id,
        )
        await session.commit()
        return {"id": invitation.id, "state": invitation.state, "revision": invitation.revision}


@router.post("/invitations/accept")
async def accept_invitation(
    request: Request, payload: InvitationAcceptInput, subject: Subject
) -> dict[str, object]:
    async with _sessions(request)() as session:
        identity = await TenantMembershipService(session).accept_invitation(
            subject, payload.token, correlation_id=request.state.correlation_id
        )
        await session.commit()
        return {
            "tenant_id": identity.tenant_id,
            "membership_id": identity.membership_id,
            "roles": sorted(role.value for role in identity.roles),
            "authorization_epoch": identity.authorization_epoch,
        }


@router.post("/platform/tenants/{tenant_id}/recovery/administrator")
async def recover_administrator(
    request: Request,
    tenant_id: str,
    payload: RecoveryInput,
    subject: PlatformSubject,
) -> dict[str, object]:
    async with _sessions(request)() as session:
        membership = await TenantMembershipService(session).recover_administrator(
            subject,
            tenant_id,
            external_id=payload.external_id,
            display_name=payload.display_name,
            correlation_id=request.state.correlation_id,
        )
        await session.commit()
        return _membership_view(membership)
