from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_module():
    return importlib.import_module("core.services.image_task_service")


@pytest.mark.asyncio
async def test_task_service_returns_stage_timings_for_success():
    module = _load_module()

    service = module.ImageTaskService(max_concurrency=2)

    async def fake_job():
        return "ok"

    result = await service.run_task(mode="generate", job_coro=fake_job)

    assert result["success"] is True
    assert result["mode"] == "generate"
    assert result["timings"]["total_elapsed_ms"] >= 0
    assert result["timings"]["queue_wait_ms"] >= 0
    assert result["timings"]["elapsed_ms"] >= 0


@pytest.mark.asyncio
async def test_task_service_records_failed_stage_name():
    module = _load_module()

    service = module.ImageTaskService(max_concurrency=1)

    async def fake_job():
        raise RuntimeError("boom")

    result = await service.run_task(mode="edit", job_coro=fake_job, stage_name="request")

    assert result["success"] is False
    assert result["mode"] == "edit"
    assert result["error_stage"] == "request"
    assert "boom" in result["error_message"]


@pytest.mark.asyncio
async def test_task_service_tracks_active_tasks():
    import asyncio

    module = _load_module()

    service = module.ImageTaskService(max_concurrency=2)
    assert service.active_tasks == 0

    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_job():
        started.set()
        await release.wait()
        return "done"

    task = asyncio.create_task(service.run_task(mode="generate", job_coro=fake_job))
    await started.wait()
    assert service.active_tasks == 1

    release.set()
    await task
    assert service.active_tasks == 0


@pytest.mark.asyncio
async def test_task_service_measures_execution_elapsed_ms_separately_from_queue_wait():
    import asyncio

    module = _load_module()

    service = module.ImageTaskService(max_concurrency=1)

    gate = asyncio.Event()

    async def first_job():
        await gate.wait()
        return "first"

    async def second_job():
        return "second"

    task1 = asyncio.create_task(service.run_task(mode="generate", job_coro=first_job))
    await asyncio.sleep(0.05)
    task2 = asyncio.create_task(service.run_task(mode="generate", job_coro=second_job))
    await asyncio.sleep(0.05)

    gate.set()
    res1, res2 = await asyncio.gather(task1, task2)

    assert res2["timings"]["queue_wait_ms"] >= 30
    assert res2["timings"]["total_elapsed_ms"] >= res2["timings"]["queue_wait_ms"]
    assert res2["timings"]["elapsed_ms"] < res2["timings"]["queue_wait_ms"]


