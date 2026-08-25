from enterprise_rag.application.evaluation import (
    EvaluationObservation,
    EvaluationService,
)
from enterprise_rag.domain.types import RequestIdentity, Role
from enterprise_rag.infrastructure.orm import (
    AssistantRecord,
    AssistantVersionRecord,
    AuditEventRecord,
    EvaluationResultRecord,
    TelemetryEventRecord,
    TenantRecord,
)
from enterprise_rag.infrastructure.telemetry import TelemetryInput, TelemetryService
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_reproducible_evaluation_gates_and_audited_override(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    identity = RequestIdentity(
        "tenant-a", "quality-admin", roles=frozenset({Role.ADMINISTRATOR, Role.QUALITY})
    )
    async with sessions() as session:
        session.add(TenantRecord(id="tenant-a", name="Tenant A"))
        await session.flush()
        assistant = AssistantRecord(
            id="assistant-a",
            tenant_id="tenant-a",
            name="Assistant",
            active_version_id="assistant-version-a",
        )
        version = AssistantVersionRecord(
            id="assistant-version-a",
            tenant_id="tenant-a",
            assistant_id=assistant.id,
            version=1,
            state="active",
            knowledge_space_ids=["space-a"],
            config_hash="config-hash",
        )
        session.add(assistant)
        await session.flush()
        session.add(version)
        await session.flush()
        service = EvaluationService(session)
        dataset, first = await service.create_dataset_version(
            identity,
            "General corpus",
            [
                {
                    "id": "answerable",
                    "question": "What is the policy?",
                    "expected_source_scope": ["document-a"],
                    "expected_answerability": True,
                    "reference_answer": "policy allows access",
                },
                {
                    "id": "unanswerable",
                    "question": "Unknown?",
                    "expected_source_scope": [],
                    "expected_answerability": False,
                },
            ],
        )
        _, second = await service.create_dataset_version(
            identity,
            "General corpus",
            [{"id": "new", "question": "New question"}],
            dataset.id,
        )
        assert first.items[0]["id"] == "answerable"
        assert first.content_hash != second.content_hash

        async def runner(item: dict[str, object]) -> EvaluationObservation:
            if item["id"] == "answerable":
                return EvaluationObservation(
                    "success",
                    "The policy allows access.",
                    True,
                    frozenset({"document-a"}),
                    25.0,
                    None,
                )
            return EvaluationObservation(
                "refusal", "Insufficient evidence.", False, frozenset(), 10.0, None
            )

        run = await service.run(identity, first.id, version.id, runner)
        assert run.aggregates["access_control"] == 1.0
        assert run.aggregates["refusal_correctness"] == 1.0
        results = list(
            (
                await session.scalars(
                    select(EvaluationResultRecord).where(EvaluationResultRecord.run_id == run.id)
                )
            ).all()
        )
        assert {item.outcome for item in results} == {"passed"}
        assert all(item.details["cost"] is None for item in results)
        repeated = await service.run(identity, first.id, version.id, runner)
        assert repeated.aggregates == run.aggregates
        assert repeated.parameters["dataset_content_hash"] == first.content_hash
        assert repeated.parameters["assistant_config_hash"] == version.config_hash
        gate = await service.create_gate(
            identity, "Strict relevance", "retrieval_relevance", ">=", 1.1, True
        )
        status = await service.gate_status(identity, run.id)
        assert status["eligible"] is False
        await service.override_gate(identity, gate.id, run.id, "Accepted for pilot", "corr-1")
        assert (await service.gate_status(identity, run.id))["eligible"] is True
        audit = await session.scalar(
            select(AuditEventRecord).where(AuditEventRecord.action == "release_gate.override")
        )
        assert audit is not None


async def test_telemetry_redacts_and_can_disable_content_capture(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        hidden = await TelemetryService(session, False).record(
            TelemetryInput(
                "tenant-a",
                "corr-hidden",
                "query",
                "model",
                "success",
                attributes={"authorization": "Bearer abc.def.ghi"},
                content={"prompt": "secret text"},
            )
        )
        captured = await TelemetryService(session, True).record(
            TelemetryInput(
                "tenant-a",
                "corr-captured",
                "query",
                "model",
                "success",
                content={"response": "identifier 123-45-6789"},
            )
        )
        assert hidden is not None and hidden.content == {}
        assert hidden.attributes["authorization"] == "[REDACTED]"
        assert captured is not None
        assert captured.content["response"] == "identifier [REDACTED ID]"
        await session.commit()
        assert (
            await session.scalar(
                select(TelemetryEventRecord).where(
                    TelemetryEventRecord.correlation_id == "corr-hidden"
                )
            )
            is not None
        )
