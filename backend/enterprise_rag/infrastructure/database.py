"""数据库基础设施：创建异步引擎、会话、仓储和事务作用域。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from typing import Any, Generic, TypeVar, cast

from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session as SyncSession

from enterprise_rag.infrastructure.orm import Base

T = TypeVar("T", bound=Base)

_request_tenant_id: ContextVar[str | None] = ContextVar(
    "enterprise_rag_request_tenant_id", default=None
)


def begin_request_tenant_boundary() -> Token[str | None]:
    """Start a request with no inherited tenant selection."""
    return _request_tenant_id.set(None)


def bind_request_tenant(tenant_id: str) -> None:
    """Bind a validated tenant for sessions created later in this request task."""
    _request_tenant_id.set(tenant_id)


def end_request_tenant_boundary(token: Token[str | None]) -> None:
    """Restore context so an async worker cannot inherit the selected tenant."""
    _request_tenant_id.reset(token)


def require_session_tenant(session: AsyncSession) -> str:
    """Return the validated session tenant or fail before an unscoped lookup."""
    tenant_id = str(session.info.get("tenant_id") or _request_tenant_id.get() or "")
    if not tenant_id:
        raise RuntimeError("tenant-owned query requires a validated tenant context")
    return tenant_id


async def get_tenant_owned(
    session: AsyncSession,
    model: type[T],
    entity_id: str,
    *,
    tenant_id: str | None = None,
    for_update: bool = False,
) -> T | None:
    """Load a tenant-owned row by both identifier and validated tenant boundary."""
    scoped_tenant_id = tenant_id or require_session_tenant(session)
    model_columns = cast(Any, model)
    statement = select(model).where(
        model_columns.id == entity_id,
        model_columns.tenant_id == scoped_tenant_id,
    )
    if for_update:
        statement = statement.with_for_update()
    return cast(T | None, await session.scalar(statement))


@event.listens_for(SyncSession, "after_begin")
def _apply_postgresql_tenant_context(
    session: SyncSession, transaction: object, connection: Any
) -> None:
    """Set PostgreSQL transaction-local RLS state for tenant-aware sessions."""
    del transaction
    tenant_id = session.info.get("tenant_id") or _request_tenant_id.get()
    if tenant_id:
        session.info["tenant_id"] = tenant_id
    if tenant_id and connection.dialect.name == "postgresql":
        connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": tenant_id},
        )


def create_engine(database_url: str, **kwargs: Any) -> AsyncEngine:
    """创建异步数据库引擎。"""
    defaults: dict[str, Any] = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        defaults["connect_args"] = {"check_same_thread": False}
    defaults.update(kwargs)
    return create_async_engine(database_url, **defaults)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """创建异步数据库会话工厂。"""
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


class SqlAlchemyRepository(Generic[T]):
    """提供按实体类型封装的常用异步增删查操作。"""

    def __init__(self, session: AsyncSession, model: type[T]) -> None:
        """初始化仓储或工作单元依赖。"""
        self.session = session
        self.model = model

    async def get(self, entity_id: str) -> T | None:
        """按标识读取实体。"""
        if hasattr(self.model, "tenant_id"):
            return await get_tenant_owned(self.session, self.model, entity_id)
        return await self.session.get(self.model, entity_id)

    async def add(self, entity: T) -> T:
        """将实体加入当前会话。"""
        self.session.add(entity)
        await self.session.flush()
        return entity

    async def delete(self, entity: T) -> None:
        """从当前会话删除实体。"""
        await self.session.delete(entity)

    async def list_for_tenant(self, tenant_id: str) -> list[T]:
        """列出租户下的实体。"""
        tenant_column = cast(Any, self.model).tenant_id
        statement = select(self.model).where(tenant_column == tenant_id)
        return list((await self.session.scalars(statement)).all())


class SqlAlchemyUnitOfWork:
    """管理一个会话的进入、提交、回滚和关闭生命周期。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """初始化仓储或工作单元依赖。"""
        self.session_factory = session_factory
        self.session: AsyncSession | None = None

    async def __aenter__(self) -> SqlAlchemyUnitOfWork:
        """进入事务工作单元。"""
        self.session = self.session_factory()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """根据执行结果提交或回滚事务。"""
        del exc_type, traceback
        if self.session is None:
            return
        if exc is not None:
            await self.session.rollback()
        await self.session.close()

    async def commit(self) -> None:
        """提交当前事务。"""
        if self.session is None:
            raise RuntimeError("unit of work is not active")
        await self.session.commit()

    async def rollback(self) -> None:
        """回滚当前事务。"""
        if self.session is None:
            raise RuntimeError("unit of work is not active")
        await self.session.rollback()


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """提供提交即成功、异常自动回滚的异步事务作用域。"""
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def set_transaction_tenant(session: AsyncSession, tenant_id: str) -> None:
    """Bind a validated tenant to the current transaction for PostgreSQL RLS."""
    normalized = tenant_id.strip()
    if not normalized:
        raise ValueError("tenant context must not be empty")
    session.info["tenant_id"] = normalized
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": normalized},
        )


async def clear_transaction_tenant(session: AsyncSession) -> None:
    """Clear application context; PostgreSQL also resets it at transaction end."""
    session.info.pop("tenant_id", None)
    bind = session.get_bind()
    if bind.dialect.name == "postgresql" and session.in_transaction():
        await session.execute(text("SELECT set_config('app.tenant_id', '', true)"))


@asynccontextmanager
async def tenant_session_scope(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: str
) -> AsyncIterator[AsyncSession]:
    """Create an API/worker session whose transaction cannot cross tenant boundaries."""
    async with session_factory() as session:
        try:
            await set_transaction_tenant(session, tenant_id)
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            if session.in_transaction():
                await clear_transaction_tenant(session)


async def check_database(engine: AsyncEngine) -> bool:
    """执行 SELECT 1 检查数据库连接是否可用。"""
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def create_schema_for_tests(engine: AsyncEngine) -> None:
    """为隔离测试数据库创建全部 ORM 表。"""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def drop_schema_for_tests(engine: AsyncEngine) -> None:
    """在复用测试数据库前删除隔离测试表。"""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
