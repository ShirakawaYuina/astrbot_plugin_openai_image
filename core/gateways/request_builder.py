"""图片接口请求体构造。"""

from __future__ import annotations


def build_generate_payload(
    model: str,
    prompt: str,
    size: str | None = None,
    quality: str = "auto",
    moderation: str = "low",
) -> dict:
    """构造 Responses 文生图请求体。"""

    image_tool = _build_image_generation_tool(
        action="generate",
        size=size,
        quality=quality,
        moderation=moderation,
    )
    return {
        "model": model,
        "input": str(prompt or "").strip(),
        "tools": [image_tool],
        "tool_choice": {"type": "image_generation"},
    }


def build_images_generate_payload(
    model: str,
    prompt: str,
    size: str | None = None,
    quality: str = "auto",
    moderation: str = "low",
) -> dict:
    """构造 Images generations 文生图请求体。"""

    payload = {
        "model": model,
        "prompt": str(prompt or "").strip(),
    }
    _append_size_if_configured(payload, size)
    _append_image_options(payload, quality=quality, moderation=moderation)
    return payload


def build_edit_payload(
    model: str,
    prompt: str,
    data_url: str = "",
    data_urls: list[str] | None = None,
    size: str | None = None,
    quality: str = "auto",
    moderation: str = "low",
) -> dict:
    """构造 Responses 改图请求体。"""

    image_urls = _normalize_image_urls(data_url=data_url, data_urls=data_urls)
    image_tool = _build_image_generation_tool(
        action="edit",
        size=size,
        quality=quality,
        moderation=moderation,
    )
    content_blocks = [
        {
            "type": "input_text",
            "text": str(prompt or "").strip(),
        },
    ]
    content_blocks.extend(
        {
            "type": "input_image",
            "image_url": image_url,
        }
        for image_url in image_urls
    )

    return {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": content_blocks,
            }
        ],
        "tools": [image_tool],
        "tool_choice": {"type": "image_generation"},
    }


def _normalize_image_urls(
    *,
    data_url: str = "",
    data_urls: list[str] | None = None,
) -> list[str]:
    """兼容单图旧参数与多图新参数，过滤空值后返回可发送的图片列表。"""

    if data_urls is not None:
        return [
            str(item or "").strip() for item in data_urls if str(item or "").strip()
        ]

    clean_data_url = str(data_url or "").strip()
    if clean_data_url:
        return [clean_data_url]
    return []


def _build_image_generation_tool(
    action: str,
    size: str | None = None,
    quality: str = "auto",
    moderation: str = "low",
) -> dict:
    """构造 Responses 图片工具参数，集中附加默认图像策略。"""

    image_tool = {"type": "image_generation", "action": action}
    _append_size_if_configured(image_tool, size)
    _append_image_options(image_tool, quality=quality, moderation=moderation)
    return image_tool


def _append_size_if_configured(payload: dict, size: str | None) -> None:
    """仅在用户显式配置尺寸时写入 size 字段，避免改变默认生成行为。"""

    clean_size = str(size or "").strip()
    if clean_size:
        payload["size"] = clean_size


def _append_image_options(payload: dict, quality: str, moderation: str) -> None:
    """写入图片质量与审核策略，默认值由插件统一控制。"""

    payload["quality"] = str(quality or "auto").strip().lower() or "auto"
    payload["moderation"] = str(moderation or "low").strip().lower() or "low"
