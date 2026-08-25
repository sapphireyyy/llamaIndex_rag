import pytest
from enterprise_rag.domain.types import JobMessage
from enterprise_rag.infrastructure.queue import InProcessQueue


@pytest.mark.asyncio
async def test_queue_is_priority_ordered_and_deduplicated() -> None:
    queue = InProcessQueue()
    low = JobMessage("low", "ingest", "tenant-a", "c1", {}, priority=1)
    high = JobMessage("high", "revoke", "tenant-a", "c2", {}, priority=100)
    await queue.publish(low)
    await queue.publish(low)
    await queue.publish(high)
    assert queue.size == 2
    assert (await queue.get()).id == "high"
    queue.task_done()
    assert (await queue.get()).id == "low"
    queue.task_done()

