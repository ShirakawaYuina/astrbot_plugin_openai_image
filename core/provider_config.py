"""图片接口供应商配置解析与文件持久化。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

DEFAULT_PROVIDER_NAME = "默认供应商"
DEFAULT_PROVIDER_ID = "default"
DEFAULT_ENDPOINT_TYPE = "responses"
DEFAULT_RESPONSES_MODEL = "gpt-5.4-mini"
DEFAULT_IMAGES_MODEL = "gpt-image-2"
ACTIVE_PROVIDER_ID_CONFIG_KEY = "active_provider_id"
IMAGE_PROVIDERS_CONFIG_KEY = "image_providers"
PROVIDERS_FILE_NAME = "providers.json"
PLUGIN_NAME = "astrbot_plugin_openai_image"


@dataclass(frozen=True)
class ImageProviderConfig:
    """运行时实际使用的图片接口供应商配置。

    这里把模型名和端点类型一起挂在供应商条目上，避免同一插件里
    不同图片接口共用一套全局配置导致切换时互相覆盖。
    """

    provider_id: str
    name: str
    base_url: str
    api_key: str
    proxy_url: str
    model: str
    endpoint_type: str


def get_default_providers_path() -> Path:
    """返回 providers.json 的默认存储路径。

    Returns:
        指向 plugin_data 目录下当前插件 providers.json 的 Path 对象。
    """
    return (
        Path(get_astrbot_plugin_data_path())
        / PLUGIN_NAME
        / PROVIDERS_FILE_NAME
    )


def build_default_provider_payload() -> list[dict[str, Any]]:
    """返回默认供应商列表配置模板。

    Returns:
        包含默认供应商字典的列表。
    """
    return [
        {
            "provider_id": DEFAULT_PROVIDER_ID,
            "name": DEFAULT_PROVIDER_NAME,
            "base_url": "https://cdn.jucode.top/v1",
            "api_key": "",
            "proxy_url": "",
            "model": DEFAULT_RESPONSES_MODEL,
            "endpoint_type": DEFAULT_ENDPOINT_TYPE,
        }
    ]


def load_providers_from_file(
    providers_path: Path | None = None,
    legacy_providers: list[Any] | None = None,
) -> list[ImageProviderConfig]:
    """从本地 JSON 文件读取供应商配置列表。

    Args:
        providers_path: 可选的自定义 JSON 文件路径，默认读取 plugin_data 下的文件。
        legacy_providers: 文件不存在时可选传入的旧配置列表，用于自动迁移。

    Returns:
        解析并标准化后的 ImageProviderConfig 列表。
    """
    path = providers_path or get_default_providers_path()
    if not path.is_file():
        initial_payload = (
            legacy_providers
            if legacy_providers and isinstance(legacy_providers, list)
            else build_default_provider_payload()
        )
        save_providers_to_file(initial_payload, path)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("[OpenAIImage] 读取 providers.json 失败: %s", exc)
        return []

    if isinstance(data, dict):
        data = data.get("providers", list(data.values()))
    if not isinstance(data, list):
        return []

    providers: list[ImageProviderConfig] = []
    for item in data:
        normalized = _normalize_provider_payload(item)
        if normalized is not None:
            providers.append(normalized)
    return providers


def save_providers_to_file(
    payload: list[Any],
    providers_path: Path | None = None,
) -> None:
    """将供应商配置列表写入本地 JSON 文件。

    Args:
        payload: 供应商字典或 ImageProviderConfig 列表。
        providers_path: 目标文件路径，默认为 plugin_data 下的 providers.json。
    """
    path = providers_path or get_default_providers_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = [
        item if isinstance(item, dict) else _provider_to_dict(item)
        for item in payload
    ]
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")


def sync_schema_provider_options(
    schema: dict[str, Any] | None,
    providers: list[ImageProviderConfig],
) -> None:
    """将供应商列表的 ID 与名称同步到 active_provider_id 的 options 与 labels 中。

    Args:
        schema: AstrBot 配置 Schema 字典。
        providers: 当前加载的有效供应商列表。
    """
    if not isinstance(schema, dict) or ACTIVE_PROVIDER_ID_CONFIG_KEY not in schema:
        return
    options = [p.provider_id for p in providers] or [DEFAULT_PROVIDER_ID]
    labels = [p.name for p in providers] or [DEFAULT_PROVIDER_NAME]
    schema[ACTIVE_PROVIDER_ID_CONFIG_KEY]["options"] = options
    schema[ACTIVE_PROVIDER_ID_CONFIG_KEY]["labels"] = labels


def resolve_active_image_provider(
    config: dict[str, Any],
    providers_path: Path | None = None,
) -> ImageProviderConfig:
    """从供应商列表中解析当前启用的图片接口供应商。

    运行时优先使用 providers.json 里的供应商条目，若 config 显式传入了
    image_providers 且为非空列表，则以传入项优先以保持兼容。并通过 active_provider_id
    选择当前启用项。若选择的槽位不存在，抛错提示用户。

    Args:
        config: 插件全局配置字典。
        providers_path: 可选的 providers.json 文件路径。

    Returns:
        解析出的当前有效 ImageProviderConfig。

    Raises:
        ValueError: 当没有可用供应商或未找到对应 provider_id 时抛出。
    """
    active_provider_id = str(
        config.get(ACTIVE_PROVIDER_ID_CONFIG_KEY, DEFAULT_PROVIDER_ID) or ""
    ).strip()

    if IMAGE_PROVIDERS_CONFIG_KEY in config and isinstance(
        config[IMAGE_PROVIDERS_CONFIG_KEY], list
    ):
        configured_providers = config[IMAGE_PROVIDERS_CONFIG_KEY]
        providers = [
            p
            for p in (_normalize_provider_payload(item) for item in configured_providers)
            if p is not None
        ]
    else:
        legacy = (
            config.get(IMAGE_PROVIDERS_CONFIG_KEY)
            if isinstance(config.get(IMAGE_PROVIDERS_CONFIG_KEY), list)
            else None
        )
        providers = load_providers_from_file(providers_path, legacy_providers=legacy)

    if not providers:
        raise ValueError("至少启用一个图片供应商，并填写图片接口基础地址")

    for provider in providers:
        if provider.provider_id == active_provider_id:
            return provider

    raise ValueError(f"未找到启用的图片供应商: {active_provider_id or '(空值)'}")


def _provider_to_dict(provider: ImageProviderConfig) -> dict[str, Any]:
    """将 ImageProviderConfig 转换为可序列化字典。"""
    return {
        "provider_id": provider.provider_id,
        "name": provider.name,
        "base_url": provider.base_url,
        "api_key": provider.api_key,
        "proxy_url": provider.proxy_url,
        "model": provider.model,
        "endpoint_type": provider.endpoint_type,
    }


def _normalize_provider_payload(payload: Any) -> ImageProviderConfig | None:
    """标准化单个供应商条目，跳过缺少地址的无效条目。

    model / endpoint_type 属于供应商自身配置，这里会先校验端点类型，
    再按端点类型补上该供应商的默认模型。
    """
    if not isinstance(payload, dict):
        return None

    base_url = str(payload.get("base_url", "") or "").strip().rstrip("/")
    if not base_url:
        return None

    provider_id = (
        str(payload.get("provider_id", "") or "").strip() or DEFAULT_PROVIDER_ID
    )
    name = str(payload.get("name", "") or "").strip() or DEFAULT_PROVIDER_NAME
    api_key = str(payload.get("api_key", "") or "").strip()
    proxy_url = str(payload.get("proxy_url", "") or "").strip()
    endpoint_type = _normalize_endpoint_type(payload.get("endpoint_type"))
    model = str(payload.get("model", "") or "").strip() or _resolve_default_model(
        endpoint_type
    )
    return ImageProviderConfig(
        provider_id=provider_id,
        name=name,
        base_url=base_url,
        api_key=api_key,
        proxy_url=proxy_url,
        model=model,
        endpoint_type=endpoint_type,
    )


def _normalize_endpoint_type(value: Any) -> str:
    """将端点类型限制为插件真正支持的两种值。"""
    clean_endpoint_type = str(value or "").strip().lower()
    if clean_endpoint_type == "images":
        return "images"
    return DEFAULT_ENDPOINT_TYPE


def _resolve_default_model(endpoint_type: str) -> str:
    """按端点类型给出该供应商的默认模型。"""
    if endpoint_type == "images":
        return DEFAULT_IMAGES_MODEL
    return DEFAULT_RESPONSES_MODEL
