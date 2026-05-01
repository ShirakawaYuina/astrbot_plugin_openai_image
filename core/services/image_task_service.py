"""图片任务调度与耗时采集。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any


class ImageTaskService:
    """控制图片任务并发并记录关键阶段耗时。"""

    def __init__(self, max_concurrency: int) -> None:
        self.max_concurrency = max(1, int(max_concurrency))
        self._semaphore = asyncio.Semaphore(self.max_concurrency)

    async def run_task(
        self,
        mode: str,
        job_coro: Callable[[], Awaitable[Any]],
        stage_name: str = "execute",
    ) -> dict[str, Any]:
        """执行单个图片任务，并输出统一的结果结构。"""

        task_start_time = time.perf_counter()
        wait_start_time = task_start_time

        async with self._semaphore:
            queue_wait_ms = int((time.perf_counter() - wait_start_time) * 1000)
            try:
                payload = await job_coro()
            except Exception as exc:  # noqa: BLE001
                total_elapsed_ms = int((time.perf_counter() - task_start_time) * 1000)
                return {
                    "success": False,
                    "mode": mode,
                    "error_stage": stage_name,
                    "error_message": str(exc),
                    "timings": {
                        "queue_wait_ms": queue_wait_ms,
                        "total_elapsed_ms": total_elapsed_ms,
                    },
                }

            total_elapsed_ms = int((time.perf_counter() - task_start_time) * 1000)
            return {
                "success": True,
                "mode": mode,
                "payload": payload,
                "timings": {
                    "queue_wait_ms": queue_wait_ms,
                    "total_elapsed_ms": total_elapsed_ms,
                },
            }
