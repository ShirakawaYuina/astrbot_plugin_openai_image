"""OpenAI 兼容图片网关。"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from astrbot.api import logger


def resolve_endpoint_candidates(base_url: str) -> list[str]:
    """根据配置的基础地址推导 Responses 接口地址。"""

    normalized = str(base_url or "").strip().rstrip("/")
    if not normalized:
        raise ValueError("图片接口地址不能为空")

    if normalized.endswith("/responses"):
        return [normalized]

    parsed = urlsplit(normalized)
    base_path = parsed.path.rstrip("/")
    merged_path = f"{base_path}/responses" if base_path else "/responses"
    return [urlunsplit((parsed.scheme, parsed.netloc, merged_path, "", ""))]


def resolve_images_generations_endpoint(base_url: str) -> str:
    """根据配置的基础地址推导 Images generations 接口地址。"""

    return _resolve_endpoint(base_url=base_url, suffix="/images/generations")


def resolve_images_edits_endpoint(base_url: str) -> str:
    """根据配置的基础地址推导 Images edits 接口地址。"""

    return _resolve_endpoint(base_url=base_url, suffix="/images/edits")


def _resolve_endpoint(base_url: str, suffix: str) -> str:
    """在用户填写站点 Base URL 时自动补全具体接口路径。"""

    normalized = str(base_url or "").strip().rstrip("/")
    if not normalized:
        raise ValueError("图片接口地址不能为空")

    clean_suffix = f"/{str(suffix or '').strip('/')}"
    if normalized.endswith(clean_suffix):
        return normalized

    parsed = urlsplit(normalized)
    base_path = parsed.path.rstrip("/")
    merged_path = f"{base_path}{clean_suffix}" if base_path else clean_suffix
    return urlunsplit((parsed.scheme, parsed.netloc, merged_path, "", ""))


class OpenAIImageGateway:
    """负责向 OpenAI 兼容图片接口发送请求。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_seconds: int = 120,
        proxy_url: str = "",
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._endpoint_candidates = resolve_endpoint_candidates(base_url)
        self._images_generations_endpoint = resolve_images_generations_endpoint(
            base_url
        )
        self._images_edits_endpoint = resolve_images_edits_endpoint(base_url)
        self._api_key = str(api_key or "").strip()
        self._timeout_seconds = max(5, int(timeout_seconds))
        # 代理只在真正访问图片接口的请求上下文中传入，避免影响网页后台、图库读取或其它 HTTP 调用。
        self._proxy_url = str(proxy_url or "").strip() or None
        self._session = session
        self._owned_session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        """关闭网关内部创建的会话。"""

        if self._owned_session and not self._owned_session.closed:
            await self._owned_session.close()
        self._owned_session = None

    async def request_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        """向 Responses 接口发送 JSON 请求。"""

        endpoint = self._endpoint_candidates[0]
        response_data = await self._post_json(endpoint=endpoint, payload=payload)
        if self._looks_like_success_response(response_data):
            return response_data

        business_error = self._build_business_error(response_data)
        logger.warning(
            "[OpenAIImage][gateway] Responses 接口返回业务错误 endpoint=%s detail=%s",
            endpoint,
            business_error,
        )
        raise RuntimeError(f"图片接口请求失败: {business_error}")

    async def request_image_generation(self, payload: dict[str, Any]) -> dict[str, Any]:
        """向 Images generations 接口发送 JSON 请求。"""

        response_data = await self._post_json(
            endpoint=self._images_generations_endpoint,
            payload=payload,
        )
        if self._looks_like_images_success_response(response_data):
            return response_data

        business_error = self._build_business_error(response_data)
        logger.warning(
            "[OpenAIImage][gateway] Images 接口返回业务错误 endpoint=%s detail=%s",
            self._images_generations_endpoint,
            business_error,
        )
        raise RuntimeError(f"图片接口请求失败: {business_error}")

    async def request_image_edit(
        self,
        data: dict[str, str],
        files: list[tuple[str, bytes, str]],
    ) -> dict[str, Any]:
        """向 Images edits 接口发送 multipart 表单请求。"""

        response_data = await self._post_multipart(
            endpoint=self._images_edits_endpoint,
            data=data,
            files=files,
        )
        if self._looks_like_images_success_response(response_data):
            return response_data

        business_error = self._build_business_error(response_data)
        logger.warning(
            "[OpenAIImage][gateway] Images edits 接口返回业务错误 endpoint=%s detail=%s",
            self._images_edits_endpoint,
            business_error,
        )
        raise RuntimeError(f"图片接口请求失败: {business_error}")

    async def download_image(self, image_url: str) -> tuple[bytes, str | None]:
        """下载上游返回的 URL 图片结果。"""

        clean_image_url = str(image_url or "").strip()
        if not clean_image_url:
            raise ValueError("图片下载地址不能为空")

        session = await self._get_session()
        try:
            async with session.get(
                clean_image_url,
                proxy=self._proxy_url,
            ) as response:
                response.raise_for_status()
                image_bytes = await response.read()
                return image_bytes, self._extract_content_type(response)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"URL 图片下载失败: {exc}") from exc

    async def _post_json(
        self,
        endpoint: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """发送 JSON 请求并返回响应，供不同图片端点复用。"""

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        session = await self._get_session()
        try:
            async with session.post(
                endpoint,
                json=payload,
                headers=headers,
                proxy=self._proxy_url,
            ) as response:
                response.raise_for_status()
                return await response.json()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"图片接口请求失败: {exc}") from exc

    async def _post_multipart(
        self,
        endpoint: str,
        data: dict[str, str],
        files: list[tuple[str, bytes, str]],
    ) -> dict[str, Any]:
        """发送 multipart 表单请求并返回响应。"""

        headers = {
            "Authorization": f"Bearer {self._api_key}",
        }
        form = aiohttp.FormData()
        for field_name, field_value in data.items():
            form.add_field(field_name, field_value)
        for filename, file_bytes, mime_type in files:
            form.add_field(
                "image",
                file_bytes,
                filename=filename,
                content_type=mime_type,
            )

        session = await self._get_session()
        try:
            async with session.post(
                endpoint,
                data=form,
                headers=headers,
                proxy=self._proxy_url,
            ) as response:
                response.raise_for_status()
                return await response.json()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"图片接口请求失败: {exc}") from exc

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取可复用的 HTTP 会话。"""

        if self._session and not self._session.closed:
            return self._session
        if self._owned_session and not self._owned_session.closed:
            return self._owned_session

        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        self._owned_session = aiohttp.ClientSession(timeout=timeout, trust_env=True)
        return self._owned_session

    @staticmethod
    def _extract_content_type(response: Any) -> str | None:
        """从下载响应头中提取 MIME 类型，去掉 charset 等附加参数。"""

        headers = getattr(response, "headers", None)
        if not headers:
            return None

        content_type = str(headers.get("Content-Type", "") or "")
        clean_content_type = content_type.split(";", 1)[0].strip().lower()
        return clean_content_type or None

    @staticmethod
    def _looks_like_success_response(response_data: Any) -> bool:
        """判断响应是否已满足 Responses 图片解析前提。"""

        return isinstance(response_data, dict) and isinstance(
            response_data.get("output"), list
        )

    @staticmethod
    def _looks_like_images_success_response(response_data: Any) -> bool:
        """判断响应是否已满足 Images generations 图片解析前提。"""

        return isinstance(response_data, dict) and isinstance(
            response_data.get("data"), list
        )

    @staticmethod
    def _build_business_error(response_data: Any) -> str:
        """提取可读的业务错误信息。"""

        if isinstance(response_data, dict):
            error_payload = response_data.get("error")
            if isinstance(error_payload, dict):
                message = str(error_payload.get("message", "") or "").strip()
                error_type = str(error_payload.get("type", "") or "").strip()
                if message:
                    return f"{message} (type={error_type or 'unknown'})"

            message = str(response_data.get("message", "") or "").strip()
            if message:
                return message

            return f"unexpected_response_keys={','.join(sorted(map(str, response_data.keys())))}"

        return f"unexpected_response_type={type(response_data).__name__}"
