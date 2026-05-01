from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_config_schema_removes_prompt_enhancement_provider_selector():
    schema_path = ROOT / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert "english_prompt_enhancer_model" not in schema
    assert "strict_translation_enabled" not in schema
    assert "translation_base_url" not in schema
    assert "translation_api_key" not in schema
    assert "translation_model" not in schema
    assert "translation_timeout_seconds" not in schema


def test_config_schema_declares_custom_negative_prompt():
    schema_path = ROOT / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert schema["negative_prompt"]["description"] == "负面提示词"
    assert schema["negative_prompt"]["type"] == "string"
    assert schema["negative_prompt"]["default"] == ""


def test_prompt_translate_service_is_removed_with_enhancement_feature():
    service_path = ROOT / "core" / "services" / "prompt_translate_service.py"

    assert not service_path.exists()
