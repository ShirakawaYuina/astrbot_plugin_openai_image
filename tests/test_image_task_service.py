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
