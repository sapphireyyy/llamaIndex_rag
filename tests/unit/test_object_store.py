from pathlib import Path

import pytest
from enterprise_rag.infrastructure.object_store import FileSystemObjectStore, tenant_object_key


@pytest.mark.asyncio
async def test_filesystem_store_round_trip_and_tenant_key(tmp_path: Path) -> None:
    store = FileSystemObjectStore(tmp_path)
    key = tenant_object_key("tenant-a", "originals", "doc-1", "../policy.txt")
    assert key == "tenants/tenant-a/originals/doc-1/policy.txt"
    await store.put(key, b"policy", "text/plain")
    assert await store.exists(key)
    assert await store.get(key) == b"policy"
    await store.delete(key)
    assert not await store.exists(key)


@pytest.mark.asyncio
async def test_store_rejects_escape(tmp_path: Path) -> None:
    store = FileSystemObjectStore(tmp_path)
    with pytest.raises(ValueError):
        await store.put("../../secret", b"x", "text/plain")

