"""异步 worker 入口：启动基础设施并持续消费摄取与 outbox 任务。"""

from __future__ import annotations

import asyncio
import signal
from contextlib import suppress

import structlog

from enterprise_rag.config import get_settings
from enterprise_rag.infrastructure.container import Container

log = structlog.get_logger()


async def run_worker() -> None:
    """启动运行时容器，注册停止信号并等待 worker 退出。"""
    container = Container(get_settings())
    await container.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signame in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signame, stop.set)
    log.info("worker_ready")
    await stop.wait()
    await container.stop()


def main() -> None:
    """为命令行入口运行异步 worker。"""
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
