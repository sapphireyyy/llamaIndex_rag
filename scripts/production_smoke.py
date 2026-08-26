from __future__ import annotations

import asyncio
import json
import os
import uuid
from copy import deepcopy
from typing import Any, cast

import httpx
import jwt
from enterprise_rag.application.worker_context import WorkerContextRejected, validate_worker_context
from enterprise_rag.config import get_settings
from enterprise_rag.domain.types import JobMessage
from enterprise_rag.infrastructure.database import create_engine, create_session_factory


def require(response: httpx.Response, expected: int) -> dict[str, Any]:
    if response.status_code != expected:
        raise RuntimeError(
            f"{response.request.method} {response.request.url.path} returned "
            f"{response.status_code}: {response.text}"
        )
    if not response.content:
        return {}
    return cast(dict[str, Any], response.json())


def parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for block in body.split("\n\n"):
        fields = dict(
            line.split(": ", 1) for line in block.splitlines() if ": " in line
        )
        if "event" in fields and "data" in fields:
            events.append((fields["event"], json.loads(fields["data"])))
    return events


def _injected_access_token() -> str:
    """Return a short-lived browser-flow token without handling user credentials."""
    token = os.getenv("RAG_SMOKE_ACCESS_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "RAG_SMOKE_ACCESS_TOKEN is required; obtain a short-lived token through "
            "the standard browser login flow because the public client disables password grants"
        )
    return token


def _legacy_main() -> None:
    base_url = os.getenv("RAG_SMOKE_BASE_URL", "http://127.0.0.1:8000")
    suffix = uuid.uuid4().hex[:12]

    with httpx.Client(timeout=90.0, trust_env=False) as client:
        token = _injected_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Correlation-ID": f"production-smoke-{suffix}",
        }

        readiness = require(client.get(f"{base_url}/health/ready"), 200)
        if not all(readiness.get("checks", readiness).values()):
            raise RuntimeError(f"readiness check failed: {readiness}")

        unauthorized = client.get(f"{base_url}/api/v1/knowledge-spaces")
        if unauthorized.status_code != 401:
            raise RuntimeError(
                f"protected API accepted an anonymous request: {unauthorized.status_code}"
            )

        space = require(
            client.post(
                f"{base_url}/api/v1/knowledge-spaces",
                headers={**headers, "Idempotency-Key": f"smoke-space-{suffix}"},
                json={
                    "name": f"Production smoke {suffix}",
                    "description": "Production-like end-to-end acceptance data.",
                },
            ),
            201,
        )
        source = require(
            client.post(
                f"{base_url}/api/v1/knowledge-spaces/{space['id']}/data-sources",
                headers=headers,
                json={"kind": "upload", "external_id": f"smoke-upload-{suffix}"},
            ),
            201,
        )
        upload = require(
            client.post(
                f"{base_url}/api/v1/documents/upload",
                headers={**headers, "Idempotency-Key": f"smoke-document-{suffix}"},
                data={"source_id": source["id"]},
                files={
                    "file": (
                        "vpn-policy.txt",
                        b"VPN access requires manager approval and multi-factor authentication.",
                        "text/plain",
                    )
                },
            ),
            202,
        )
        if upload["status"] != "succeeded":
            raise RuntimeError(f"ingestion did not succeed: {upload}")

        assistant = require(
            client.post(
                f"{base_url}/api/v1/assistants",
                headers=headers,
                json={
                    "name": f"Production smoke assistant {suffix}",
                    "draft": {
                        "knowledge_space_ids": [space["id"]],
                        "policy_version_ids": {},
                        "provider_profile_ids": {},
                    },
                },
            ),
            201,
        )
        version_id = assistant["draft"]["id"]
        require(
            client.post(
                f"{base_url}/api/v1/assistants/{assistant['id']}"
                f"/versions/{version_id}/activate",
                headers=headers,
            ),
            200,
        )
        query = client.post(
            f"{base_url}/api/v1/chat/query",
            headers=headers,
            json={
                "assistant_id": assistant["id"],
                "question": "What does VPN access require?",
            },
        )
        if query.status_code != 200:
            raise RuntimeError(f"query failed: {query.status_code}: {query.text}")
        events = parse_sse(query.text)
        terminal = next((data for name, data in events if name == "terminal"), None)
        citation = next((data for name, data in events if name == "citation"), None)
        if terminal != {"status": "success"} or citation is None:
            raise RuntimeError(f"grounded SSE contract failed: {events}")
        citation_source = require(
            client.get(f"{base_url}{citation['source_url']}", headers=headers), 200
        )
        if citation_source["document_id"] != upload["document_id"]:
            raise RuntimeError("citation does not point to the ingested document")

    print(
        json.dumps(
            {
                "status": "passed",
                "readiness": readiness,
                "authentication": "keycloak-oidc",
                "anonymous_access": "denied",
                "ingestion": upload["status"],
                "query": terminal["status"],
                "citation_verified": True,
            },
            indent=2,
            sort_keys=True,
        )
    )


async def _worker_suspension_result(
    tenant_id: str,
    authorization_epoch: int,
    configuration_version_id: str,
) -> str:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    sessions = create_session_factory(engine)
    message = JobMessage(
        f"smoke-worker-{uuid.uuid4().hex[:8]}",
        "ingest",
        tenant_id,
        "production-smoke-worker",
        {
            "authorization_epoch": authorization_epoch,
            "configuration_version_id": configuration_version_id,
            "resource_version_id": "smoke-resource-version",
            "policy_version_ids": [],
        },
    )
    try:
        async with sessions() as session:
            try:
                await validate_worker_context(session, message)
            except WorkerContextRejected as exc:
                return exc.outcome
        raise RuntimeError("worker accepted a job for a suspended tenant")
    finally:
        await engine.dispose()


def _subject_id(token: str) -> str:
    claims = jwt.decode(token, options={"verify_signature": False})
    return str(claims["sub"])


def main() -> None:
    """Exercise the production multi-tenant control and data planes end to end."""
    base_url = os.getenv("RAG_SMOKE_BASE_URL", "http://127.0.0.1:8000")
    suffix = uuid.uuid4().hex[:12]
    with httpx.Client(timeout=90.0, trust_env=False) as client:
        token = _injected_access_token()
        subject_id = _subject_id(token)
        platform_headers = {
            "Authorization": f"Bearer {token}",
            "X-Correlation-ID": f"multi-tenant-smoke-{suffix}",
        }
        readiness = require(client.get(f"{base_url}/health/ready"), 200)
        if not readiness.get("ready"):
            raise RuntimeError(f"readiness check failed: {readiness}")
        if client.get(f"{base_url}/api/v1/tenants").status_code != 401:
            raise RuntimeError("tenant discovery accepted an anonymous request")

        tenants: list[dict[str, Any]] = []
        for label in ("alpha", "beta"):
            created = require(
                client.post(
                    f"{base_url}/api/v1/platform/tenants",
                    headers={
                        **platform_headers,
                        "Idempotency-Key": f"smoke-tenant-{label}-{suffix}",
                    },
                    json={
                        "name": f"Smoke {label} {suffix}",
                        "slug": f"smoke-{label}-{suffix}",
                        "administrator_external_id": subject_id,
                        "metadata": {"acceptance": True},
                    },
                ),
                201,
            )
            tenants.append(
                require(
                    client.post(
                        f"{base_url}/api/v1/platform/tenants/{created['id']}/actions/activate",
                        headers={**platform_headers, "If-Match": str(created["revision"])},
                        json={"reason": "production smoke bootstrap complete"},
                    ),
                    200,
                )
            )
        tenant_a, tenant_b = tenants
        headers_a = {**platform_headers, "X-Tenant-ID": str(tenant_a["id"])}
        headers_b = {**platform_headers, "X-Tenant-ID": str(tenant_b["id"])}

        discovery = require(client.get(f"{base_url}/api/v1/tenants", headers=platform_headers), 200)
        discovered = {item["id"] for item in discovery["items"]}
        if not {tenant_a["id"], tenant_b["id"]}.issubset(discovered):
            raise RuntimeError("tenant discovery did not return both assigned tenants")
        context_a = require(client.get(f"{base_url}/api/v1/tenant/context", headers=headers_a), 200)
        context_b = require(client.get(f"{base_url}/api/v1/tenant/context", headers=headers_b), 200)
        if context_a["tenant_id"] == context_b["tenant_id"]:
            raise RuntimeError("tenant switching did not change persisted context")

        require(
            client.post(
                f"{base_url}/api/v1/tenant/members",
                headers={**headers_a, "Idempotency-Key": f"smoke-member-{suffix}"},
                json={
                    "external_id": f"smoke-reader-{suffix}",
                    "display_name": "Smoke reader",
                    "roles": ["reader"],
                },
            ),
            201,
        )
        effective = require(
            client.get(f"{base_url}/api/v1/tenant/settings/effective", headers=headers_a),
            200,
        )
        config = deepcopy(effective["config"])
        config["feature_flags"]["production_smoke"] = True
        config["quotas"]["members"] = 2
        require(
            client.post(
                f"{base_url}/api/v1/tenant/settings/versions",
                headers={**headers_a, "If-Match": str(effective["tenant_revision"])},
                json={"config": config, "activate": True},
            ),
            201,
        )
        over_quota = client.post(
            f"{base_url}/api/v1/tenant/members",
            headers=headers_a,
            json={"external_id": f"quota-rejected-{suffix}", "roles": ["reader"]},
        )
        if over_quota.status_code != 429:
            raise RuntimeError(f"member quota was not enforced: {over_quota.status_code}")

        space = require(
            client.post(
                f"{base_url}/api/v1/knowledge-spaces",
                headers={**headers_a, "Idempotency-Key": f"smoke-space-{suffix}"},
                json={"name": f"Tenant A knowledge {suffix}", "description": "isolated"},
            ),
            201,
        )
        source = require(
            client.post(
                f"{base_url}/api/v1/knowledge-spaces/{space['id']}/data-sources",
                headers=headers_a,
                json={"kind": "upload", "external_id": f"upload-{suffix}"},
            ),
            201,
        )
        upload = require(
            client.post(
                f"{base_url}/api/v1/documents/upload",
                headers={**headers_a, "Idempotency-Key": f"smoke-document-{suffix}"},
                data={"source_id": source["id"]},
                files={
                    "file": (
                        "vpn-policy.txt",
                        b"VPN access requires manager approval and multi-factor authentication.",
                        "text/plain",
                    )
                },
            ),
            202,
        )
        assistant = require(
            client.post(
                f"{base_url}/api/v1/assistants",
                headers=headers_a,
                json={
                    "name": f"Tenant A assistant {suffix}",
                    "draft": {
                        "knowledge_space_ids": [space["id"]],
                        "policy_version_ids": {},
                        "provider_profile_ids": {},
                    },
                },
            ),
            201,
        )
        version_id = assistant["draft"]["id"]
        require(
            client.post(
                f"{base_url}/api/v1/assistants/{assistant['id']}/versions/{version_id}/activate",
                headers=headers_a,
            ),
            200,
        )
        query = client.post(
            f"{base_url}/api/v1/chat/query",
            headers=headers_a,
            json={"assistant_id": assistant["id"], "question": "What does VPN access require?"},
        )
        if query.status_code != 200:
            raise RuntimeError(f"tenant A query failed: {query.status_code}: {query.text}")
        events = parse_sse(query.text)
        terminal = next((data for name, data in events if name == "terminal"), None)
        citation = next((data for name, data in events if name == "citation"), None)
        if terminal != {"status": "success"} or citation is None:
            raise RuntimeError(f"grounded query contract failed: {events}")
        source_response = require(
            client.get(f"{base_url}{citation['source_url']}", headers=headers_a), 200
        )
        if source_response["document_id"] != upload["document_id"]:
            raise RuntimeError("citation does not reference the tenant A document")
        foreign_preview = client.get(
            f"{base_url}/api/v1/documents/{upload['document_id']}/preview", headers=headers_b
        )
        foreign_citation = client.get(f"{base_url}{citation['source_url']}", headers=headers_b)
        if (
            foreign_preview.status_code not in {403, 404}
            or foreign_citation.status_code not in {403, 404}
        ):
            raise RuntimeError("tenant B accessed tenant A document or citation")

        latest_tenant_a = require(
            client.get(
                f"{base_url}/api/v1/platform/tenants/{tenant_a['id']}",
                headers=platform_headers,
            ),
            200,
        )
        suspended = require(
            client.post(
                f"{base_url}/api/v1/platform/tenants/{tenant_a['id']}/actions/suspend",
                headers={**platform_headers, "If-Match": str(latest_tenant_a["revision"])},
                json={"reason": "production smoke suspension"},
            ),
            200,
        )
        if client.get(f"{base_url}/api/v1/tenant/context", headers=headers_a).status_code != 423:
            raise RuntimeError("suspended tenant remained available through the API")
        worker_outcome = asyncio.run(
            _worker_suspension_result(
                str(tenant_a["id"]),
                int(context_a["authorization_epoch"]),
                str(context_a["configuration_version_id"]),
            )
        )
        require(
            client.post(
                f"{base_url}/api/v1/platform/tenants/{tenant_a['id']}/actions/reactivate",
                headers={**platform_headers, "If-Match": str(suspended["revision"])},
                json={"reason": "production smoke reactivation"},
            ),
            200,
        )
        require(client.get(f"{base_url}/api/v1/tenant/context", headers=headers_a), 200)

    print(
        json.dumps(
            {
                "status": "passed",
                "tenants_created": 2,
                "tenant_switching": "persisted",
                "member_assignment": "passed",
                "configuration_activation": "passed",
                "quota_rejection": "passed",
                "cross_tenant_document_access": "denied",
                "citation_isolation": "passed",
                "suspension": "api_and_worker_rejected",
                "worker_outcome": worker_outcome,
                "reactivation": "passed",
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
