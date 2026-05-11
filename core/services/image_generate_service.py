"""图片生成应用服务。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..gateways.request_builder import (
    build_generate_payload,
    build_images_generate_payload,
)
from ..gateways.response_parser import parse_image_response, parse_images_response


class ImageGenerateService:
    """负责组织文本生图的请求、解析与缓存流程。"""

    def __init__(self, gateway: Any, cache_store: Any) -> None:
        self.gateway = gateway
        self.cache_store = cache_store

    async def generate(
        self,
        model: str,
        prompt: str,
        negative_prompt: str = "",
        endpoint_type: str = "responses",
        size: str | None = None,
        quality: str = "auto",
        moderation: str = "low",
    ) -> Path:
        """执行一次文本生图。"""

        clean_endpoint_type = str(endpoint_type or "responses").strip().lower()
        if clean_endpoint_type == "images":
            payload = build_images_generate_payload(
                model=model,
                prompt=prompt,
                negative_prompt=negative_prompt,
                size=size,
                quality=quality,
                moderation=moderation,
            )
            response_data = await self.gateway.request_image_generation(payload)
            parser = parse_images_response
        else:
            payload = build_generate_payload(
                model=model,
                prompt=prompt,
                negative_prompt=negative_prompt,
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
                "[OpenAIImage][generate] 响应解析失败 raw_response_summary=%s",
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
                "mode": "generate",
            },
        )
        return image_path

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
