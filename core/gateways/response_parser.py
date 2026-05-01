"""图片接口响应解析。"""

from __future__ import annotations

import base64
from typing import Any

from ..models import ParsedImageResponse


def parse_image_response(response_data: dict[str, Any]) -> ParsedImageResponse:
    """从 Responses 接口响应中解析图片结果。"""

    output_items = response_data.get("output")
    if not isinstance(output_items, list) or not output_items:
        raise ValueError("响应中缺少 output")

    image_result = _extract_image_result(output_items)
    if not image_result:
        raise ValueError("响应中缺少图片结果")

    try:
        image_bytes = base64.b64decode(image_result, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("base64 图片数据解析失败") from exc

    return ParsedImageResponse(
        mime_type="image/png",
        extension=".png",
        image_bytes=image_bytes,
    )


def parse_images_response(response_data: dict[str, Any]) -> ParsedImageResponse:
    """从 Images generations 接口响应中解析图片结果。"""

    data_items = response_data.get("data")
    if not isinstance(data_items, list) or not data_items:
        raise ValueError("响应中缺少 data")

    image_result = _extract_images_b64_json(data_items)
    if not image_result:
        raise ValueError("响应中缺少图片结果")

    try:
        image_bytes = base64.b64decode(image_result, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("base64 图片数据解析失败") from exc

    return ParsedImageResponse(
        mime_type="image/png",
        extension=".png",
        image_bytes=image_bytes,
    )


def _extract_image_result(output_items: list[Any]) -> str:
    """从 Responses 的 output 列表里提取图片 base64 结果。"""

    for item in output_items:
        if not isinstance(item, dict):
            continue
        if str(item.get("type", "") or "").strip() != "image_generation_call":
            continue

        result = str(item.get("result", "") or "").strip()
        if result:
            return result

    return ""


def _extract_images_b64_json(data_items: list[Any]) -> str:
    """从 Images generations 的 data 列表里提取第一张图片 base64。"""

    for item in data_items:
        if not isinstance(item, dict):
            continue

        b64_json = str(item.get("b64_json", "") or "").strip()
        if b64_json:
            return b64_json

    return ""
