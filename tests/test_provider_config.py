from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_module():
    return importlib.import_module("core.provider_config")


def test_resolve_active_image_provider_uses_dropdown_selected_provider_id():
    module = _load_module()

    provider = module.resolve_active_image_provider(
        {
            "active_provider_id": "primary",
            "image_providers": [
                {
                    "provider_id": "backup",
                    "name": "备用供应商",
                    "base_url": "https://backup.example.com/v1",
                    "api_key": "backup-key",
                    "proxy_url": "http://127.0.0.1:7891",
                    "model": "gpt-backup",
                    "endpoint_type": "responses",
                },
                {
                    "provider_id": "primary",
                    "name": "主供应商",
                    "base_url": " https://primary.example.com/v1/ ",
                    "api_key": " primary-key ",
                    "proxy_url": " http://127.0.0.1:7890 ",
                    "model": " gpt-image-primary ",
                    "endpoint_type": " images ",
                },
            ]
        }
    )

    assert provider.name == "主供应商"
    assert provider.base_url == "https://primary.example.com/v1"
    assert provider.api_key == "primary-key"
    assert provider.proxy_url == "http://127.0.0.1:7890"
    assert provider.model == "gpt-image-primary"
    assert provider.endpoint_type == "images"


def test_resolve_active_image_provider_uses_provider_local_defaults():
    module = _load_module()

    provider = module.resolve_active_image_provider(
        {
            "image_providers": [
                {
                    "provider_id": "default",
                    "name": "默认供应商",
                    "base_url": "https://default.example.com/v1",
                    "api_key": "default-key",
                }
            ]
        }
    )

    assert provider.name == "默认供应商"
    assert provider.base_url == "https://default.example.com/v1"
    assert provider.api_key == "default-key"
    assert provider.proxy_url == ""
    assert provider.endpoint_type == "responses"
    assert provider.model == "gpt-5.4-mini"


def test_resolve_active_image_provider_rejects_unknown_provider_id():
    module = _load_module()

    try:
        module.resolve_active_image_provider(
            {
                "active_provider_id": "missing",
                "image_providers": [
                    {
                        "provider_id": "default",
                        "base_url": "https://default.example.com/v1",
                    }
                ],
            }
        )
    except ValueError as exc:
        assert "未找到启用的图片供应商" in str(exc)
    else:
        raise AssertionError("启用供应商槽位不存在时应该抛出 ValueError")


def test_resolve_active_image_provider_rejects_missing_base_url():
    module = _load_module()

    try:
        module.resolve_active_image_provider({"image_providers": []})
    except ValueError as exc:
        assert "至少启用一个图片供应商" in str(exc)
    else:
        raise AssertionError("缺少可用供应商时应该抛出 ValueError")


def test_config_schema_replaces_base_url_and_api_key_with_provider_list():
    schema_path = ROOT / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert "base_url" not in schema
    assert "api_key" not in schema
    assert schema["active_provider_id"]["type"] == "string"
    assert "default" in schema["active_provider_id"]["options"]
    assert schema["image_providers"]["type"] == "template_list"
    assert "openai_compatible" in schema["image_providers"]["templates"]
    assert "model" not in schema
    assert "endpoint_type" not in schema
    provider_items = schema["image_providers"]["templates"]["openai_compatible"][
        "items"
    ]
    assert provider_items["proxy_url"]["type"] == "string"
    assert "供应商" in provider_items["proxy_url"]["hint"]
    assert provider_items["model"]["type"] == "string"
    assert provider_items["endpoint_type"]["default"] == "responses"
    assert provider_items["endpoint_type"]["options"] == ["responses", "images"]
    assert "image_proxy_url" not in schema
    assert "prompt_optimizer_model" not in schema
    assert "prompt_optimizer_base_url" not in schema
    assert "prompt_optimizer_api_key" not in schema
