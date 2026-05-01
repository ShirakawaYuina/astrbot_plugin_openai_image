from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_module():
    return importlib.import_module("core.commands")


def test_parse_command_defaults_to_single_image():
    module = _load_module()

    parsed = module.parse_command_payload("生成一只猫")

    assert parsed.count == 1
    assert parsed.prompt == "生成一只猫"
    assert parsed.quality == "auto"
    assert parsed.moderation == "low"


def test_parse_command_reads_explicit_count():
    module = _load_module()

    parsed = module.parse_command_payload("3 生成一只猫")

    assert parsed.count == 3
    assert parsed.prompt == "生成一只猫"
    assert parsed.size is None


def test_parse_command_reads_size_option_after_count():
    module = _load_module()

    parsed = module.parse_command_payload("2 --size 1024x1536 生成竖版角色图")

    assert parsed.count == 2
    assert parsed.size == "1024x1536"
    assert parsed.prompt == "生成竖版角色图"


def test_parse_command_reads_short_size_alias_before_prompt():
    module = _load_module()

    parsed = module.parse_command_payload("-s landscape 生成横版风景图")

    assert parsed.count == 1
    assert parsed.size == "1536x1024"
    assert parsed.prompt == "生成横版风景图"


def test_parse_command_reads_quality_and_moderation_options():
    module = _load_module()

    parsed = module.parse_command_payload(
        "2 --quality high --moderation auto 生成电影海报"
    )

    assert parsed.count == 2
    assert parsed.quality == "high"
    assert parsed.moderation == "auto"
    assert parsed.prompt == "生成电影海报"


def test_parse_command_reads_short_quality_and_moderation_options():
    module = _load_module()

    parsed = module.parse_command_payload("-q medium -m low 生成横版风景图")

    assert parsed.quality == "medium"
    assert parsed.moderation == "low"
    assert parsed.prompt == "生成横版风景图"


def test_parse_command_reads_equals_quality_and_moderation_options():
    module = _load_module()

    parsed = module.parse_command_payload("--quality=low --moderation=auto 生成一只猫")

    assert parsed.quality == "low"
    assert parsed.moderation == "auto"
    assert parsed.prompt == "生成一只猫"


def test_parse_command_rejects_invalid_size():
    module = _load_module()

    with pytest.raises(ValueError, match="尺寸"):
        module.parse_command_payload("--size 1000x1000 生成一只猫")


def test_parse_command_rejects_invalid_quality():
    module = _load_module()

    with pytest.raises(ValueError, match="质量"):
        module.parse_command_payload("--quality ultra 生成一只猫")


def test_parse_command_rejects_invalid_moderation():
    module = _load_module()

    with pytest.raises(ValueError, match="审核"):
        module.parse_command_payload("--moderation strict 生成一只猫")


def test_parse_command_rejects_too_small_custom_size():
    module = _load_module()

    with pytest.raises(ValueError, match="总像素"):
        module.parse_command_payload("--size 512x512 生成一只猫")


def test_parse_command_keeps_trailing_ampersand_as_prompt_text():
    module = _load_module()

    parsed = module.parse_command_payload("2 一位站在雨夜街头的少女&")

    assert parsed.count == 2
    assert parsed.prompt == "一位站在雨夜街头的少女&"


def test_parse_command_keeps_middle_ampersand_as_normal_text():
    module = _load_module()

    parsed = module.parse_command_payload("1 black & white portrait")

    assert parsed.count == 1
    assert parsed.prompt == "black & white portrait"


def test_parse_command_rejects_invalid_count():
    module = _load_module()

    with pytest.raises(ValueError, match="数量必须为正整数"):
        module.parse_command_payload("0 一只猫")


def test_parse_command_rejects_empty_prompt():
    module = _load_module()

    with pytest.raises(ValueError, match="提示词不能为空"):
        module.parse_command_payload("2")
