from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_generate_module():
    return importlib.import_module("core.services.image_generate_service")


def _load_edit_module():
    return importlib.import_module("core.services.image_edit_service")


class _FakeGateway:
    def __init__(self, response_data: dict):
        self.response_data = response_data
        self.recorded_payloads: list[dict] = []

    async def request_response(self, payload: dict) -> dict:
        self.recorded_payloads.append(payload)
        return self.response_data

    async def request_image_generation(self, payload: dict) -> dict:
        self.recorded_payloads.append(payload)
        return self.response_data

    async def request_image_edit(self, data: dict, files: list[tuple]) -> dict:
        self.recorded_payloads.append({"data": data, "files": files})
        return self.response_data


class _FakeCacheStore:
    def __init__(self, result_path: Path):
        self.result_path = result_path
        self.saved_images: list[tuple[bytes, str]] = []

    def save_image(self, image_bytes: bytes, extension: str) -> Path:
        self.saved_images.append((image_bytes, extension))
        return self.result_path


class _FakeLogger:
    def __init__(self) -> None:
        self.error_calls: list[tuple[tuple, dict]] = []

    def error(self, *args, **kwargs) -> None:
        self.error_calls.append((args, kwargs))


@pytest.mark.asyncio
async def test_generate_service_builds_payload_and_caches_image(tmp_path: Path):
    module = _load_generate_module()
    gateway = _FakeGateway(
        {
            "output": [
                {
                    "type": "image_generation_call",
                    "result": "aGVsbG8=",
                }
            ]
        }
    )
    cache_store = _FakeCacheStore(tmp_path / "generated.png")
    service = module.ImageGenerateService(gateway=gateway, cache_store=cache_store)

    result_path = await service.generate(
        model="gpt-draw-1024x1536",
        prompt="生成一只小猫",
        negative_prompt="低清晰度、文字水印",
    )

    assert result_path == tmp_path / "generated.png"
    assert gateway.recorded_payloads == [
        {
            "model": "gpt-draw-1024x1536",
            "input": "生成一只小猫\n\nMust Avoid: 低清晰度、文字水印",
            "tools": [
                {
                    "type": "image_generation",
                    "action": "generate",
                    "quality": "auto",
                    "moderation": "low",
                }
            ],
            "tool_choice": {"type": "image_generation"},
        }
    ]
    assert cache_store.saved_images == [(b"hello", ".png")]


@pytest.mark.asyncio
async def test_generate_service_can_use_images_generations_endpoint(tmp_path: Path):
    module = _load_generate_module()
    gateway = _FakeGateway(
        {
            "data": [
                {
                    "b64_json": "aGVsbG8=",
                }
            ]
        }
    )
    cache_store = _FakeCacheStore(tmp_path / "generated.png")
    service = module.ImageGenerateService(gateway=gateway, cache_store=cache_store)

    result_path = await service.generate(
        model="gpt-image-2",
        prompt="生成一只小猫",
        endpoint_type="images",
    )

    assert result_path == tmp_path / "generated.png"
    assert gateway.recorded_payloads == [
        {
            "model": "gpt-image-2",
            "prompt": "生成一只小猫",
            "quality": "auto",
            "moderation": "low",
        }
    ]
    assert cache_store.saved_images == [(b"hello", ".png")]


@pytest.mark.asyncio
async def test_generate_service_includes_response_summary_when_output_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module = _load_generate_module()
    gateway = _FakeGateway(
        {
            "error": {
                "message": "temporary upstream response",
            },
            "request_id": "req_demo",
        }
    )
    cache_store = _FakeCacheStore(tmp_path / "generated.png")
    fake_logger = _FakeLogger()
    monkeypatch.setattr(module, "logger", fake_logger)
    service = module.ImageGenerateService(gateway=gateway, cache_store=cache_store)

    with pytest.raises(ValueError, match="top_level_keys=error,request_id"):
        await service.generate(
            model="gpt-draw-1024x1536",
            prompt="测试响应摘要",
        )

    assert fake_logger.error_calls
    first_args = fake_logger.error_calls[0][0]
    assert "raw_response_summary=%s" in first_args[0]


@pytest.mark.asyncio
async def test_edit_service_builds_multimodal_payload_and_caches_image(
    tmp_path: Path,
):
    module = _load_edit_module()
    gateway = _FakeGateway(
        {
            "output": [
                {
                    "type": "image_generation_call",
                    "result": "d29ybGQ=",
                }
            ]
        }
    )
    cache_store = _FakeCacheStore(tmp_path / "edited.jpg")
    service = module.ImageEditService(gateway=gateway, cache_store=cache_store)

    result_path = await service.edit(
        model="gpt-draw-1024x1536",
        prompt="改成真人版",
        data_url="data:image/png;base64,abc123",
        negative_prompt="低清晰度、文字水印",
    )

    assert result_path == tmp_path / "edited.jpg"
    assert gateway.recorded_payloads == [
        {
            "model": "gpt-draw-1024x1536",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "改成真人版\n\nMust Avoid: 低清晰度、文字水印",
                        },
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,abc123",
                        },
                    ],
                }
            ],
            "tools": [
                {
                    "type": "image_generation",
                    "action": "edit",
                    "quality": "auto",
                    "moderation": "low",
                }
            ],
            "tool_choice": {"type": "image_generation"},
        }
    ]
    assert cache_store.saved_images == [(b"world", ".png")]


@pytest.mark.asyncio
async def test_edit_service_builds_payload_with_multiple_input_images(
    tmp_path: Path,
):
    module = _load_edit_module()
    gateway = _FakeGateway(
        {
            "output": [
                {
                    "type": "image_generation_call",
                    "result": "d29ybGQ=",
                }
            ]
        }
    )
    cache_store = _FakeCacheStore(tmp_path / "edited.jpg")
    service = module.ImageEditService(gateway=gateway, cache_store=cache_store)

    await service.edit(
        model="gpt-5.4-mini",
        prompt="融合两张参考图",
        data_urls=[
            "data:image/png;base64,Zmlyc3Q=",
            "data:image/jpeg;base64,c2Vjb25k",
        ],
    )

    content_blocks = gateway.recorded_payloads[0]["input"][0]["content"]
    assert content_blocks == [
        {
            "type": "input_text",
            "text": "融合两张参考图",
        },
        {
            "type": "input_image",
            "image_url": "data:image/png;base64,Zmlyc3Q=",
        },
        {
            "type": "input_image",
            "image_url": "data:image/jpeg;base64,c2Vjb25k",
        },
    ]


@pytest.mark.asyncio
async def test_edit_service_can_use_images_edits_endpoint_with_multiple_images(
    tmp_path: Path,
):
    module = _load_edit_module()
    gateway = _FakeGateway(
        {
            "data": [
                {
                    "b64_json": "d29ybGQ=",
                }
            ]
        }
    )
    cache_store = _FakeCacheStore(tmp_path / "edited.png")
    service = module.ImageEditService(gateway=gateway, cache_store=cache_store)

    result_path = await service.edit(
        model="gpt-image-2",
        prompt="将图中的角色换成星见雅",
        data_urls=[
            "data:image/png;base64,Zmlyc3Q=",
            "data:image/jpeg;base64,c2Vjb25k",
        ],
        endpoint_type="images",
    )

    assert result_path == tmp_path / "edited.png"
    assert gateway.recorded_payloads == [
        {
            "data": {
                "model": "gpt-image-2",
                "prompt": "将图中的角色换成星见雅",
                "response_format": "b64_json",
                "quality": "auto",
                "moderation": "low",
            },
            "files": [
                ("image_1.png", b"first", "image/png"),
                ("image_2.jpg", b"second", "image/jpeg"),
            ],
        }
    ]
    assert cache_store.saved_images == [(b"world", ".png")]


@pytest.mark.asyncio
async def test_generate_service_passes_quality_and_moderation_to_images_endpoint(
    tmp_path: Path,
):
    module = _load_generate_module()
    gateway = _FakeGateway({"data": [{"b64_json": "aGVsbG8="}]})
    cache_store = _FakeCacheStore(tmp_path / "generated.png")
    service = module.ImageGenerateService(gateway=gateway, cache_store=cache_store)

    await service.generate(
        model="gpt-image-2",
        prompt="生成一只小猫",
        endpoint_type="images",
        quality="high",
        moderation="auto",
    )

    assert gateway.recorded_payloads[0]["quality"] == "high"
    assert gateway.recorded_payloads[0]["moderation"] == "auto"


@pytest.mark.asyncio
async def test_edit_service_passes_quality_and_moderation_to_images_endpoint(
    tmp_path: Path,
):
    module = _load_edit_module()
    gateway = _FakeGateway({"data": [{"b64_json": "d29ybGQ="}]})
    cache_store = _FakeCacheStore(tmp_path / "edited.png")
    service = module.ImageEditService(gateway=gateway, cache_store=cache_store)

    await service.edit(
        model="gpt-image-2",
        prompt="改成电影海报",
        data_url="data:image/png;base64,Zmlyc3Q=",
        endpoint_type="images",
        quality="medium",
        moderation="auto",
    )

    assert gateway.recorded_payloads[0]["data"]["quality"] == "medium"
    assert gateway.recorded_payloads[0]["data"]["moderation"] == "auto"
