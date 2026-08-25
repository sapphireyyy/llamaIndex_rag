from fastapi.testclient import TestClient


def test_knowledge_space_lifecycle_and_configuration(client: TestClient) -> None:
    created = client.post(
        "/api/v1/knowledge-spaces",
        json={"name": "General", "description": "Shared knowledge"},
    )
    assert created.status_code == 201
    space_id = created.json()["id"]

    membership = client.put(
        f"/api/v1/knowledge-spaces/{space_id}/memberships",
        json={"principal_token": "group:employees", "role": "reader"},
    )
    assert membership.status_code == 200
    source = client.post(
        f"/api/v1/knowledge-spaces/{space_id}/data-sources",
        json={"kind": "upload", "external_id": "manual"},
    )
    assert source.status_code == 201
    source_id = source.json()["id"]
    assert client.patch(
        f"/api/v1/data-sources/{source_id}", json={"enabled": False}
    ).status_code == 200

    policy = client.post(
        "/api/v1/policies",
        json={"kind": "retrieval", "config": {"top_k": 8}, "activate": True},
    )
    assert policy.status_code == 201
    profile = client.post(
        "/api/v1/provider-profiles",
        json={
            "name": "Built-in model",
            "kind": "model",
            "adapter": "extractive",
            "config": {"required_capabilities": ["grounded-context"]},
        },
    )
    assert profile.status_code == 201
    assistant = client.post(
        "/api/v1/assistants",
        json={
            "name": "General assistant",
            "draft": {
                "knowledge_space_ids": [space_id],
                "policy_version_ids": {"retrieval": policy.json()["id"]},
                "provider_profile_ids": {"model": profile.json()["id"]},
            },
        },
    )
    assert assistant.status_code == 201
    assistant_id = assistant.json()["id"]
    version_id = assistant.json()["draft"]["id"]
    activated = client.post(
        f"/api/v1/assistants/{assistant_id}/versions/{version_id}/activate"
    )
    assert activated.status_code == 200
    assert activated.json()["state"] == "active"

    second = client.post(
        f"/api/v1/assistants/{assistant_id}/versions",
        json={
            "knowledge_space_ids": [space_id],
            "policy_version_ids": {"retrieval": policy.json()["id"]},
            "provider_profile_ids": {"model": profile.json()["id"]},
        },
    )
    assert second.status_code == 201
    assert second.json()["config_hash"] == activated.json()["config_hash"]
    assert client.post(
        f"/api/v1/assistants/{assistant_id}/versions/{second.json()['id']}/activate"
    ).status_code == 200
    rollback = client.post(
        f"/api/v1/assistants/{assistant_id}/versions/{version_id}/activate"
    )
    assert rollback.status_code == 200
    history = client.get(f"/api/v1/assistants/{assistant_id}/versions")
    assert history.status_code == 200
    assert [item["version"] for item in history.json()["items"]] == [2, 1]

    archived = client.post(f"/api/v1/knowledge-spaces/{space_id}/actions/archive")
    assert archived.status_code == 200
    assert archived.json()["state"] == "archived"
    restored = client.post(f"/api/v1/knowledge-spaces/{space_id}/actions/restore")
    assert restored.status_code == 200
    assert restored.json()["state"] == "active"


def test_incompatible_provider_and_secret_values_are_not_exposed(client: TestClient) -> None:
    invalid = client.post(
        "/api/v1/provider-profiles",
        json={"name": "Unknown", "kind": "model", "adapter": "missing"},
    )
    assert invalid.status_code == 422
    profiles = client.get("/api/v1/provider-profiles")
    assert profiles.status_code == 200
    assert "reference" not in profiles.text
    assert "secret" not in profiles.text.lower() or "secret_bindings" in profiles.text
