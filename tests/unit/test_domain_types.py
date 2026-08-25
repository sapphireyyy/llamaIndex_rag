import pytest
from enterprise_rag.domain.types import (
    AuthorizationScope,
    InvitationState,
    MembershipState,
    RequestIdentity,
    Role,
    TenantState,
    require_invitation_transition,
    require_membership_transition,
    require_tenant_transition,
    stable_hash,
)


def test_identity_builds_normalized_principal_tokens() -> None:
    identity = RequestIdentity(
        tenant_id="tenant-a",
        user_id="alice",
        groups=("finance", "employees"),
        roles=frozenset({Role.READER}),
    )
    assert identity.principal_tokens == {
        "user:alice",
        "group:finance",
        "group:employees",
    }


def test_authorization_cache_key_changes_with_acl_epoch() -> None:
    base = AuthorizationScope("tenant-a", frozenset({"user:alice"}), frozenset({"space-1"}), 1)
    changed = AuthorizationScope("tenant-a", frozenset({"user:alice"}), frozenset({"space-1"}), 2)
    assert base.cache_key("assistant-v1") != changed.cache_key("assistant-v1")
    assert stable_hash("same") == stable_hash(b"same")


def test_authorization_cache_key_captures_configuration_resource_and_policy_versions() -> None:
    base = AuthorizationScope(
        "tenant-a",
        frozenset({"user:alice"}),
        frozenset({"space-1"}),
        2,
        configuration_version_id="config-1",
        resource_versions=("document-v1",),
        policy_versions=("retrieval-v1",),
    )
    changed = AuthorizationScope(
        "tenant-a",
        frozenset({"user:alice"}),
        frozenset({"space-1"}),
        2,
        configuration_version_id="config-2",
        resource_versions=("document-v1",),
        policy_versions=("retrieval-v1",),
    )
    assert base.cache_key("assistant-v1") != changed.cache_key("assistant-v1")


def test_control_plane_lifecycle_transitions_are_validated() -> None:
    require_tenant_transition(TenantState.ACTIVE, TenantState.SUSPENDED)
    require_membership_transition(MembershipState.SUSPENDED, MembershipState.ACTIVE)
    require_invitation_transition(InvitationState.PENDING, InvitationState.ACCEPTED)

    with pytest.raises(ValueError, match="invalid tenant transition"):
        require_tenant_transition(TenantState.PROVISIONING, TenantState.SUSPENDED)
    with pytest.raises(ValueError, match="invalid membership transition"):
        require_membership_transition(MembershipState.REMOVED, MembershipState.ACTIVE)
    with pytest.raises(ValueError, match="invalid invitation transition"):
        require_invitation_transition(InvitationState.ACCEPTED, InvitationState.REVOKED)
