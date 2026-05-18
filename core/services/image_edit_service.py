"""图片编辑应用服务。"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..gateways.request_builder import build_edit_payload
from ..gateways.response_parser import parse_image_response, parse_images_response


class ImageEditService:
    """负责组织图生图的请求、解析与缓存流程。"""

    def __init__(self, gateway: Any, cache_store: Any) -> None:
        self.gateway = gateway
        self.cache_store = cache_store

    async def edit(
        self,
        model: str,
        prompt: str,
        data_url: str = "",
        data_urls: list[str] | None = None,
        endpoint_type: str = "responses",
        size: str | None = None,
        quality: str = "auto",
        moderation: str = "low",
    ) -> Path:
        """执行一次图片编辑。"""

        clean_endpoint_type = str(endpoint_type or "responses").strip().lower()
        if clean_endpoint_type == "images":
            image_files = self._build_images_edit_files(
                data_url=data_url,
                data_urls=data_urls,
            )
            response_data = await self.gateway.request_image_edit(
                data={
                    "model": model,
                    "prompt": str(prompt or "").strip(),
                    "response_format": "b64_json",
                    **_build_size_form_data(size),
                    **_build_image_option_form_data(
                        quality=quality,
                        moderation=moderation,
                    ),
                },
                files=image_files,
            )
            parser = parse_images_response
        else:
            payload = build_edit_payload(
                model=model,
                prompt=prompt,
                data_url=data_url,
                data_urls=data_urls,
                size=size,
                quality=quality,
                moderation=moderation,
            )
            response_data = await self.gateway.request_response(payload)
            parser = parse_image_response

        try:
            parsed_response = parser(response_data)
        except ValueError as exc:
            response_summary = self._summarize_response(response_data)
            logger.error(
                "[OpenAIImage][edit] 响应解析失败 raw_response_summary=%s",
                response_summary,
            )
            raise ValueError(f"{exc}; {response_summary}") from exc
        image_path = self.cache_store.save_image(
            parsed_response.image_bytes,
            parsed_response.extension,
        )
        self.cache_store.save_image_metadata(
            image_path,
            {
                "prompt": prompt,
                "size": size or "",
                "mode": "edit",
            },
        )
        return image_path

    @staticmethod
    def _build_images_edit_files(
        *,
        data_url: str = "",
        data_urls: list[str] | None = None,
    ) -> list[tuple[str, bytes, str]]:
        """将 data URL 输入图转换为 Images edits multipart 文件字段。"""

        clean_urls = data_urls if data_urls is not None else [data_url]
        image_files: list[tuple[str, bytes, str]] = []
        for index, image_url in enumerate(clean_urls, start=1):
            clean_image_url = str(image_url or "").strip()
            if not clean_image_url:
                continue

            mime_type, image_bytes = ImageEditService._decode_data_url(clean_image_url)
            extension = ImageEditService._extension_from_mime_type(mime_type)
            image_files.append((f"image_{index}{extension}", image_bytes, mime_type))

        if not image_files:
            raise ValueError("未检测到可用于编辑的输入图片")
        return image_files

    @staticmethod
    def _decode_data_url(data_url: str) -> tuple[str, bytes]:
        """解析 data URL，返回 MIME 类型与图片二进制。"""

        header, separator, payload = data_url.partition(",")
        if not separator or ";base64" not in header:
            raise ValueError("输入图片 data URL 格式无效")

        mime_type = header.removeprefix("data:").split(";", 1)[0].strip()
        if not mime_type:
            mime_type = "image/png"

        try:
            return mime_type, base64.b64decode(payload, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("输入图片 base64 数据解析失败") from exc

    @staticmethod
    def _extension_from_mime_type(mime_type: str) -> str:
        """根据 MIME 类型生成稳定文件名后缀，避免上游无法识别文件类型。"""

        normalized_mime_type = str(mime_type or "").lower().strip()
        if normalized_mime_type in {"image/jpeg", "image/jpg"}:
            return ".jpg"
        if normalized_mime_type == "image/webp":
            return ".webp"
        return ".png"

    @staticmethod
    def _summarize_response(response_data: Any) -> str:
        """生成可安全打印的响应摘要，便于排查线上接口兼容问题。"""

        if isinstance(response_data, dict):
            top_level_keys = ",".join(sorted(map(str, response_data.keys())))
            compact_json = json.dumps(response_data, ensure_ascii=False, default=str)
            return (
                f"top_level_keys={top_level_keys or '(empty)'} "
                f"payload_preview={compact_json[:300]}"
            )
        return f"response_type={type(response_data).__name__} payload_preview={str(response_data)[:300]}"


def _build_size_form_data(size: str | None) -> dict[str, str]:
    """为 Images edits multipart 表单补充尺寸字段，未配置时保持旧行为。"""

    clean_size = str(size or "").strip()
    if not clean_size:
        return {}
    return {"size": clean_size}


def _build_image_option_form_data(quality: str, moderation: str) -> dict[str, str]:
    """为 Images edits multipart 表单补充质量与审核策略。"""

    return {
        "quality": str(quality or "auto").strip().lower() or "auto",
        "moderation": str(moderation or "low").strip().lower() or "low",
    }
