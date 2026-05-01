"""从消息事件中提取待编辑图片。"""

from __future__ import annotations

from typing import Any


def _normalize_component_type(component: Any) -> str:
    """兼容 AstrBot 枚举类型与字符串类型的组件标识。"""

    raw_type = getattr(component, "type", "")
    if hasattr(raw_type, "value"):
        return str(getattr(raw_type, "value", "")).strip().lower()
    return str(raw_type or "").strip().lower()


def _find_first_image_component(components: list[Any]) -> Any | None:
    """从消息组件列表中找到第一张图片。"""

    for component in components:
        if _normalize_component_type(component) == "image":
            return component
    return None


def _find_image_components(components: list[Any]) -> list[Any]:
    """从消息组件列表中按原顺序收集所有图片。"""

    return [
        component
        for component in components
        if _normalize_component_type(component) == "image"
    ]


def extract_first_image_component(event: Any) -> Any | None:
    """优先从回复消息中取图，找不到时再回退到当前消息。"""

    message_components = list(
        getattr(getattr(event, "message_obj", None), "message", []) or []
    )
    for component in message_components:
        if _normalize_component_type(component) != "reply":
            continue
        reply_chain = list(getattr(component, "chain", []) or [])
        reply_image = _find_first_image_component(reply_chain)
        if reply_image is not None:
            return reply_image

    return _find_first_image_component(message_components)


def extract_image_components(event: Any) -> list[Any]:
    """优先从回复消息收集多张图，找不到时再回退到当前消息多图。"""

    message_components = list(
        getattr(getattr(event, "message_obj", None), "message", []) or []
    )
    for component in message_components:
        if _normalize_component_type(component) != "reply":
            continue
        reply_chain = list(getattr(component, "chain", []) or [])
        reply_images = _find_image_components(reply_chain)
        if reply_images:
            return reply_images

    return _find_image_components(message_components)


def extract_first_at_target(event: Any) -> str | None:
    """从当前消息中提取第一个被 @ 的目标 ID。"""

    message_components = list(
        getattr(getattr(event, "message_obj", None), "message", []) or []
    )
    for component in message_components:
        if _normalize_component_type(component) != "at":
            continue

        for field_name in ("qq", "user_id", "target"):
            value = str(getattr(component, field_name, "") or "").strip()
            if value:
                return value

    return None
