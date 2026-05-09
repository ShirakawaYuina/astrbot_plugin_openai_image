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
                },
                {
                    "provider_id": "primary",
                    "name": "主供应商",
                    "base_url": " https://primary.example.com/v1/ ",
                    "api_key": " primary-key ",
                },
            ]
        }
    )

    assert provider.name == "主供应商"
    assert provider.base_url == "https://primary.example.com/v1"
    assert provider.api_key == "primary-key"


def test_resolve_active_image_provider_accepts_provider_name_as_selection_value():
    module = _load_module()

    provider = module.resolve_active_image_provider(
        {
            "active_provider_id": "主供应商",
            "image_providers": [
                {
                    "provider_id": "backup",
                    "name": "备用供应商",
                    "base_url": "https://backup.example.com/v1",
                    "api_key": "backup-key",
                },
                {
                    "provider_id": "primary",
                    "name": "主供应商",
                    "base_url": "https://primary.example.com/v1",
                    "api_key": "primary-key",
                },
            ],
        }
    )

    assert provider.provider_id == "primary"
    assert provider.name == "主供应商"


def test_resolve_active_image_provider_falls_back_to_legacy_fields():
    module = _load_module()

    provider = module.resolve_active_image_provider(
        {
            "base_url": "https://legacy.example.com/v1",
            "api_key": "legacy-key",
        }
    )

    assert provider.name == "默认供应商"
    assert provider.base_url == "https://legacy.example.com/v1"
    assert provider.api_key == "legacy-key"


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
    provider_template = schema["image_providers"]["templates"]["openai_compatible"]
    assert provider_template["items"]["provider_id"]["invisible"] is True
