"""Shared PostgreSQL RLS inventory, policy expressions, and read-only preflight."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

TENANT_SETTING = "NULLIF(current_setting('app.tenant_id', true), '')"

# Keep this inventory next to the ORM boundary. Migrations and the release gate
# both import it so a newly introduced tenant table cannot silently miss RLS.
RLS_POLICY_EXPRESSIONS: dict[str, str] = {
    "tenants": f"id = {TENANT_SETTING}",
    **{
        table: f"tenant_id = {TENANT_SETTING}"
        for table in (
            "principals",
            "groups",
            "principal_groups",
            "tenant_memberships",
            "tenant_group_roles",
            "tenant_invitations",
            "tenant_configuration_versions",
            "tenant_usage",
            "knowledge_spaces",
            "space_memberships",
            "data_sources",
            "documents",
            "document_versions",
            "index_generations",
            "chunks",
            "ingestion_jobs",
            "policy_versions",
            "assistants",
            "assistant_versions",
            "secret_bindings",
            "provider_profiles",
            "conversations",
            "query_executions",
            "feedback",
            "evaluation_datasets",
            "evaluation_dataset_versions",
            "evaluation_runs",
            "release_gates",
            "release_gate_overrides",
            "telemetry_events",
            "audit_events",
            "outbox",
            "idempotency_keys",
        )
    },
    "job_attempts": (
        f"EXISTS (SELECT 1 FROM ingestion_jobs j WHERE j.id = job_attempts.job_id "
        f"AND j.tenant_id = {TENANT_SETTING})"
    ),
    "messages": (
        f"EXISTS (SELECT 1 FROM conversations c WHERE c.id = messages.conversation_id "
        f"AND c.tenant_id = {TENANT_SETTING})"
    ),
    "answers": (
        f"EXISTS (SELECT 1 FROM query_executions q WHERE q.id = answers.query_id "
        f"AND q.tenant_id = {TENANT_SETTING})"
    ),
    "citations": (
        f"EXISTS (SELECT 1 FROM answers a JOIN query_executions q ON q.id = a.query_id "
        f"WHERE a.id = citations.answer_id AND q.tenant_id = {TENANT_SETTING})"
    ),
    "evaluation_results": (
        f"EXISTS (SELECT 1 FROM evaluation_runs r WHERE r.id = evaluation_results.run_id "
        f"AND r.tenant_id = {TENANT_SETTING})"
    ),
}

RLS_PROTECTED_TABLES = tuple(RLS_POLICY_EXPRESSIONS)


def _policy_is_tenant_scoped(policy: Mapping[str, Any]) -> bool:
    """确认策略涉及的读写路径都绑定了当前事务租户。"""
    command = str(policy.get("cmd") or "*")
    qual = str(policy.get("qual") or "")
    with_check = str(policy.get("with_check") or "")
    reads_rows = command in {"*", "r", "w", "d"}
    writes_rows = command in {"*", "a", "w"}
    return (
        (not reads_rows or "app.tenant_id" in qual)
        and (not writes_rows or "app.tenant_id" in with_check)
    )


class RLSPreflightError(RuntimeError):
    """运行账号或受保护表未满足生产 RLS 门禁。"""


async def postgres_rls_preflight(
    engine: AsyncEngine,
    runtime_role: str | None = None,
) -> dict[str, Any]:
    """只读核对角色属性、owner、RLS 标志和策略覆盖率。"""
    if engine.dialect.name != "postgresql":
        return {"dialect": engine.dialect.name, "skipped": True}
    async with engine.connect() as connection:
        current_user = str((await connection.scalar(text("SELECT current_user"))) or "")
        role_name = runtime_role or current_user
        if runtime_role is not None and current_user != runtime_role:
            raise RLSPreflightError("database connection user does not match runtime role")
        role = (
            await connection.execute(
                text(
                    "SELECT rolname, rolsuper, rolbypassrls, rolinherit "
                    "FROM pg_roles WHERE rolname = :role"
                ),
                {"role": role_name},
            )
        ).mappings().one_or_none()
        if role is None:
            raise RLSPreflightError("runtime role does not exist")
        if role["rolsuper"] or role["rolbypassrls"] or role["rolinherit"]:
            raise RLSPreflightError("runtime role must be NOSUPERUSER NOBYPASSRLS NOINHERIT")
        memberships = list(
            (
                await connection.execute(
                    text(
                        "SELECT parent.rolname FROM pg_auth_members m "
                        "JOIN pg_roles child ON child.oid = m.member "
                        "JOIN pg_roles parent ON parent.oid = m.roleid "
                        "WHERE child.rolname = :role"
                    ),
                    {"role": role_name},
                )
            ).scalars()
        )
        table_rows = list(
            (
                await connection.execute(
                    text(
                        "SELECT c.relname, r.rolname AS owner, c.relrowsecurity, "
                        "c.relforcerowsecurity FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "JOIN pg_roles r ON r.oid = c.relowner "
                        "WHERE n.nspname = 'public' "
                        "AND c.relname = ANY(CAST(:tables AS text[]))"
                    ),
                    {"tables": list(RLS_PROTECTED_TABLES)},
                )
            ).mappings()
        )
        policy_rows = list(
            (
                await connection.execute(
                    text(
                        "SELECT tablename, policyname, cmd, permissive, qual, with_check "
                        "FROM pg_policies WHERE schemaname = 'public' "
                        "AND tablename = ANY(CAST(:tables AS text[]))"
                    ),
                    {"tables": list(RLS_PROTECTED_TABLES)},
                )
            ).mappings()
        )
    tables = {str(row["relname"]): dict(row) for row in table_rows}
    policies_by_table: dict[str, list[dict[str, Any]]] = {}
    for row in policy_rows:
        policies_by_table.setdefault(str(row["tablename"]), []).append(dict(row))

    missing = sorted(set(RLS_PROTECTED_TABLES) - set(tables))
    unprotected = sorted(
        table
        for table, row in tables.items()
        if (
            not row["relrowsecurity"]
            or not row["relforcerowsecurity"]
            or table not in policies_by_table
            or not all(
                _policy_is_tenant_scoped(policy) for policy in policies_by_table[table]
            )
        )
    )
    wrong_owner = sorted(
        table for table, row in tables.items() if str(row["owner"]) == role_name
    )
    report: dict[str, Any] = {
        "dialect": "postgresql",
        "role": role_name,
        "role_attributes": dict(role),
        "memberships": [str(item) for item in memberships],
        "tables": tables,
        "policies": policies_by_table,
        "missing_tables": missing,
        "unprotected_tables": unprotected,
        "owner_tables": wrong_owner,
    }
    if memberships or missing or unprotected or wrong_owner:
        raise RLSPreflightError("PostgreSQL RLS preflight failed")
    return report
