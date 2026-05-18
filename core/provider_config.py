"""图片接口供应商配置解析。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_PROVIDER_NAME = "默认供应商"
DEFAULT_PROVIDER_ID = "default"
ACTIVE_PROVIDER_ID_CONFIG_KEY = "active_provider_id"
IMAGE_PROVIDERS_CONFIG_KEY = "image_providers"
LEGACY_BASE_URL_CONFIG_KEY = "base_url"
LEGACY_API_KEY_CONFIG_KEY = "api_key"


@dataclass(frozen=True)
class ImageProviderConfig:
    """运行时实际使用的图片接口供应商配置。"""

    provider_id: str
    name: str
    base_url: str
    api_key: str
    proxy_url: str


def resolve_active_image_provider(config: dict[str, Any]) -> ImageProviderConfig:
    """从新旧配置中解析当前启用的图片接口供应商。

    新版配置允许用户添加多个供应商，并通过 active_provider_id 下拉选择启用项。
    旧版配置只有 base_url/api_key 两个顶层字段；这里保留兼容迁移逻辑，
    避免用户升级插件后因为配置结构变化导致图片功能立即不可用。
    """

    active_provider_id = str(
        config.get(ACTIVE_PROVIDER_ID_CONFIG_KEY, DEFAULT_PROVIDER_ID) or ""
    ).strip()
    configured_providers = config.get(IMAGE_PROVIDERS_CONFIG_KEY, [])
    if isinstance(configured_providers, list):
        fallback_provider: ImageProviderConfig | None = None
        for provider_payload in configured_providers:
            provider = _normalize_provider_payload(provider_payload)
            if provider is None:
                continue
            if provider.provider_id == active_provider_id:
                return provider
            if fallback_provider is None:
                fallback_provider = provider
        if fallback_provider is not None:
            return fallback_provider

    legacy_provider = _resolve_legacy_provider(config)
    if legacy_provider is not None:
        return legacy_provider

    raise ValueError("至少启用一个图片供应商，并填写图片接口基础地址")


def _normalize_provider_payload(payload: Any) -> ImageProviderConfig | None:
    """标准化单个供应商条目，跳过缺少地址的无效条目。"""

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
    return ImageProviderConfig(
        provider_id=provider_id,
        name=name,
        base_url=base_url,
        api_key=api_key,
        proxy_url=proxy_url,
    )


def _resolve_legacy_provider(config: dict[str, Any]) -> ImageProviderConfig | None:
    """从旧版 base_url/api_key 顶层字段迁移出一个默认供应商。"""

    base_url = str(config.get(LEGACY_BASE_URL_CONFIG_KEY, "") or "").strip().rstrip("/")
    if not base_url:
        return None

    return ImageProviderConfig(
        provider_id=DEFAULT_PROVIDER_ID,
        name=DEFAULT_PROVIDER_NAME,
        base_url=base_url,
        api_key=str(config.get(LEGACY_API_KEY_CONFIG_KEY, "") or "").strip(),
        proxy_url="",
    )
