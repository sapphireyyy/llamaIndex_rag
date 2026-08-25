"""Idempotent operator command for multi-tenant control-plane backfill."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict

from enterprise_rag.application.tenant_bootstrap import backfill_all_tenants
from enterprise_rag.config import get_settings
from enterprise_rag.infrastructure.database import create_engine, create_session_factory


def _administrator_mapping(values: list[str]) -> dict[str, str]:
    mappings: dict[str, str] = {}
    for value in values:
        tenant_id, separator, external_id = value.partition("=")
        if not separator or not tenant_id.strip() or not external_id.strip():
            raise argparse.ArgumentTypeError(
                "--administrator must use TENANT_ID=OIDC_SUBJECT format"
            )
        mappings[tenant_id.strip()] = external_id.strip()
    return mappings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill tenant lifecycle, active configuration, durable usage, "
            "and initial tenant administrators."
        )
    )
    parser.add_argument(
        "--administrator",
        action="append",
        default=[],
        metavar="TENANT_ID=OIDC_SUBJECT",
        help="Assign an administrator to a tenant; repeat for multiple tenants.",
    )
    parser.add_argument(
        "--created-by",
        default="operator:enterprise-rag-bootstrap",
        help="Non-secret operator subject written to provenance.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Calculate and verify, then roll back."
    )
    parser.add_argument(
        "--allow-missing-administrators",
        action="store_true",
        help="Return success even when non-archived tenants still lack an administrator.",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    sessions = create_session_factory(engine)
    try:
        async with sessions() as session:
            report = await backfill_all_tenants(
                session,
                created_by_subject=args.created_by,
                administrators=_administrator_mapping(args.administrator),
            )
            if args.dry_run:
                await session.rollback()
            else:
                await session.commit()
        payload = {
            **asdict(report),
            "database": settings.database_url.split("@")[-1],
            "dry_run": bool(args.dry_run),
            "ready": not report.missing_administrator_tenant_ids,
            "next_step": (
                "Enable persisted tenant authorization enforcement."
                if not report.missing_administrator_tenant_ids
                else "Assign an administrator for every reported tenant."
            ),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        if report.missing_administrator_tenant_ids and not args.allow_missing_administrators:
            return 2
        return 0
    finally:
        await engine.dispose()


def main() -> None:
    """Run the bootstrap and expose a stable process exit code for automation."""
    args = _parser().parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
