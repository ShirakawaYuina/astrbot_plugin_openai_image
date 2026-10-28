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


def test_config_schema_replaces_template_list_with_json_file_reference():
    schema_path = ROOT / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert "base_url" not in schema
    assert "api_key" not in schema
    assert "image_providers" not in schema
    assert schema["active_provider_id"]["type"] == "string"
    assert "default" in schema["active_provider_id"]["options"]
    assert "providers.json" in schema["active_provider_id"]["hint"]
    assert "model" not in schema
    assert "endpoint_type" not in schema
    assert "image_proxy_url" not in schema
    assert "prompt_optimizer_model" not in schema
    assert "prompt_optimizer_base_url" not in schema
    assert "prompt_optimizer_api_key" not in schema


def test_load_providers_from_file_initializes_default(tmp_path: Path):
    module = _load_module()
    target_file = tmp_path / "providers.json"
    assert not target_file.exists()

    providers = module.load_providers_from_file(target_file)
    assert len(providers) == 1
    assert providers[0].provider_id == "default"
    assert providers[0].name == "默认供应商"
    assert providers[0].base_url == "https://cdn.jucode.top/v1"
    assert target_file.is_file()


def test_load_providers_from_file_reads_custom_providers(tmp_path: Path):
    module = _load_module()
    target_file = tmp_path / "providers.json"
    target_file.write_text(
        json.dumps(
            [
                {
                    "provider_id": "silicon",
                    "name": "硅基流动",
                    "base_url": "https://api.siliconflow.cn/v1",
                    "api_key": "sk-123",
                    "model": "deepseek-ai/Janus-Pro-7B",
                    "endpoint_type": "images",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    providers = module.load_providers_from_file(target_file)
    assert len(providers) == 1
    assert providers[0].provider_id == "silicon"
    assert providers[0].name == "硅基流动"
    assert providers[0].model == "deepseek-ai/Janus-Pro-7B"
    assert providers[0].endpoint_type == "images"


def test_resolve_active_image_provider_reads_from_file(tmp_path: Path):
    module = _load_module()
    target_file = tmp_path / "providers.json"
    target_file.write_text(
        json.dumps(
            [
                {
                    "provider_id": "first",
                    "name": "供应商1",
                    "base_url": "https://first.example.com/v1",
                    "model": "model-1",
                },
                {
                    "provider_id": "second",
                    "name": "供应商2",
                    "base_url": "https://second.example.com/v1",
                    "model": "model-2",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    provider = module.resolve_active_image_provider(
        {"active_provider_id": "second"},
        providers_path=target_file,
    )
    assert provider.provider_id == "second"
    assert provider.name == "供应商2"
    assert provider.model == "model-2"


def test_sync_schema_provider_options():
    module = _load_module()
    schema = {
        "active_provider_id": {
            "options": ["old"],
            "labels": ["旧供应商"],
        },
        "fallback_provider_id": {
            "options": [""],
            "labels": ["旧回退"],
        },
    }
    providers = [
        module.ImageProviderConfig(
            provider_id="p1",
            name="供应商A",
            base_url="https://a.com",
            api_key="",
            proxy_url="",
            model="m1",
            endpoint_type="responses",
        ),
        module.ImageProviderConfig(
            provider_id="p2",
            name="供应商B",
            base_url="https://b.com",
            api_key="",
            proxy_url="",
            model="m2",
            endpoint_type="images",
        ),
    ]

    module.sync_schema_provider_options(schema, providers)
    assert schema["active_provider_id"]["options"] == ["p1", "p2"]
    assert schema["active_provider_id"]["labels"] == ["m1（供应商A）", "m2（供应商B）"]
    assert schema["fallback_provider_id"]["options"] == ["", "p1", "p2"]
    assert schema["fallback_provider_id"]["labels"] == ["不启用回退", "m1（供应商A）", "m2（供应商B）"]


def test_normalize_providers_list_deduplicates_and_assigns_unique_ids():
    module = _load_module()
    raw = [
        {
            "name": "jucode",
            "base_url": "https://cdn.jucode.top/v1",
            "model": "gpt-image-2",
        },
        {
            "name": "jucode",
            "base_url": "https://cdn.jucode.top/v1",
            "model": "gpt-image-2.5-sunburst",
        },
        {
            "name": "jucode",
            "base_url": "https://cdn.jucode.top/v1",
            "model": "gpt-image-2.5-sunburst",
        },
    ]

    providers, has_changes = module.normalize_providers_list(raw)
    assert has_changes is True
    assert len(providers) == 3
    assert providers[0].provider_id == "default"
    assert providers[1].provider_id == "jucode_gpt-image-2.5-sunburst"
    assert providers[2].provider_id == "jucode_gpt-image-2.5-sunburst_2"
    assert len({p.provider_id for p in providers}) == 3


def test_load_providers_from_file_auto_heals_duplicate_ids(tmp_path: Path):
    module = _load_module()
    target_file = tmp_path / "providers.json"
    target_file.write_text(
        json.dumps(
            [
                {
                    "provider_id": "default",
                    "name": "jucode",
                    "base_url": "https://cdn.jucode.top/v1",
                    "model": "gpt-image-2",
                },
                {
                    "provider_id": "default",
                    "name": "jucode",
                    "base_url": "https://cdn.jucode.top/v1",
                    "model": "gpt-image-2.5-sunburst",
                },
            ]
        ),
        encoding="utf-8",
    )

    providers = module.load_providers_from_file(target_file)
    assert len(providers) == 2
    assert providers[0].provider_id == "default"
    assert providers[1].provider_id == "jucode_gpt-image-2.5-sunburst"

    saved_data = json.loads(target_file.read_text(encoding="utf-8"))
    assert saved_data[0]["provider_id"] == "default"
    assert saved_data[1]["provider_id"] == "jucode_gpt-image-2.5-sunburst"

    # 验证能精准根据第二个 provider_id 激活第二个模型
    active = module.resolve_active_image_provider(
        {"active_provider_id": "jucode_gpt-image-2.5-sunburst"},
        providers_path=target_file,
    )
    assert active.model == "gpt-image-2.5-sunburst"


def test_write_raw_providers_json_deduplicates_ids(tmp_path: Path):
    module = _load_module()
    target_file = tmp_path / "providers.json"
    raw_content = json.dumps(
        [
            {
                "name": "my_api",
                "base_url": "https://api.example.com/v1",
                "model": "model-a",
            },
            {
                "name": "my_api",
                "base_url": "https://api.example.com/v1",
                "model": "model-b",
            },
        ]
    )

    providers = module.write_raw_providers_json(raw_content, target_file)
    assert len(providers) == 2
    assert providers[0].provider_id == "default"
    assert providers[1].provider_id == "my_api_model-b"

    disk_data = json.loads(target_file.read_text(encoding="utf-8"))
    assert disk_data[1]["provider_id"] == "my_api_model-b"


def test_resolve_fallback_image_provider(tmp_path: Path):
    module = _load_module()
    target_file = tmp_path / "providers.json"
    target_file.write_text(
        json.dumps(
            [
                {
                    "provider_id": "primary",
                    "name": "主供应商",
                    "base_url": "https://p1.com/v1",
                    "model": "model-1",
                },
                {
                    "provider_id": "backup",
                    "name": "备用供应商",
                    "base_url": "https://p2.com/v1",
                    "model": "model-2",
                },
            ]
        ),
        encoding="utf-8",
    )

    # 正常解析回退配置
    fallback = module.resolve_fallback_image_provider(
        {"fallback_provider_id": "backup"},
        providers_path=target_file,
    )
    assert fallback is not None
    assert fallback.provider_id == "backup"
    assert fallback.model == "model-2"

    # 未配置或显式禁用
    assert (
        module.resolve_fallback_image_provider(
            {"fallback_provider_id": ""},
            providers_path=target_file,
        )
        is None
    )
    assert (
        module.resolve_fallback_image_provider(
            {"fallback_provider_id": "none"},
            providers_path=target_file,
        )
        is None
    )
    assert (
        module.resolve_fallback_image_provider(
            {},
            providers_path=target_file,
        )
        is None
    )

    # 配置了不存在的 ID
    assert (
        module.resolve_fallback_image_provider(
            {"fallback_provider_id": "nonexistent"},
            providers_path=target_file,
        )
        is None
    )


def test_is_same_model():
    module = _load_module()
    p1 = module.ImageProviderConfig(
        provider_id="p1",
        name="供应商A",
        base_url="https://a.com",
        api_key="",
        proxy_url="",
        model="gpt-image-2",
        endpoint_type="responses",
    )
    p2 = module.ImageProviderConfig(
        provider_id="p2",
        name="供应商B",
        base_url="https://b.com",
        api_key="",
        proxy_url="",
        model="gpt-image-2",
        endpoint_type="images",
    )
    p3 = module.ImageProviderConfig(
        provider_id="p3",
        name="供应商C",
        base_url="https://c.com",
        api_key="",
        proxy_url="",
        model="dall-e-3",
        endpoint_type="images",
    )

    # 任一为空
    assert module.is_same_model(None, p1) is True
    assert module.is_same_model(p1, None) is True

    # 相同 provider_id
    assert module.is_same_model(p1, p1) is True

    # 不同 provider_id 但 model 名称相同（大小写不敏感）
    assert module.is_same_model(p1, p2) is True

    # 完全不同的模型
    assert module.is_same_model(p1, p3) is False




def test_is_local_model():
    module = _load_module()

    # provider_id 以 local 开头（大小写不敏感）
    assert module.is_local_model(provider_id="local_sd") is True
    assert module.is_local_model(provider_id="LOCAL_FASTAPI") is True
    assert module.is_local_model(provider_id="local") is True

    # model 以 local 开头（大小写不敏感）
    assert module.is_local_model(model="local-flux") is True
    assert module.is_local_model(model="LOCAL_SDXL") is True

    # 使用 provider 实例
    local_p1 = module.ImageProviderConfig(
        provider_id="local_custom",
        name="本地接口",
        base_url="http://127.0.0.1:8000/v1",
        api_key="",
        proxy_url="",
        model="sdxl",
        endpoint_type="responses",
    )
    assert module.is_local_model(provider=local_p1) is True

    local_p2 = module.ImageProviderConfig(
        provider_id="custom_id",
        name="本地模型",
        base_url="http://127.0.0.1:8000/v1",
        api_key="",
        proxy_url="",
        model="local-sd15",
        endpoint_type="responses",
    )
    assert module.is_local_model(provider=local_p2) is True

    # 非 local 接口
    cloud_p = module.ImageProviderConfig(
        provider_id="default",
        name="默认接口",
        base_url="https://api.example.com/v1",
        api_key="",
        proxy_url="",
        model="gpt-image-2",
        endpoint_type="responses",
    )
    assert module.is_local_model(provider=cloud_p) is False
    assert module.is_local_model(provider_id="default", model="gpt-5.4-mini") is False
    assert module.is_local_model() is False


def test_apply_prompt_prefix():
    module = _load_module()

    # 用户示例：前缀 abc，用户提示词 一只猫 -> abc，一只猫
    assert module.apply_prompt_prefix("一只猫", "abc") == "abc，一只猫"

    # 前缀已带全角标点，不重复拼接
    assert module.apply_prompt_prefix("一只猫", "abc，") == "abc，一只猫"
    assert module.apply_prompt_prefix("一只猫", "高清壁纸；") == "高清壁纸；一只猫"

    # 前缀已带半角逗号加空格
    assert module.apply_prompt_prefix("一只猫", "abc, ") == "abc, 一只猫"
    assert module.apply_prompt_prefix("a cute cat", "abc, ") == "abc, a cute cat"

    # 英文提示词与英文前缀，自动半角逗号加空格
    assert (
        module.apply_prompt_prefix("1girl, solo", "masterpiece, best quality")
        == "masterpiece, best quality, 1girl, solo"
    )

    # 提示词开头已有多余标点
    assert module.apply_prompt_prefix("，一只猫", "abc") == "abc，一只猫"
    assert module.apply_prompt_prefix(", a dog", "photorealistic") == "photorealistic, a dog"

    # 幂等性：已追加过前缀，不再重复追加
    assert module.apply_prompt_prefix("abc，一只猫", "abc") == "abc，一只猫"
    assert module.apply_prompt_prefix("abc, a dog", "abc") == "abc, a dog"
    assert module.apply_prompt_prefix("abc a dog", "abc") == "abc a dog"

    # 空前缀或空提示词
    assert module.apply_prompt_prefix("一只猫", "") == "一只猫"
    assert module.apply_prompt_prefix("一只猫", "  ") == "一只猫"
    assert module.apply_prompt_prefix("", "abc") == ""
    assert module.apply_prompt_prefix("   ", "abc") == ""
