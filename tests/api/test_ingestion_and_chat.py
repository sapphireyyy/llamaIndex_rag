import json

from fastapi.testclient import TestClient


def _events(response_text: str) -> list[tuple[str, dict[str, object]]]:
    events = []
    for block in response_text.split("\n\n"):
        lines = dict(
            line.split(": ", 1) for line in block.splitlines() if ": " in line
        )
        if "event" in lines and "data" in lines:
            events.append((lines["event"], json.loads(lines["data"])))
    return events


def _space_and_source(client: TestClient) -> tuple[str, str]:
    space = client.post(
        "/api/v1/knowledge-spaces",
        json={"name": "Employee handbook", "description": "Policy documents"},
    )
    assert space.status_code == 201
    source = client.post(
        f"/api/v1/knowledge-spaces/{space.json()['id']}/data-sources",
        json={"kind": "upload", "external_id": "manual-upload"},
    )
    assert source.status_code == 201
    return space.json()["id"], source.json()["id"]


def _assistant(client: TestClient, space_id: str) -> str:
    created = client.post(
        "/api/v1/assistants",
        json={
            "name": "Handbook assistant",
            "draft": {
                "knowledge_space_ids": [space_id],
                "policy_version_ids": {},
                "provider_profile_ids": {},
            },
        },
    )
    assert created.status_code == 201
    assistant_id = created.json()["id"]
    version_id = created.json()["draft"]["id"]
    activated = client.post(
        f"/api/v1/assistants/{assistant_id}/versions/{version_id}/activate"
    )
    assert activated.status_code == 200
    return assistant_id


def test_ingestion_unchanged_update_preview_and_delete(client: TestClient) -> None:
    _, source_id = _space_and_source(client)
    first = client.post(
        "/api/v1/documents/upload",
        data={"source_id": source_id},
        files={"file": ("leave.md", b"# Leave\nEmployees receive 20 leave days.", "text/markdown")},
        headers={"Idempotency-Key": "upload-one"},
    )
    assert first.status_code == 202
    assert first.json()["status"] == "succeeded"
    document_id = first.json()["document_id"]
    first_version = first.json()["document_version_id"]

    repeated = client.post(
        "/api/v1/documents/upload",
        data={"source_id": source_id},
        files={"file": ("leave.md", b"# Leave\nEmployees receive 20 leave days.", "text/markdown")},
        headers={"Idempotency-Key": "upload-two"},
    )
    assert repeated.status_code == 202
    assert repeated.json()["stage"] == "unchanged"
    assert repeated.json()["document_version_id"] == first_version

    changed = client.post(
        "/api/v1/documents/upload",
        data={"source_id": source_id},
        files={"file": ("leave.md", b"# Leave\nEmployees receive 25 leave days.", "text/markdown")},
    )
    assert changed.status_code == 202
    assert changed.json()["document_version_id"] != first_version
    preview = client.get(f"/api/v1/documents/{document_id}/preview")
    assert preview.status_code == 200
    assert "25 leave days" in preview.text
    assert "20 leave days" not in preview.text
    assert client.delete(f"/api/v1/documents/{document_id}").status_code == 204
    assert client.get(f"/api/v1/documents/{document_id}/preview").status_code == 404


def test_upload_rejects_unsupported_and_malicious_content(client: TestClient) -> None:
    _, source_id = _space_and_source(client)
    unsupported = client.post(
        "/api/v1/documents/upload",
        data={"source_id": source_id},
        files={"file": ("payload.exe", b"binary", "application/octet-stream")},
    )
    assert unsupported.status_code == 415
    malicious = client.post(
        "/api/v1/documents/upload",
        data={"source_id": source_id},
        files={
            "file": (
                "payload.txt",
                b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE",
                "text/plain",
            )
        },
    )
    assert malicious.status_code == 422


def test_grounded_chat_stream_and_no_evidence_refusal(client: TestClient) -> None:
    space_id, source_id = _space_and_source(client)
    uploaded = client.post(
        "/api/v1/documents/upload",
        data={"source_id": source_id},
        files={
            "file": (
                "vpn.txt",
                b"VPN access requires multi-factor authentication and manager approval.",
                "text/plain",
            )
        },
    )
    assert uploaded.json()["status"] == "succeeded"
    assistant_id = _assistant(client, space_id)
    grounded = client.post(
        "/api/v1/chat/query",
        json={"assistant_id": assistant_id, "question": "What does VPN access require?"},
    )
    assert grounded.status_code == 200
    assert "event: query" in grounded.text
    assert "event: citation" in grounded.text
    assert '"status": "success"' in grounded.text
    refused = client.post(
        "/api/v1/chat/query",
        json={"assistant_id": assistant_id, "question": "What is the cafeteria menu?"},
    )
    assert refused.status_code == 200
    assert "当前授权范围内缺少" in refused.text
    assert '"status": "refusal"' in refused.text


def test_citation_accuracy_multiturn_profile_switch_and_revocation(client: TestClient) -> None:
    space_id, source_id = _space_and_source(client)
    uploaded = client.post(
        "/api/v1/documents/upload",
        data={"source_id": source_id},
        files={
            "file": (
                "remote.txt",
                b"Remote access requires manager approval and multi-factor authentication.",
                "text/plain",
            )
        },
    )
    document_id = uploaded.json()["document_id"]
    assistant_id = _assistant(client, space_id)
    first = client.post(
        "/api/v1/chat/query",
        json={"assistant_id": assistant_id, "question": "What does remote access require?"},
    )
    events = _events(first.text)
    query_event = next(data for name, data in events if name == "query")
    citation = next(data for name, data in events if name == "citation")
    source = client.get(str(citation["source_url"]))
    assert source.status_code == 200
    assert source.json()["document_id"] == document_id
    assert "source_key" not in source.text
    assert "acl_tokens" not in source.text

    follow_up = client.post(
        "/api/v1/chat/query",
        json={
            "assistant_id": assistant_id,
            "question": "Who must approve it?",
            "conversation_id": query_event["conversation_id"],
        },
    )
    assert "manager approval" in follow_up.text

    second_space, second_source = _space_and_source_named(client, "Product", "product-upload")
    client.post(
        "/api/v1/documents/upload",
        data={"source_id": second_source},
        files={"file": ("product.txt", b"Product Aurora supports SSO.", "text/plain")},
    )
    second_assistant = _assistant_named(client, second_space, "Product assistant")
    switched = client.post(
        "/api/v1/chat/query",
        json={"assistant_id": second_assistant, "question": "What does Product Aurora support?"},
    )
    assert "supports SSO" in switched.text

    removed = client.put(
        f"/api/v1/knowledge-spaces/{space_id}/memberships",
        json={"principal_token": "user:admin-demo", "role": None},
    )
    assert removed.status_code == 204
    revoked = client.post(
        "/api/v1/chat/query",
        json={
            "assistant_id": assistant_id,
            "question": "What does it require?",
            "conversation_id": query_event["conversation_id"],
        },
    )
    assert revoked.status_code == 403


def _space_and_source_named(
    client: TestClient, space_name: str, external_id: str
) -> tuple[str, str]:
    space = client.post(
        "/api/v1/knowledge-spaces",
        json={"name": space_name, "description": "Additional profile"},
    )
    source = client.post(
        f"/api/v1/knowledge-spaces/{space.json()['id']}/data-sources",
        json={"kind": "upload", "external_id": external_id},
    )
    return space.json()["id"], source.json()["id"]


def _assistant_named(client: TestClient, space_id: str, name: str) -> str:
    created = client.post(
        "/api/v1/assistants",
        json={
            "name": name,
            "draft": {
                "knowledge_space_ids": [space_id],
                "policy_version_ids": {},
                "provider_profile_ids": {},
            },
        },
    )
    version_id = created.json()["draft"]["id"]
    client.post(f"/api/v1/assistants/{created.json()['id']}/versions/{version_id}/activate")
    return created.json()["id"]


def test_conflicting_sources_are_cited_without_silent_resolution(client: TestClient) -> None:
    space_id, source_id = _space_and_source(client)
    for name, content in (
        ("old.txt", b"The policy leave limit is 20 days."),
        ("new.txt", b"The policy leave limit is 25 days."),
    ):
        result = client.post(
            "/api/v1/documents/upload",
            data={"source_id": source_id},
            files={"file": (name, content, "text/plain")},
        )
        assert result.json()["status"] == "succeeded"
    assistant_id = _assistant(client, space_id)
    response = client.post(
        "/api/v1/chat/query",
        json={"assistant_id": assistant_id, "question": "What is the policy leave limit?"},
    )
    assert "发现相互矛盾的已授权来源" in response.text
    assert '"status": "degraded"' in response.text
    assert sum(1 for name, _ in _events(response.text) if name == "citation") == 2
