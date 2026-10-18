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
FALLBACK_PROVIDER_ID_CONFIG_KEY = "fallback_provider_id"
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


import re

def _slugify(text: str) -> str:
    """清理字符串为合法的 ASCII 标识符片段。"""
    cleaned = re.sub(r"[^a-zA-Z0-9_\-\.]+", "_", text.strip().lower()).strip("_")
    return cleaned or "item"


def normalize_providers_list(
    raw_list: list[Any],
) -> tuple[list[ImageProviderConfig], bool]:
    """标准化供应商列表并确保每个条目的 provider_id 全局唯一。

    Args:
        raw_list: 包含供应商字典的原始列表。

    Returns:
        元组 (解析并规范化后的 ImageProviderConfig 列表, 原始 provider_id 是否发生过修正或补全)。
    """
    seen_ids: set[str] = set()
    normalized: list[ImageProviderConfig] = []
    has_changes = False

    for item in raw_list:
        if not isinstance(item, dict):
            continue

        base_url = str(item.get("base_url", "") or "").strip().rstrip("/")
        if not base_url:
            continue

        endpoint_type = _normalize_endpoint_type(item.get("endpoint_type"))
        model = str(item.get("model", "") or "").strip() or _resolve_default_model(
            endpoint_type
        )
        name = str(item.get("name", "") or "").strip() or DEFAULT_PROVIDER_NAME
        api_key = str(item.get("api_key", "") or "").strip()
        proxy_url = str(item.get("proxy_url", "") or "").strip()

        raw_id = str(item.get("provider_id", "") or "").strip()

        # 生成候选 provider_id
        if raw_id and raw_id != DEFAULT_PROVIDER_ID:
            candidate_id = _slugify(raw_id)
        elif not seen_ids and (not raw_id or raw_id == DEFAULT_PROVIDER_ID):
            # 第一个条目未指定或显式指定 default 时，优先沿用 "default" 以保持向下兼容
            candidate_id = DEFAULT_PROVIDER_ID
        else:
            # 其它情况根据供应商名称与模型名称自动构建具名标识
            name_slug = _slugify(name)
            model_slug = _slugify(model)
            if name_slug and name_slug != _slugify(DEFAULT_PROVIDER_NAME):
                candidate_id = f"{name_slug}_{model_slug}"
            else:
                candidate_id = model_slug

        # 确保 candidate_id 全局唯一，避免前端 select 出现重复 value 导致选项回退
        final_id = candidate_id
        if final_id in seen_ids:
            model_slug = _slugify(model)
            if model_slug not in final_id:
                final_id = f"{final_id}_{model_slug}"
            counter = 2
            base = final_id
            while final_id in seen_ids:
                final_id = f"{base}_{counter}"
                counter += 1

        seen_ids.add(final_id)
        if final_id != raw_id:
            has_changes = True

        normalized.append(
            ImageProviderConfig(
                provider_id=final_id,
                name=name,
                base_url=base_url,
                api_key=api_key,
                proxy_url=proxy_url,
                model=model,
                endpoint_type=endpoint_type,
            )
        )

    return normalized, has_changes


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

    providers, has_changes = normalize_providers_list(data)
    if has_changes and providers:
        try:
            save_providers_to_file(providers, path)
            logger.info(
                "[OpenAIImage] 已对 providers.json 中缺失或重复的 provider_id 完成唯一化持久化"
            )
        except Exception as exc:
            logger.warning("[OpenAIImage] 自动持久化更新 providers.json 失败: %s", exc)

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


def read_raw_providers_json(providers_path: Path | None = None) -> str:
    """读取 providers.json 的原始 JSON 文本内容。若文件不存在则初始化。

    Args:
        providers_path: 可选的自定义文件路径。

    Returns:
        JSON 格式字符串。
    """
    path = providers_path or get_default_providers_path()
    if not path.is_file():
        load_providers_from_file(path)
    return path.read_text(encoding="utf-8")


def write_raw_providers_json(
    content: str,
    providers_path: Path | None = None,
) -> list[ImageProviderConfig]:
    """校验并写入原始 JSON 文本到 providers.json。

    Args:
        content: 用户提交的 JSON 文本字符串。
        providers_path: 可选的自定义文件路径。

    Returns:
        解析出的有效 ImageProviderConfig 列表。

    Raises:
        ValueError: 当 JSON 格式错误或未包含有效供应商时抛出。
    """
    try:
        data = json.loads(content)
    except Exception as exc:
        raise ValueError(f"JSON 格式解析失败: {exc}") from exc

    if isinstance(data, dict):
        raw_list = data.get("providers", list(data.values()))
    elif isinstance(data, list):
        raw_list = data
    else:
        raise ValueError("providers.json 顶层必须是数组或包含 providers 的对象")

    if not isinstance(raw_list, list):
        raise ValueError("供应商配置列表必须是数组")

    normalized, _ = normalize_providers_list(raw_list)
    if not normalized:
        raise ValueError("至少需要包含一个填写了有效 base_url 的供应商")

    path = providers_path or get_default_providers_path()
    save_providers_to_file(normalized, path)
    return normalized


def format_provider_label(provider: ImageProviderConfig) -> str:
    """按 '模型名称（供应商名称）' 格式化供应商选项显示标签。

    Args:
        provider: 供应商配置对象。

    Returns:
        形如 'gpt-image-2（jucode）' 的显示标签字符串。
    """
    model = str(provider.model or "").strip()
    name = str(provider.name or "").strip()
    if model and name:
        return f"{model}（{name}）"
    return model or name or DEFAULT_PROVIDER_NAME


def sync_schema_provider_options(
    schema: dict[str, Any] | None,
    providers: list[ImageProviderConfig],
) -> None:
    """将供应商列表的 ID 与模型（供应商）名称同步到 active_provider_id 与 fallback_provider_id 的 options 与 labels 中。

    Args:
        schema: AstrBot 配置 Schema 字典。
        providers: 当前加载的有效供应商列表。
    """
    if not isinstance(schema, dict):
        return

    default_label = f"{DEFAULT_RESPONSES_MODEL}（{DEFAULT_PROVIDER_NAME}）"
    options = [p.provider_id for p in providers] or [DEFAULT_PROVIDER_ID]
    labels = [format_provider_label(p) for p in providers] or [default_label]

    if ACTIVE_PROVIDER_ID_CONFIG_KEY in schema:
        schema[ACTIVE_PROVIDER_ID_CONFIG_KEY]["options"] = options
        schema[ACTIVE_PROVIDER_ID_CONFIG_KEY]["labels"] = labels

    if FALLBACK_PROVIDER_ID_CONFIG_KEY in schema:
        schema[FALLBACK_PROVIDER_ID_CONFIG_KEY]["options"] = [""] + options
        schema[FALLBACK_PROVIDER_ID_CONFIG_KEY]["labels"] = ["不启用回退"] + labels


def resolve_active_image_provider(
    config: dict[str, Any],
    providers_path: Path | None = None,
) -> ImageProviderConfig:
    """从供应商列表中解析当前启用的图片接口供应商。

    若指定了 providers_path 则直接从该文件读取。未指定时优先检查 config 中的
    image_providers（保持单测隔离与兼容）；若均未指定则读取持久化的 providers.json。
    通过 active_provider_id 选择当前启用项。若选择的槽位不存在，抛错提示用户。

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

    if providers_path is not None:
        providers = load_providers_from_file(providers_path)
    elif IMAGE_PROVIDERS_CONFIG_KEY in config and isinstance(
        config[IMAGE_PROVIDERS_CONFIG_KEY], list
    ):
        providers, _ = normalize_providers_list(config[IMAGE_PROVIDERS_CONFIG_KEY])
    else:
        legacy = (
            config.get(IMAGE_PROVIDERS_CONFIG_KEY)
            if isinstance(config.get(IMAGE_PROVIDERS_CONFIG_KEY), list)
            else None
        )
        providers = load_providers_from_file(None, legacy_providers=legacy)

    if not providers:
        raise ValueError("至少启用一个图片供应商，并填写图片接口基础地址")

    for provider in providers:
        if provider.provider_id == active_provider_id:
            return provider

    raise ValueError(f"未找到启用的图片供应商: {active_provider_id or '(空值)'}")


def resolve_fallback_image_provider(
    config: dict[str, Any],
    providers_path: Path | None = None,
) -> ImageProviderConfig | None:
    """从配置中解析失败回退图片供应商。

    若未配置 fallback_provider_id、配置为空或指定为 'none' 等禁用值，返回 None。
    若配置的 ID 在可用供应商列表中不存在，返回 None。

    Args:
        config: 插件全局配置字典。
        providers_path: 可选的 providers.json 文件路径。

    Returns:
        解析出的回退 ImageProviderConfig，未配置或未找到有效条目时返回 None。
    """
    fallback_id = str(
        config.get(FALLBACK_PROVIDER_ID_CONFIG_KEY, "") or ""
    ).strip()
    if not fallback_id or fallback_id.lower() in (
        "none",
        "null",
        "false",
        "off",
        "disable",
        "",
    ):
        return None

    if providers_path is not None:
        providers = load_providers_from_file(providers_path)
    elif IMAGE_PROVIDERS_CONFIG_KEY in config and isinstance(
        config[IMAGE_PROVIDERS_CONFIG_KEY], list
    ):
        providers, _ = normalize_providers_list(config[IMAGE_PROVIDERS_CONFIG_KEY])
    else:
        legacy = (
            config.get(IMAGE_PROVIDERS_CONFIG_KEY)
            if isinstance(config.get(IMAGE_PROVIDERS_CONFIG_KEY), list)
            else None
        )
        providers = load_providers_from_file(None, legacy_providers=legacy)

    for provider in providers:
        if provider.provider_id == fallback_id:
            return provider

    return None


def is_same_model(
    primary: ImageProviderConfig | None,
    fallback: ImageProviderConfig | None,
) -> bool:
    """判断主模型与回退模型是否为同一模型。

    Args:
        primary: 主供应商配置。
        fallback: 回退供应商配置。

    Returns:
        若任一为空、ID 相同或模型名称相同则返回 True，否则返回 False。
    """
    if primary is None or fallback is None:
        return True
    if primary.provider_id == fallback.provider_id:
        return True
    primary_model = str(primary.model or "").strip().lower()
    fallback_model = str(fallback.model or "").strip().lower()
    if primary_model == fallback_model:
        return True
    return False


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
    providers, _ = normalize_providers_list([payload])
    return providers[0] if providers else None


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
