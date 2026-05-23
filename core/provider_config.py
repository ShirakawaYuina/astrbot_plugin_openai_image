"""图片接口供应商配置解析。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_PROVIDER_NAME = "默认供应商"
DEFAULT_PROVIDER_ID = "default"
DEFAULT_ENDPOINT_TYPE = "responses"
DEFAULT_RESPONSES_MODEL = "gpt-5.4-mini"
DEFAULT_IMAGES_MODEL = "gpt-image-2"
ACTIVE_PROVIDER_ID_CONFIG_KEY = "active_provider_id"
IMAGE_PROVIDERS_CONFIG_KEY = "image_providers"


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


def resolve_active_image_provider(config: dict[str, Any]) -> ImageProviderConfig:
    """从供应商列表中解析当前启用的图片接口供应商。

    运行时只认 `image_providers` 里的供应商条目，并通过 active_provider_id
    选择当前启用项。若选择的槽位不存在，就直接抛错，让配置问题在启动时暴露，
    避免把请求悄悄发到别的图片接口。
    """

    active_provider_id = str(
        config.get(ACTIVE_PROVIDER_ID_CONFIG_KEY, DEFAULT_PROVIDER_ID) or ""
    ).strip()
    configured_providers = config.get(IMAGE_PROVIDERS_CONFIG_KEY, [])
    if isinstance(configured_providers, list):
        has_valid_provider = False
        for provider_payload in configured_providers:
            provider = _normalize_provider_payload(provider_payload)
            if provider is None:
                continue
            has_valid_provider = True
            if provider.provider_id == active_provider_id:
                return provider

        if not has_valid_provider:
            raise ValueError("至少启用一个图片供应商，并填写图片接口基础地址")
        raise ValueError(f"未找到启用的图片供应商: {active_provider_id or '(空值)'}")

    raise ValueError("至少启用一个图片供应商，并填写图片接口基础地址")


def _normalize_provider_payload(payload: Any) -> ImageProviderConfig | None:
    """标准化单个供应商条目，跳过缺少地址的无效条目。

    model / endpoint_type 现在都属于供应商自身配置，因此这里会先校验
    端点类型，再按端点类型补上该供应商的默认模型。
    """

    if not isinstance(payload, dict):
        return None

    # template_list 会带上 __template_key，它只服务于 WebUI 模板选择，运行时无需使用。
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
