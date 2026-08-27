from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from enterprise_rag.config import get_settings
from enterprise_rag.infrastructure.database import (
    create_engine,
    create_schema_for_tests,
    create_session_factory,
    drop_schema_for_tests,
)
from enterprise_rag.main import create_app
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    database_url = os.getenv("RAG_TEST_DATABASE_URL")
    if database_url is None:
        database = tmp_path / "test.db"
        database_url = f"sqlite+aiosqlite:///{database.as_posix()}"
    test_engine = create_engine(database_url)
    await drop_schema_for_tests(test_engine)
    await create_schema_for_tests(test_engine)
    try:
        yield test_engine
    finally:
        await drop_schema_for_tests(test_engine)
        await test_engine.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[TestClient]:
    monkeypatch.setenv("RAG_ENVIRONMENT", "test")
    database_url = os.getenv("RAG_TEST_DATABASE_URL") or (
        f"sqlite+aiosqlite:///{(tmp_path / 'api.db').as_posix()}"
    )
    monkeypatch.setenv("RAG_DATABASE_URL", database_url)
    monkeypatch.setenv("RAG_OBJECT_STORE_ROOT", str(tmp_path / "objects"))
    monkeypatch.setenv("RAG_DEV_AUTH_ENABLED", "true")
    monkeypatch.setenv("RAG_VECTOR_BACKEND", "memory")
    monkeypatch.setenv("RAG_LEXICAL_BACKEND", "memory")
    monkeypatch.setenv("RAG_MODEL_BACKEND", "extractive")
    # Existing API tests use inline; queued contract tests cover the production boundary.
    monkeypatch.setenv("RAG_INGESTION_EXECUTION_MODE", "inline")
    get_settings.cache_clear()
    if database_url.startswith("postgresql"):
        reset_engine = create_engine(database_url)
        asyncio.run(drop_schema_for_tests(reset_engine))
        asyncio.run(reset_engine.dispose())
    with TestClient(create_app()) as test_client:
        yield test_client
    if database_url.startswith("postgresql"):
        reset_engine = create_engine(database_url)
        asyncio.run(drop_schema_for_tests(reset_engine))
        asyncio.run(reset_engine.dispose())
    get_settings.cache_clear()


@pytest.fixture
def queued_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[TestClient]:
    """Provide an API-only app that exercises durable queued submission semantics."""
    monkeypatch.setenv("RAG_ENVIRONMENT", "test")
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'queued-api.db').as_posix()}"
    monkeypatch.setenv("RAG_DATABASE_URL", database_url)
    monkeypatch.setenv("RAG_OBJECT_STORE_ROOT", str(tmp_path / "objects"))
    monkeypatch.setenv("RAG_DEV_AUTH_ENABLED", "true")
    monkeypatch.setenv("RAG_VECTOR_BACKEND", "memory")
    monkeypatch.setenv("RAG_LEXICAL_BACKEND", "memory")
    monkeypatch.setenv("RAG_MODEL_BACKEND", "extractive")
    monkeypatch.setenv("RAG_INGESTION_EXECUTION_MODE", "queued")
    monkeypatch.setenv("RAG_QUEUE_BACKEND", "inprocess")
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()
