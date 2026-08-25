from __future__ import annotations

import asyncio
from datetime import timedelta

from enterprise_rag.domain.types import utc_now
from enterprise_rag.infrastructure.orm import TenantInvitationRecord
from enterprise_rag.infrastructure.telemetry import TelemetryInput, TelemetryService
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def test_platform_tenant_lifecycle_discovery_and_switching(client: TestClient) -> None:
    discovery = client.get("/api/v1/tenants")
    assert discovery.status_code == 200
    assert [item["id"] for item in discovery.json()["items"]] == ["tenant-demo"]

    payload = {
        "name": "Finance workspace",
        "slug": "finance",
        "administrator_external_id": "admin-demo",
        "metadata": {"cost_center": "FIN"},
    }
    created = client.post(
        "/api/v1/platform/tenants",
        json=payload,
        headers={"Idempotency-Key": "create-finance"},
    )
    assert created.status_code == 201, created.text
    tenant = created.json()
    assert tenant["state"] == "provisioning"
    repeated = client.post(
        "/api/v1/platform/tenants",
        json=payload,
        headers={"Idempotency-Key": "create-finance"},
    )
    assert repeated.status_code == 201
    assert repeated.json()["id"] == tenant["id"]

    activated = client.post(
        f"/api/v1/platform/tenants/{tenant['id']}/actions/activate",
        json={"reason": "bootstrap complete"},
        headers={"If-Match": str(tenant["revision"])},
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["state"] == "active"

    context = client.get(
        "/api/v1/tenant/context", headers={"X-Tenant-ID": tenant["id"]}
    )
    assert context.status_code == 200, context.text
    assert context.json()["roles"] == ["administrator"]

    stale = client.patch(
        f"/api/v1/platform/tenants/{tenant['id']}",
        json={"name": "Stale update"},
        headers={"If-Match": "1"},
    )
    assert stale.status_code == 409

    suspended = client.post(
        f"/api/v1/platform/tenants/{tenant['id']}/actions/suspend",
        json={"reason": "security review"},
        headers={"If-Match": str(activated.json()["revision"])},
    )
    assert suspended.status_code == 200
    denied = client.get(
        "/api/v1/tenant/context", headers={"X-Tenant-ID": tenant["id"]}
    )
    assert denied.status_code == 423


def test_tenant_discovery_does_not_enumerate_unknown_selection(client: TestClient) -> None:
    response = client.get(
        "/api/v1/tenant/context", headers={"X-Tenant-ID": "tenant-does-not-exist"}
    )
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Tenant context is unavailable."


def test_member_group_roles_and_last_administrator_protection(client: TestClient) -> None:
    assigned = client.post(
        "/api/v1/tenant/members",
        json={"external_id": "oidc|alice", "display_name": "Alice", "roles": ["reader"]},
        headers={"Idempotency-Key": "assign-alice"},
    )
    assert assigned.status_code == 201, assigned.text
    repeated = client.post(
        "/api/v1/tenant/members",
        json={"external_id": "oidc|alice", "display_name": "Alice", "roles": ["reader"]},
        headers={"Idempotency-Key": "assign-alice"},
    )
    assert repeated.status_code == 201
    assert repeated.json()["id"] == assigned.json()["id"]

    members = client.get("/api/v1/tenant/members").json()["items"]
    current_admin = next(item for item in members if item["external_id"] == "admin-demo")
    blocked = client.patch(
        f"/api/v1/tenant/members/{current_admin['id']}",
        json={"state": "suspended", "reason": "test"},
        headers={"If-Match": str(current_admin["revision"])},
    )
    assert blocked.status_code == 409
    assert "last active" in blocked.json()["error"]["message"]

    promoted = client.patch(
        f"/api/v1/tenant/members/{assigned.json()['id']}",
        json={"roles": ["administrator"], "reason": "backup administrator"},
        headers={"If-Match": str(assigned.json()["revision"])},
    )
    assert promoted.status_code == 200, promoted.text

    group = client.post(
        "/api/v1/tenant/groups",
        json={"external_id": "finance", "display_name": "Finance"},
        headers={"Idempotency-Key": "create-finance-group"},
    )
    assert group.status_code == 201, group.text
    principal_id = next(
        item["principal_id"]
        for item in client.get("/api/v1/tenant/members").json()["items"]
        if item["external_id"] == "oidc|alice"
    )
    link = client.put(
        f"/api/v1/tenant/groups/{group.json()['id']}/members/{principal_id}",
        json={"active": True},
    )
    assert link.status_code == 200, link.text
    role = client.put(
        f"/api/v1/tenant/groups/{group.json()['id']}/roles/operator",
        json={"active": True},
    )
    assert role.status_code == 200, role.text
    alice = next(
        item
        for item in client.get("/api/v1/tenant/members").json()["items"]
        if item["external_id"] == "oidc|alice"
    )
    assert set(alice["effective_roles"]) == {"administrator", "operator"}


def test_invitation_token_is_single_use_and_never_listed(client: TestClient) -> None:
    issued = client.post(
        "/api/v1/tenant/invitations",
        json={"destination": "admin-demo", "roles": ["administrator"]},
    )
    assert issued.status_code == 201, issued.text
    token = issued.json()["token"]
    listed = client.get("/api/v1/tenant/invitations")
    assert listed.status_code == 200
    assert token not in listed.text

    accepted = client.post("/api/v1/invitations/accept", json={"token": token})
    assert accepted.status_code == 200, accepted.text
    reused = client.post("/api/v1/invitations/accept", json={"token": token})
    assert reused.status_code == 404


def test_configuration_versions_quota_and_secret_validation(client: TestClient) -> None:
    effective = client.get("/api/v1/tenant/settings/effective")
    assert effective.status_code == 200, effective.text
    body = effective.json()
    config = body["config"]
    config["quotas"]["members"] = 1
    activated = client.post(
        "/api/v1/tenant/settings/versions",
        json={"config": config, "activate": True},
        headers={"If-Match": str(body["tenant_revision"])},
    )
    assert activated.status_code == 201, activated.text
    assert activated.json()["state"] == "active"

    over_quota = client.post(
        "/api/v1/tenant/members",
        json={"external_id": "oidc|quota-user", "roles": ["reader"]},
    )
    assert over_quota.status_code == 429
    assert over_quota.json()["error"]["category"] == "quota_exceeded"

    invalid = dict(config)
    invalid["provider_policy"] = {"api_key": "raw-secret"}
    rejected = client.post(
        "/api/v1/tenant/settings/versions",
        json={"config": invalid},
        headers={"If-Match": str(activated.json()["version"] + body["tenant_revision"])},
    )
    assert rejected.status_code == 422
    assert "raw-secret" not in rejected.text

    usage = client.get("/api/v1/tenant/usage")
    assert usage.status_code == 200
    members = next(
        item for item in usage.json()["items"] if item["resource"] == "members"
    )
    assert members["used"] == 1
    assert members["limit"] == 1
    assert members["warning"] is True


def test_platform_role_is_separate_from_persisted_tenant_role(client: TestClient) -> None:
    settings = client.app.state.settings
    original = settings.dev_platform_admin
    settings.dev_platform_admin = False
    try:
        denied = client.get("/api/v1/platform/tenants")
        assert denied.status_code == 403
        allowed = client.get("/api/v1/tenant/members")
        assert allowed.status_code == 200
    finally:
        settings.dev_platform_admin = original


def test_expired_invitation_is_rejected_without_enumeration(client: TestClient) -> None:
    issued = client.post(
        "/api/v1/tenant/invitations",
        json={"destination": "expired@example.invalid", "roles": ["reader"], "expires_in_hours": 1},
    )
    assert issued.status_code == 201
    invitation_id = issued.json()["id"]
    token = issued.json()["token"]
    sessions: async_sessionmaker[AsyncSession] = client.app.state.container.sessions

    async def expire() -> None:
        async with sessions() as session:
            invitation = await session.get(TenantInvitationRecord, invitation_id)
            assert invitation is not None
            invitation.expires_at = utc_now() - timedelta(seconds=1)
            await session.commit()

    asyncio.run(expire())
    accepted = client.post("/api/v1/invitations/accept", json={"token": token})
    assert accepted.status_code == 404
    assert token not in accepted.text


def test_configuration_concurrency_and_rollback_as_new_version(client: TestClient) -> None:
    effective = client.get("/api/v1/tenant/settings/effective").json()
    original_version_id = effective["configuration_version_id"]
    config = effective["config"]
    config["locale"] = "en-US"
    draft = client.post(
        "/api/v1/tenant/settings/versions",
        json={"config": config, "activate": False},
        headers={"If-Match": str(effective["tenant_revision"])},
    )
    assert draft.status_code == 201, draft.text
    stale = client.post(
        "/api/v1/tenant/settings/versions",
        json={"config": config, "activate": False},
        headers={"If-Match": str(effective["tenant_revision"])},
    )
    assert stale.status_code == 409
    current = client.get("/api/v1/tenant/settings/effective").json()
    rolled_back = client.post(
        "/api/v1/tenant/settings/rollback",
        json={"source_version_id": original_version_id},
        headers={"If-Match": str(current["tenant_revision"])},
    )
    assert rolled_back.status_code == 201, rolled_back.text
    assert rolled_back.json()["id"] != original_version_id
    assert rolled_back.json()["state"] == "active"


def test_two_tenant_audit_telemetry_evaluation_and_export_isolation(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/v1/platform/tenants",
        json={
            "name": "Tenant B",
            "slug": "tenant-b",
            "administrator_external_id": "admin-demo",
        },
        headers={"Idempotency-Key": "tenant-b-e2e"},
    ).json()
    activated = client.post(
        f"/api/v1/platform/tenants/{created['id']}/actions/activate",
        json={"reason": "e2e"},
        headers={"If-Match": str(created["revision"])},
    )
    assert activated.status_code == 200
    headers_a = {"X-Tenant-ID": "tenant-demo"}
    headers_b = {"X-Tenant-ID": created["id"]}
    space_a = client.post(
        "/api/v1/knowledge-spaces",
        headers={**headers_a, "Idempotency-Key": "space-a-e2e"},
        json={"name": "Only tenant A", "description": "A"},
    ).json()
    space_b = client.post(
        "/api/v1/knowledge-spaces",
        headers={**headers_b, "Idempotency-Key": "space-b-e2e"},
        json={"name": "Only tenant B", "description": "B"},
    ).json()
    export_a = client.get("/api/v1/audit-events/export", headers=headers_a)
    export_b = client.get("/api/v1/audit-events/export", headers=headers_b)
    assert export_a.status_code == export_b.status_code == 200
    assert space_a["id"] in export_a.text and space_b["id"] not in export_a.text
    assert space_b["id"] in export_b.text and space_a["id"] not in export_b.text

    sessions: async_sessionmaker[AsyncSession] = client.app.state.container.sessions

    async def seed_telemetry() -> None:
        async with sessions() as session:
            await TelemetryService(session, False).record(
                TelemetryInput("tenant-demo", "correlation-a", "query", "model", "success")
            )
            await TelemetryService(session, False).record(
                TelemetryInput(created["id"], "correlation-b", "query", "model", "success")
            )
            await session.commit()

    asyncio.run(seed_telemetry())
    telemetry_a = client.get("/api/v1/telemetry/events", headers=headers_a).json()["items"]
    telemetry_b = client.get("/api/v1/telemetry/events", headers=headers_b).json()["items"]
    assert {item["correlation_id"] for item in telemetry_a} >= {"correlation-a"}
    assert "correlation-b" not in {item["correlation_id"] for item in telemetry_a}
    assert {item["correlation_id"] for item in telemetry_b} >= {"correlation-b"}
    assert "correlation-a" not in {item["correlation_id"] for item in telemetry_b}

    dataset_a = client.post(
        "/api/v1/evaluation/datasets",
        headers=headers_a,
        json={"name": "Tenant A set", "items": [{"id": "a", "question": "A?"}]},
    )
    assert dataset_a.status_code == 201
    foreign_versions = client.get(
        f"/api/v1/evaluation/datasets/{dataset_a.json()['id']}/versions",
        headers=headers_b,
    )
    assert foreign_versions.status_code == 200
    assert foreign_versions.json()["items"] == []
