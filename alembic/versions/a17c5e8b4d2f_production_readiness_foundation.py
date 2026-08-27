"""Add durable ingestion leases and versioned index generations."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from enterprise_rag.infrastructure.rls import RLS_POLICY_EXPRESSIONS


revision: str = "a17c5e8b4d2f"
down_revision: Union[str, Sequence[str], None] = "8f31a2c4d9b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add_columns() -> None:
    """Add nullable or server-defaulted columns so existing rows remain readable."""
    op.add_column("documents", sa.Column("active_generation_id", sa.String(80), nullable=True))
    op.add_column("chunks", sa.Column("index_generation_id", sa.String(80), nullable=True))
    op.add_column("ingestion_jobs", sa.Column("claimed_by", sa.String(120), nullable=True))
    op.add_column(
        "ingestion_jobs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "ingestion_jobs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "ingestion_jobs", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "ingestion_jobs",
        sa.Column("processing_config_version_id", sa.String(80), nullable=True),
    )
    op.add_column(
        "ingestion_jobs",
        sa.Column(
            "processing_snapshot",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.add_column("outbox", sa.Column("claimed_by", sa.String(120), nullable=True))
    op.add_column(
        "outbox", sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("outbox", sa.Column("last_error", sa.String(512), nullable=True))
    op.add_column(
        "provider_profiles",
        sa.Column("profile_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "provider_profiles",
        sa.Column("profile_hash", sa.String(64), nullable=False, server_default=""),
    )
    op.add_column(
        "query_executions",
        sa.Column("delivery_mode", sa.String(30), nullable=False, server_default="buffered"),
    )
    op.add_column(
        "query_executions",
        sa.Column(
            "policy_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'"),
        ),
    )
    op.add_column(
        "query_executions",
        sa.Column(
            "provider_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'"),
        ),
    )


def _add_indexes() -> None:
    """Index lease and generation lookups used by workers and reconciliation."""
    for name, table, columns in (
        ("ix_documents_active_generation_id", "documents", ["active_generation_id"]),
        ("ix_chunks_index_generation_id", "chunks", ["index_generation_id"]),
        ("ix_ingestion_jobs_claimed_by", "ingestion_jobs", ["claimed_by"]),
        ("ix_ingestion_jobs_lease_expires_at", "ingestion_jobs", ["lease_expires_at"]),
        ("ix_ingestion_jobs_heartbeat_at", "ingestion_jobs", ["heartbeat_at"]),
        ("ix_ingestion_jobs_next_attempt_at", "ingestion_jobs", ["next_attempt_at"]),
        (
            "ix_ingestion_jobs_processing_config_version_id",
            "ingestion_jobs",
            ["processing_config_version_id"],
        ),
        ("ix_outbox_claimed_by", "outbox", ["claimed_by"]),
        ("ix_outbox_claim_expires_at", "outbox", ["claim_expires_at"]),
        ("ix_index_generations_tenant_id", "index_generations", ["tenant_id"]),
        ("ix_index_generations_document_id", "index_generations", ["document_id"]),
        (
            "ix_index_generations_document_version_id",
            "index_generations",
            ["document_version_id"],
        ),
        (
            "ix_index_generations_processing_config_version_id",
            "index_generations",
            ["processing_config_version_id"],
        ),
        ("ix_index_generations_state", "index_generations", ["state"]),
    ):
        op.create_index(name, table, columns, unique=False)


def _replace_chunk_unique_constraint() -> None:
    """Replace the old document-version ordinal key with a generation-local key."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    matching = [
        item
        for item in inspector.get_unique_constraints("chunks")
        if set(item.get("column_names") or ()) == {"document_version_id", "ordinal"}
    ]
    if not matching:
        return
    old_name = matching[0].get("name")
    if old_name:
        with op.batch_alter_table("chunks") as batch:
            batch.drop_constraint(old_name, type_="unique")
            batch.create_unique_constraint(
                "uq_chunks_index_generation_ordinal", ["index_generation_id", "ordinal"]
            )
        return

    # SQLite can expose an unnamed table constraint. Rebuild from reflection so
    # the operation remains reversible without guessing a generated constraint name.
    reflected = sa.Table("chunks", sa.MetaData(), autoload_with=bind)
    for constraint in list(reflected.constraints):
        if isinstance(constraint, sa.UniqueConstraint) and {
            column.name for column in constraint.columns
        } == {"document_version_id", "ordinal"}:
            reflected.constraints.remove(constraint)
    reflected.append_constraint(
        sa.UniqueConstraint(
            reflected.c.index_generation_id,
            reflected.c.ordinal,
            name="uq_chunks_index_generation_ordinal",
        )
    )
    with op.batch_alter_table("chunks", recreate="always", copy_from=reflected):
        pass


def _replace_policy_unique_constraint() -> None:
    """Scope policy version numbers by tenant so every tenant can have v1."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    matching = [
        item
        for item in inspector.get_unique_constraints("policy_versions")
        if set(item.get("column_names") or ()) == {"policy_id", "version"}
    ]
    if not matching:
        return
    old_name = matching[0].get("name")
    if old_name:
        with op.batch_alter_table("policy_versions") as batch:
            batch.drop_constraint(old_name, type_="unique")
            batch.create_unique_constraint(
                "uq_policy_versions_tenant_policy_version",
                ["tenant_id", "policy_id", "version"],
            )
        return

    reflected = sa.Table("policy_versions", sa.MetaData(), autoload_with=bind)
    for constraint in list(reflected.constraints):
        if isinstance(constraint, sa.UniqueConstraint) and {
            column.name for column in constraint.columns
        } == {"policy_id", "version"}:
            reflected.constraints.remove(constraint)
    reflected.append_constraint(
        sa.UniqueConstraint(
            reflected.c.tenant_id,
            reflected.c.policy_id,
            reflected.c.version,
            name="uq_policy_versions_tenant_policy_version",
        )
    )
    with op.batch_alter_table("policy_versions", recreate="always", copy_from=reflected):
        pass


def _backfill_generations() -> None:
    """Create a compatible generation for every existing content version."""
    bind = op.get_bind()
    concat = (
        "'generation_' || dv.id"
        if bind.dialect.name != "mysql"
        else "CONCAT('generation_', dv.id)"
    )
    op.execute(
        sa.text(
            "INSERT INTO index_generations "
            "(id, tenant_id, document_id, document_version_id, generation_number, "
            "processing_config_version_id, processing_strategy_version, processing_config_hash, "
            "component_versions, reason, state, dense_state, lexical_state, chunk_count, "
            "created_at, updated_at) "
            "SELECT "
            + concat
            + ", dv.tenant_id, dv.document_id, dv.id, 1, NULL, 'ingestion-v1', "
            "'0000000000000000000000000000000000000000000000000000000000000000', '{}', "
            "'migration', CASE WHEN d.active_version_id = dv.id THEN 'active' ELSE 'superseded' END, "
            "'published', 'published', "
            "(SELECT COUNT(*) FROM chunks c WHERE c.document_version_id = dv.id), "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
            "FROM document_versions dv JOIN documents d ON d.id = dv.document_id "
            "WHERE NOT EXISTS "
            "(SELECT 1 FROM index_generations g WHERE g.document_version_id = dv.id)"
        )
    )
    op.execute(
        sa.text(
            "UPDATE chunks SET index_generation_id = 'generation_' || document_version_id "
            "WHERE index_generation_id IS NULL"
        )
    )
    op.execute(
        sa.text(
            "UPDATE documents SET active_generation_id = 'generation_' || active_version_id "
            "WHERE active_version_id IS NOT NULL AND active_generation_id IS NULL"
        )
    )


def upgrade() -> None:
    """Upgrade schema without changing the currently active document versions."""
    op.create_table(
        "index_generations",
        sa.Column("id", sa.String(80), nullable=False),
        sa.Column("tenant_id", sa.String(80), nullable=False),
        sa.Column("document_id", sa.String(80), nullable=False),
        sa.Column("document_version_id", sa.String(80), nullable=False),
        sa.Column("generation_number", sa.Integer(), nullable=False),
        sa.Column("processing_config_version_id", sa.String(80), nullable=True),
        sa.Column("processing_strategy_version", sa.String(80), nullable=False),
        sa.Column("processing_config_hash", sa.String(64), nullable=False),
        sa.Column("component_versions", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(80), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("dense_state", sa.String(30), nullable=False),
        sa.Column("lexical_state", sa.String(30), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(512), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "generation_number"),
    )
    _add_columns()
    _add_indexes()
    _replace_chunk_unique_constraint()
    _replace_policy_unique_constraint()
    _backfill_generations()

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table, predicate in RLS_POLICY_EXPRESSIONS.items():
            op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
            op.execute(sa.text(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY'))
            op.execute(sa.text(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"'))
            op.execute(
                sa.text(
                    f'CREATE POLICY tenant_isolation ON "{table}" USING ({predicate}) '
                    f'WITH CHECK ({predicate})'
                )
            )


def downgrade() -> None:
    """Remove the additive readiness schema and restore the old chunk key."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in RLS_POLICY_EXPRESSIONS:
            op.execute(sa.text(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"'))
            op.execute(sa.text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'))
    with op.batch_alter_table("chunks") as batch:
        batch.drop_constraint("uq_chunks_index_generation_ordinal", type_="unique")
        batch.create_unique_constraint(
            "uq_chunks_document_version_ordinal", ["document_version_id", "ordinal"]
        )
    with op.batch_alter_table("policy_versions") as batch:
        batch.drop_constraint("uq_policy_versions_tenant_policy_version", type_="unique")
        batch.create_unique_constraint(
            "uq_policy_versions_policy_version", ["policy_id", "version"]
        )
    op.drop_index("ix_index_generations_state", table_name="index_generations")
    op.drop_index(
        "ix_index_generations_processing_config_version_id", table_name="index_generations"
    )
    op.drop_index("ix_index_generations_document_version_id", table_name="index_generations")
    op.drop_index("ix_index_generations_document_id", table_name="index_generations")
    op.drop_index("ix_index_generations_tenant_id", table_name="index_generations")
    op.drop_table("index_generations")
    for name, table in (
        ("ix_outbox_claim_expires_at", "outbox"),
        ("ix_outbox_claimed_by", "outbox"),
        ("ix_ingestion_jobs_processing_config_version_id", "ingestion_jobs"),
        ("ix_ingestion_jobs_next_attempt_at", "ingestion_jobs"),
        ("ix_ingestion_jobs_heartbeat_at", "ingestion_jobs"),
        ("ix_ingestion_jobs_lease_expires_at", "ingestion_jobs"),
        ("ix_ingestion_jobs_claimed_by", "ingestion_jobs"),
        ("ix_chunks_index_generation_id", "chunks"),
        ("ix_documents_active_generation_id", "documents"),
    ):
        op.drop_index(name, table_name=table)
    for table, column in (
        ("query_executions", "provider_snapshot"),
        ("query_executions", "policy_snapshot"),
        ("query_executions", "delivery_mode"),
        ("provider_profiles", "profile_hash"),
        ("provider_profiles", "profile_version"),
        ("outbox", "last_error"),
        ("outbox", "claim_expires_at"),
        ("outbox", "claimed_by"),
        ("ingestion_jobs", "processing_snapshot"),
        ("ingestion_jobs", "processing_config_version_id"),
        ("ingestion_jobs", "next_attempt_at"),
        ("ingestion_jobs", "heartbeat_at"),
        ("ingestion_jobs", "lease_expires_at"),
        ("ingestion_jobs", "claimed_by"),
        ("chunks", "index_generation_id"),
        ("documents", "active_generation_id"),
    ):
        op.drop_column(table, column)
