"""图片解析结果补全。"""

from __future__ import annotations

from typing import Any

from ..models import ParsedImageResponse
from ..utils.mime_utils import extension_from_mime_type


async def resolve_parsed_image_response(
    gateway: Any,
    parsed_response: ParsedImageResponse,
) -> ParsedImageResponse:
    """补全解析后的图片内容。

    base64 响应在解析阶段已经得到二进制；URL 响应必须在服务层通过网关下载，
    这样网络 I/O 仍然集中在异步流程中，解析器只负责识别响应结构。
    """

    if parsed_response.image_bytes:
        return parsed_response

    image_url = str(parsed_response.image_url or "").strip()
    if not image_url:
        raise ValueError("响应中缺少图片结果")

    download_image = getattr(gateway, "download_image", None)
    if not callable(download_image):
        raise RuntimeError("当前图片网关不支持下载 URL 图片结果")

    image_bytes, downloaded_mime_type = await download_image(image_url)
    if not image_bytes:
        raise ValueError("URL 图片下载结果为空")

    # 下载响应头优先作为真实格式依据；缺少 Content-Type 时沿用 URL 后缀推断结果。
    resolved_mime_type = _resolve_downloaded_mime_type(
        downloaded_mime_type=downloaded_mime_type,
        fallback_mime_type=parsed_response.mime_type,
    )
    parsed_response.mime_type = resolved_mime_type
    parsed_response.extension = extension_from_mime_type(resolved_mime_type)
    parsed_response.image_bytes = bytes(image_bytes)
    return parsed_response


def _resolve_downloaded_mime_type(
    *,
    downloaded_mime_type: str | None,
    fallback_mime_type: str,
) -> str:
    """规范化下载结果 MIME 类型，避免把错误页当作图片缓存。"""

    clean_downloaded_mime_type = (
        str(downloaded_mime_type or "").split(";", 1)[0].strip().lower()
    )
    if clean_downloaded_mime_type:
        if not clean_downloaded_mime_type.startswith("image/"):
            raise ValueError(
                f"URL 图片下载返回非图片内容: {clean_downloaded_mime_type}"
            )
        return clean_downloaded_mime_type

    return str(fallback_mime_type or "image/png").strip().lower() or "image/png"
