"""图片接口响应解析。"""

from __future__ import annotations

import base64
import mimetypes
from typing import Any
from urllib.parse import urlsplit

from ..models import ParsedImageResponse
from ..utils.mime_utils import extension_from_mime_type


def parse_image_response(response_data: dict[str, Any]) -> ParsedImageResponse:
    """从 Responses 接口响应中解析图片结果。"""

    output_items = response_data.get("output")
    if not isinstance(output_items, list) or not output_items:
        raise ValueError("响应中缺少 output")

    image_result = _extract_image_result(output_items)
    if not image_result:
        raise ValueError("响应中缺少图片结果")

    return _parse_image_result(image_result)


def parse_images_response(response_data: dict[str, Any]) -> ParsedImageResponse:
    """从 Images generations 接口响应中解析图片结果。"""

    data_items = response_data.get("data")
    if not isinstance(data_items, list) or not data_items:
        raise ValueError("响应中缺少 data")

    image_result = _extract_images_result(data_items)
    if not image_result:
        raise ValueError("响应中缺少图片结果")

    return _parse_image_result(image_result)


def _extract_image_result(output_items: list[Any]) -> str:
    """从 Responses 的 output 列表里提取图片结果。"""

    for item in output_items:
        if not isinstance(item, dict):
            continue
        if str(item.get("type", "") or "").strip() != "image_generation_call":
            continue

        result = str(item.get("result", "") or "").strip()
        if result:
            return result

    return ""


def _extract_images_result(data_items: list[Any]) -> str:
    """从 Images generations 的 data 列表里提取第一张图片结果。"""

    for item in data_items:
        if not isinstance(item, dict):
            continue

        b64_json = str(item.get("b64_json", "") or "").strip()
        if b64_json:
            return b64_json

        url = str(item.get("url", "") or "").strip()
        if url:
            return url

    return ""


def _parse_image_result(image_result: str) -> ParsedImageResponse:
    """把图片结果解析为本地二进制或待下载 URL。"""

    clean_result = str(image_result or "").strip()
    if not clean_result:
        raise ValueError("响应中缺少图片结果")

    # OpenAI 兼容接口有的返回 b64_json，有的返回 url；URL 需要保留到服务层异步下载。
    if _is_http_url(clean_result):
        mime_type = _guess_mime_type_from_url(clean_result)
        return ParsedImageResponse(
            mime_type=mime_type,
            extension=extension_from_mime_type(mime_type),
            image_url=clean_result,
        )

    try:
        image_bytes = base64.b64decode(clean_result, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("base64 图片数据解析失败") from exc

    return ParsedImageResponse(
        mime_type="image/png",
        extension=".png",
        image_bytes=image_bytes,
    )


def _is_http_url(value: str) -> bool:
    """判断上游图片结果是否为可下载的 HTTP(S) URL。"""

    parsed = urlsplit(str(value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _guess_mime_type_from_url(image_url: str) -> str:
    """从 URL 路径推断 MIME 类型，无法推断时按 PNG 处理。"""

    parsed = urlsplit(str(image_url or "").strip())
    # Windows 默认 mimetypes 表可能不含 webp，因此先用插件明确支持的图片后缀做稳定映射。
    extension_mapping = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }
    for extension, mime_type in extension_mapping.items():
        if parsed.path.lower().endswith(extension):
            return mime_type

    guessed = mimetypes.guess_type(parsed.path)[0]
    return guessed or "image/png"
