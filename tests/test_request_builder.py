from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_module():
    return importlib.import_module("core.gateways.request_builder")


def test_build_generate_payload_uses_string_content():
    module = _load_module()

    payload = module.build_generate_payload(
        model="gpt-image-2",
        prompt="生成一只猫",
    )

    assert payload == {
        "model": "gpt-image-2",
        "input": "生成一只猫",
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


def test_build_images_generate_payload_uses_images_endpoint_schema():
    module = _load_module()

    payload = module.build_images_generate_payload(
        model="gpt-image-2",
        prompt="生成一只猫",
    )

    assert payload == {
        "model": "gpt-image-2",
        "prompt": "生成一只猫",
        "quality": "auto",
        "moderation": "low",
    }


def test_build_generate_payload_adds_size_to_responses_tool_when_configured():
    module = _load_module()

    payload = module.build_generate_payload(
        model="gpt-image-2",
        prompt="生成一只猫",
        size="1024x1536",
    )

    assert payload["tools"] == [
        {
            "type": "image_generation",
            "action": "generate",
            "size": "1024x1536",
            "quality": "auto",
            "moderation": "low",
        }
    ]


def test_build_generate_payload_overrides_quality_and_moderation():
    module = _load_module()

    payload = module.build_generate_payload(
        model="gpt-image-2",
        prompt="生成一只猫",
        quality="high",
        moderation="auto",
    )

    assert payload["tools"] == [
        {
            "type": "image_generation",
            "action": "generate",
            "quality": "high",
            "moderation": "auto",
        }
    ]


def test_build_images_generate_payload_adds_size_when_configured():
    module = _load_module()

    payload = module.build_images_generate_payload(
        model="gpt-image-2",
        prompt="生成一只猫",
        size="1536x1024",
    )

    assert payload["size"] == "1536x1024"
    assert payload["quality"] == "auto"
    assert payload["moderation"] == "low"


def test_build_images_generate_payload_overrides_quality_and_moderation():
    module = _load_module()

    payload = module.build_images_generate_payload(
        model="gpt-image-2",
        prompt="生成一只猫",
        quality="medium",
        moderation="auto",
    )

    assert payload["quality"] == "medium"
    assert payload["moderation"] == "auto"


def test_build_edit_payload_uses_input_text_and_input_image_blocks():
    module = _load_module()

    payload = module.build_edit_payload(
        model="gpt-image-2",
        prompt="改成动漫风格",
        data_url="data:image/png;base64,aGVsbG8=",
    )

    assert payload == {
        "model": "gpt-image-2",
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "改成动漫风格",
                    },
                    {
                        "type": "input_image",
                        "image_url": "data:image/png;base64,aGVsbG8=",
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


def test_build_edit_payload_accepts_multiple_input_images():
    module = _load_module()

    payload = module.build_edit_payload(
        model="gpt-5.4-mini",
        prompt="把两张图融合成电影海报",
        data_urls=[
            "data:image/png;base64,Zmlyc3Q=",
            "data:image/jpeg;base64,c2Vjb25k",
        ],
    )

    assert payload["input"][0]["content"] == [
        {
            "type": "input_text",
            "text": "把两张图融合成电影海报",
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


def test_build_edit_payload_adds_size_to_responses_tool_when_configured():
    module = _load_module()

    payload = module.build_edit_payload(
        model="gpt-image-2",
        prompt="改成动漫风格",
        data_url="data:image/png;base64,aGVsbG8=",
        size="1024x1024",
    )

    assert payload["tools"] == [
        {
            "type": "image_generation",
            "action": "edit",
            "size": "1024x1024",
            "quality": "auto",
            "moderation": "low",
        }
    ]


def test_build_edit_payload_overrides_quality_and_moderation():
    module = _load_module()

    payload = module.build_edit_payload(
        model="gpt-image-2",
        prompt="改成动漫风格",
        data_url="data:image/png;base64,aGVsbG8=",
        quality="low",
        moderation="auto",
    )

    assert payload["tools"] == [
        {
            "type": "image_generation",
            "action": "edit",
            "quality": "low",
            "moderation": "auto",
        }
    ]
