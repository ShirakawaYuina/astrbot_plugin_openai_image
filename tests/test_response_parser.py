from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_module():
    return importlib.import_module("core.gateways.response_parser")


def test_parse_image_response_extracts_mime_and_bytes():
    module = _load_module()

    response = {
        "output": [
            {
                "type": "image_generation_call",
                "result": "aGVsbG8=",
            }
        ]
    }

    parsed = module.parse_image_response(response)

    assert parsed.mime_type == "image/png"
    assert parsed.extension == ".png"
    assert parsed.image_bytes == b"hello"


def test_parse_images_response_extracts_data_b64_json():
    module = _load_module()

    response = {
        "data": [
            {
                "b64_json": "aGVsbG8=",
            }
        ]
    }

    parsed = module.parse_images_response(response)

    assert parsed.mime_type == "image/png"
    assert parsed.extension == ".png"
    assert parsed.image_bytes == b"hello"


def test_parse_image_response_rejects_missing_output_items():
    module = _load_module()

    with pytest.raises(ValueError, match="响应中缺少 output"):
        module.parse_image_response({})


def test_parse_image_response_rejects_missing_image_generation_result():
    module = _load_module()

    response = {
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "这里只返回了一段普通文本",
                    }
                ],
            }
        ]
    }

    with pytest.raises(ValueError, match="响应中缺少图片结果"):
        module.parse_image_response(response)


def test_parse_image_response_uses_revised_prompt_without_affecting_decode():
    module = _load_module()

    response = {
        "output": [
            {
                "type": "image_generation_call",
                "result": "d29ybGQ=",
                "revised_prompt": "A cute domestic cat sitting on a windowsill",
            }
        ]
    }

    parsed = module.parse_image_response(response)

    assert parsed.mime_type == "image/png"
    assert parsed.extension == ".png"
    assert parsed.image_bytes == b"world"


def test_parse_image_response_rejects_invalid_base64():
    module = _load_module()

    response = {
        "output": [
            {
                "type": "image_generation_call",
                "result": "@@@",
            }
        ]
    }

    with pytest.raises(ValueError, match="base64 图片数据解析失败"):
        module.parse_image_response(response)
