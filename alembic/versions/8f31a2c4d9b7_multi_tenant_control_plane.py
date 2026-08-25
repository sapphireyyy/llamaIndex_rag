"""Add multi-tenant administration control plane and PostgreSQL RLS.

Revision ID: 8f31a2c4d9b7
Revises: 50adeaf9b913
Create Date: 2026-08-25
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Sequence, Union
from uuid import uuid4

from alembic import op
import sqlalchemy as sa


revision: str = "8f31a2c4d9b7"
down_revision: Union[str, Sequence[str], None] = "50adeaf9b913"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


RLS_TABLES = (
    "knowledge_spaces",
    "space_memberships",
    "data_sources",
    "documents",
    "document_versions",
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
)


def _slug(value: str, tenant_id: str, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not base:
        base = re.sub(r"[^a-z0-9]+", "-", tenant_id.lower()).strip("-")
    base = (base or "tenant")[:100]
    candidate = base
    suffix = 1
    while candidate in used:
        suffix += 1
        candidate = f"{base[:110 - len(str(suffix))]}-{suffix}"
    used.add(candidate)
    return candidate


def _backfill_control_plane() -> None:
    bind = op.get_bind()
    tenants = bind.execute(sa.text("SELECT id, name, settings FROM tenants")).mappings()
    used: set[str] = set()
    now = datetime.now(UTC)
    resources = (
        "members",
        "groups",
        "knowledge_spaces",
        "documents",
        "stored_bytes",
        "provider_units",
    )
    count_queries = {
        "members": "SELECT COUNT(*) FROM principals WHERE tenant_id = :tenant_id AND active = true",
        "groups": "SELECT COUNT(*) FROM groups WHERE tenant_id = :tenant_id",
        "knowledge_spaces": "SELECT COUNT(*) FROM knowledge_spaces WHERE tenant_id = :tenant_id AND state = 'active'",
        "documents": "SELECT COUNT(*) FROM documents WHERE tenant_id = :tenant_id AND state = 'active'",
        "stored_bytes": "SELECT COALESCE(SUM(size_bytes), 0) FROM document_versions WHERE tenant_id = :tenant_id",
        "provider_units": "SELECT 0",
    }
    for tenant in tenants:
        tenant_id = str(tenant["id"])
        settings = tenant["settings"] or {}
        if isinstance(settings, str):
            settings = json.loads(settings)
        config = {
            "schema_version": 1,
            "locale": settings.get("locale", "zh-CN"),
            "time_zone": settings.get("time_zone", "Asia/Shanghai"),
            "retention": settings.get("retention", {"audit_days": 365, "content_days": 365}),
            "uploads": settings.get("uploads", {"max_file_bytes": 52428800}),
            "quotas": settings.get("quotas", {}),
            "rates": settings.get("rates", {}),
            "concurrency": settings.get("concurrency", {}),
            "feature_flags": settings.get("feature_flags", {}),
            "provider_policy": settings.get("provider_policy", {}),
            "secret_binding_ids": settings.get("secret_binding_ids", []),
            "default_rag_policy_ids": settings.get("default_rag_policy_ids", {}),
        }
        canonical = json.dumps(config, sort_keys=True, separators=(",", ":"))
        config_id = f"tenant_config_{uuid4().hex}"
        bind.execute(
            sa.text(
                """
                INSERT INTO tenant_configuration_versions
                    (id, tenant_id, version, schema_version, state, config,
                     config_hash, validation_errors, previous_version_id,
                     created_by_subject, activated_at, created_at, updated_at)
                VALUES
                    (:id, :tenant_id, 1, 1, 'active', :config,
                     :config_hash, :validation_errors, NULL,
                     'migration:8f31a2c4d9b7', :now, :now, :now)
                """
            ),
            {
                "id": config_id,
                "tenant_id": tenant_id,
                "config": canonical,
                "config_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                "validation_errors": "[]",
                "now": now,
            },
        )
        bind.execute(
            sa.text(
                """
                UPDATE tenants
                SET slug = :slug, state = 'active', revision = 1,
                    active_config_version_id = :config_id
                WHERE id = :tenant_id
                """
            ),
            {
                "slug": _slug(str(tenant["name"]), tenant_id, used),
                "config_id": config_id,
                "tenant_id": tenant_id,
            },
        )
        for resource in resources:
            used_value = int(
                bind.execute(
                    sa.text(count_queries[resource]), {"tenant_id": tenant_id}
                ).scalar_one()
            )
            bind.execute(
                sa.text(
                    """
                    INSERT INTO tenant_usage
                        (tenant_id, resource, used, reserved, revision,
                         reconciled_at, created_at, updated_at)
                    VALUES
                        (:tenant_id, :resource, :used, 0, 1, :now, :now, :now)
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "resource": resource,
                    "used": used_value,
                    "now": now,
                },
            )


def _enable_postgresql_rls() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    predicate = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')"
    for table in RLS_TABLES:
        op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY'))
        op.execute(
            sa.text(
                f'CREATE POLICY tenant_isolation ON "{table}" '
                f"USING ({predicate}) WITH CHECK ({predicate})"
            )
        )


def upgrade() -> None:
    op.add_column("tenants", sa.Column("slug", sa.String(120), nullable=True))
    op.add_column(
        "tenants",
        sa.Column("state", sa.String(30), nullable=False, server_default="active"),
    )
    op.add_column(
        "tenants", sa.Column("state_reason", sa.Text(), nullable=False, server_default="")
    )
    op.add_column("tenants", sa.Column("suspended_at", sa.DateTime(timezone=True)))
    op.add_column("tenants", sa.Column("archived_at", sa.DateTime(timezone=True)))
    op.add_column(
        "tenants", sa.Column("revision", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column("tenants", sa.Column("active_config_version_id", sa.String(80)))
    op.create_index("ux_tenants_slug", "tenants", ["slug"], unique=True)
    op.create_index("ix_tenants_state", "tenants", ["state"])
    op.create_index(
        "ix_tenants_active_config_version_id", "tenants", ["active_config_version_id"]
    )

    for table, allowed in (
        ("principals", "'invited', 'active', 'suspended', 'removed'"),
        ("groups", "'active', 'suspended', 'removed'"),
    ):
        op.add_column(
            table,
            sa.Column("state", sa.String(30), nullable=False, server_default="active"),
        )
        op.add_column(
            table, sa.Column("revision", sa.Integer(), nullable=False, server_default="1")
        )
        op.add_column(
            table, sa.Column("source", sa.String(80), nullable=False, server_default="local")
        )
        op.add_column(
            table, sa.Column("provenance", sa.JSON(), nullable=False, server_default="{}")
        )
        op.create_index(f"ix_{table}_state", table, ["state"])
        with op.batch_alter_table(table) as batch:
            batch.create_unique_constraint(
                f"uq_{table}_tenant_id_id", ["tenant_id", "id"]
            )
            batch.create_check_constraint(f"ck_{table}_state", f"state IN ({allowed})")

    op.create_table(
        "principal_groups_new",
        sa.Column("tenant_id", sa.String(80), nullable=False),
        sa.Column("principal_id", sa.String(80), nullable=False),
        sa.Column("group_id", sa.String(80), nullable=False),
        sa.Column("state", sa.String(30), nullable=False, server_default="active"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source", sa.String(80), nullable=False, server_default="local"),
        sa.Column("provenance", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "principal_id", "group_id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["principals.tenant_id", "principals.id"],
            name="fk_principal_groups_tenant_principal",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "group_id"],
            ["groups.tenant_id", "groups.id"],
            name="fk_principal_groups_tenant_group",
        ),
        sa.CheckConstraint(
            "state IN ('active', 'suspended', 'removed')",
            name="ck_principal_groups_state",
        ),
    )
    now = datetime.now(UTC)
    op.execute(
        sa.text(
            """
            INSERT INTO principal_groups_new
                (tenant_id, principal_id, group_id, state, revision, source,
                 provenance, created_at, updated_at)
            SELECT p.tenant_id, pg.principal_id, pg.group_id, 'active', 1,
                   'migration', '{}', :now, :now
            FROM principal_groups pg
            JOIN principals p ON p.id = pg.principal_id
            JOIN groups g ON g.id = pg.group_id AND g.tenant_id = p.tenant_id
            """
        ).bindparams(now=now)
    )
    op.drop_table("principal_groups")
    op.rename_table("principal_groups_new", "principal_groups")
    op.create_index("ix_principal_groups_tenant_id", "principal_groups", ["tenant_id"])

    op.create_table(
        "tenant_memberships",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("tenant_id", sa.String(80), nullable=False),
        sa.Column("principal_id", sa.String(80), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("direct_roles", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("state_reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["principals.tenant_id", "principals.id"],
            name="fk_tenant_memberships_tenant_principal",
        ),
        sa.UniqueConstraint("tenant_id", "principal_id"),
        sa.CheckConstraint(
            "state IN ('invited', 'active', 'suspended', 'removed')",
            name="ck_tenant_memberships_state",
        ),
    )
    op.create_index("ix_tenant_memberships_tenant_id", "tenant_memberships", ["tenant_id"])
    op.create_index("ix_tenant_memberships_principal_id", "tenant_memberships", ["principal_id"])
    op.create_index("ix_tenant_memberships_state", "tenant_memberships", ["state"])

    op.create_table(
        "tenant_group_roles",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("tenant_id", sa.String(80), nullable=False),
        sa.Column("group_id", sa.String(80), nullable=False),
        sa.Column("role", sa.String(30), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(
            ["tenant_id", "group_id"],
            ["groups.tenant_id", "groups.id"],
            name="fk_tenant_group_roles_tenant_group",
        ),
        sa.UniqueConstraint("tenant_id", "group_id", "role"),
        sa.CheckConstraint(
            "state IN ('active', 'suspended', 'removed')",
            name="ck_tenant_group_roles_state",
        ),
    )
    op.create_index("ix_tenant_group_roles_tenant_id", "tenant_group_roles", ["tenant_id"])
    op.create_index("ix_tenant_group_roles_group_id", "tenant_group_roles", ["group_id"])
    op.create_index("ix_tenant_group_roles_role", "tenant_group_roles", ["role"])

    op.create_table(
        "tenant_invitations",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("tenant_id", sa.String(80), nullable=False),
        sa.Column("destination", sa.String(320), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("roles", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("accepted_by_principal_id", sa.String(80)),
        sa.Column("created_by_subject", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.UniqueConstraint("tenant_id", "token_hash"),
        sa.CheckConstraint(
            "state IN ('pending', 'accepted', 'revoked', 'expired')",
            name="ck_tenant_invitations_state",
        ),
    )
    op.create_index("ix_tenant_invitations_tenant_id", "tenant_invitations", ["tenant_id"])
    op.create_index("ix_tenant_invitations_destination", "tenant_invitations", ["destination"])
    op.create_index("ix_tenant_invitations_token_hash", "tenant_invitations", ["token_hash"])
    op.create_index("ix_tenant_invitations_state", "tenant_invitations", ["state"])

    op.create_table(
        "tenant_configuration_versions",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("tenant_id", sa.String(80), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("validation_errors", sa.JSON(), nullable=False),
        sa.Column("previous_version_id", sa.String(80)),
        sa.Column("created_by_subject", sa.String(255), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.UniqueConstraint("tenant_id", "version"),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.CheckConstraint("version > 0", name="ck_tenant_configuration_version_positive"),
        sa.CheckConstraint(
            "state IN ('draft', 'active', 'superseded', 'invalid')",
            name="ck_tenant_configuration_versions_state",
        ),
    )
    op.create_index(
        "ix_tenant_configuration_versions_tenant_id",
        "tenant_configuration_versions",
        ["tenant_id"],
    )
    op.create_index(
        "ix_tenant_configuration_versions_previous_version_id",
        "tenant_configuration_versions",
        ["previous_version_id"],
    )

    op.create_table(
        "tenant_usage",
        sa.Column("tenant_id", sa.String(80), nullable=False),
        sa.Column("resource", sa.String(80), nullable=False),
        sa.Column("used", sa.BigInteger(), nullable=False),
        sa.Column("reserved", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("reconciled_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("tenant_id", "resource"),
        sa.CheckConstraint("used >= 0", name="ck_tenant_usage_used_nonnegative"),
        sa.CheckConstraint("reserved >= 0", name="ck_tenant_usage_reserved_nonnegative"),
    )
    op.create_index("ix_tenant_usage_tenant_id", "tenant_usage", ["tenant_id"])

    _backfill_control_plane()

    with op.batch_alter_table("tenants") as batch:
        batch.alter_column("slug", existing_type=sa.String(120), nullable=False)
        batch.create_check_constraint(
            "ck_tenants_state",
            "state IN ('provisioning', 'active', 'suspended', 'archived')",
        )

    _enable_postgresql_rls()


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in reversed(RLS_TABLES):
            op.execute(sa.text(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"'))
            op.execute(sa.text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'))

    for table in (
        "tenant_usage",
        "tenant_configuration_versions",
        "tenant_invitations",
        "tenant_group_roles",
        "tenant_memberships",
    ):
        op.drop_table(table)

    op.create_table(
        "principal_groups_old",
        sa.Column("principal_id", sa.String(80), nullable=False),
        sa.Column("group_id", sa.String(80), nullable=False),
        sa.ForeignKeyConstraint(["principal_id"], ["principals.id"]),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"]),
        sa.PrimaryKeyConstraint("principal_id", "group_id"),
    )
    op.execute(
        sa.text(
            "INSERT INTO principal_groups_old (principal_id, group_id) "
            "SELECT principal_id, group_id FROM principal_groups"
        )
    )
    op.drop_table("principal_groups")
    op.rename_table("principal_groups_old", "principal_groups")

    for table in ("groups", "principals"):
        op.drop_index(f"ix_{table}_state", table_name=table)
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f"uq_{table}_tenant_id_id", type_="unique")
            batch.drop_constraint(f"ck_{table}_state", type_="check")
            for column in ("provenance", "source", "revision", "state"):
                batch.drop_column(column)

    op.drop_index("ix_tenants_active_config_version_id", table_name="tenants")
    op.drop_index("ix_tenants_state", table_name="tenants")
    op.drop_index("ux_tenants_slug", table_name="tenants")
    with op.batch_alter_table("tenants") as batch:
        batch.drop_constraint("ck_tenants_state", type_="check")
        for column in (
            "active_config_version_id",
            "revision",
            "archived_at",
            "suspended_at",
            "state_reason",
            "state",
            "slug",
        ):
            batch.drop_column(column)
