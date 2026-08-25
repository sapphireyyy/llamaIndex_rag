import json
from pathlib import Path

import pytest
from enterprise_rag.application.evaluation import EvaluationObservation, evaluate_item
from enterprise_rag.application.ports import IndexDocument
from enterprise_rag.domain.types import AuthorizationScope
from enterprise_rag.infrastructure.search import (
    DeterministicEmbedding,
    MemoryLexicalSearch,
    MemoryVectorSearch,
)


def test_general_three_domain_corpus_passes_mandatory_security_logic() -> None:
    corpus_path = Path(__file__).parents[2] / "evaluation" / "corpora" / "general-v1.json"
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    assert set(corpus["domains"]) == {"policy", "it-help", "product"}
    assert {item["case"] for item in corpus["items"]} >= {
        "answerable",
        "unanswerable",
        "conflicting",
        "permission-sensitive",
        "multi-turn",
        "source-update",
        "source-deletion",
        "mid-session-revocation",
    }
    outcomes = []
    for item in corpus["items"]:
        expected_sources = frozenset(item["expected_source_scope"])
        answerable = item["expected_answerability"]
        observation = EvaluationObservation(
            "success" if answerable else "refusal",
            str(item.get("reference_answer", "Insufficient evidence.")),
            answerable,
            expected_sources if answerable else frozenset(),
            100.0,
            None,
        )
        metrics, outcome, details = evaluate_item(item, observation)
        outcomes.append(outcome)
        assert metrics["access_control"] == 1.0
        assert metrics["refusal_correctness"] == 1.0
        assert details["cost"] is None
    assert set(outcomes) == {"passed"}


@pytest.mark.asyncio
async def test_general_corpus_retrieval_never_crosses_tenant_or_acl() -> None:
    corpus_path = Path(__file__).parents[2] / "evaluation" / "corpora" / "general-v1.json"
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    embedding = DeterministicEmbedding()
    dense = MemoryVectorSearch(embedding)
    lexical = MemoryLexicalSearch()
    texts = {
        "policy-handbook": "Employees receive 20 leave days.",
        "policy-current": "The travel limit is 25 days.",
        "policy-legacy": "The travel limit is 20 days.",
        "it-vpn": "VPN remote access requires manager approval and MFA.",
        "it-mfa-current": "MFA version 3 is current.",
        "product-aurora": "Aurora supports SSO centralized sign-in. SKU AUR-ENT-01.",
        "executive-restricted": "Executive bonus policy is confidential.",
        "other-tenant": "Tomorrow cafeteria menu is pizza.",
    }
    documents = []
    for document_id, text in texts.items():
        tenant = "other" if document_id == "other-tenant" else "benchmark"
        acl = ["group:executives"] if document_id == "executive-restricted" else [
            "group:employees", "group:customers"
        ]
        vector = (await embedding.embed([text]))[0]
        documents.append(
            IndexDocument(
                f"chunk-{document_id}",
                text,
                vector,
                {
                    "tenant_id": tenant,
                    "knowledge_space_id": "general",
                    "document_id": document_id,
                    "document_version_id": f"version-{document_id}",
                    "acl_tokens": acl,
                },
            )
        )
    for search in (dense, lexical):
        await search.stage(documents)
        for document_id in texts:
            await search.publish(f"version-{document_id}")
        search.set_acl_epoch("benchmark", 1)
    for item in corpus["items"]:
        groups = item["access_context"]["groups"]
        scope = AuthorizationScope(
            "benchmark",
            frozenset(f"group:{group}" for group in groups),
            frozenset({"general"}),
            1,
        )
        results = await dense.retrieve(item["question"], scope, 10) + await lexical.retrieve(
            item["question"], scope, 10
        )
        returned = {result.metadata["document_id"] for result in results}
        assert "other-tenant" not in returned
        assert "executive-restricted" not in returned
