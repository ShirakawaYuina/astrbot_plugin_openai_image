"""本地图片接口网关，带健康检查功能。"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from astrbot.api import logger
from .openai_image_gateway import OpenAIImageGateway

DEFAULT_HEALTH_TIMEOUT_SECONDS = 5


def resolve_health_endpoint(base_url: str) -> str:
    """根据配置的基础地址推导健康检查接口地址。

    Args:
        base_url: 供应商配置的基础地址。

    Returns:
        健康检查接口完整 URL。

    Raises:
        ValueError: 当 base_url 为空时抛出。
    """
    normalized = str(base_url or "").strip().rstrip("/")
    if not normalized:
        raise ValueError("图片接口地址不能为空")

    parsed = urlsplit(normalized)
    base_path = parsed.path.rstrip("/")
    if base_path.endswith("/health"):
        return normalized
    if base_path.endswith("/v1"):
        base_path = base_path[:-3]
    health_path = f"{base_path}/health" if base_path else "/health"
    return urlunsplit((parsed.scheme, parsed.netloc, health_path, "", ""))


class LocalImageGateway(OpenAIImageGateway):
    """面向本地供应商 (provider_id 以 local 开头) 的图片接口网关。

    在接收到生图、编辑请求后，先执行一次健康度检查，当且仅当健康度检查
    接口成功返回 (2xx) 时才继续进行下游请求，否则立刻中断请求。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_seconds: int = 120,
        proxy_url: str = "",
        session: aiohttp.ClientSession | None = None,
        health_timeout_seconds: int = DEFAULT_HEALTH_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            proxy_url=proxy_url,
            session=session,
        )
        self._health_endpoint = resolve_health_endpoint(base_url)
        self._health_timeout_seconds = max(1, int(health_timeout_seconds))

    @property
    def health_endpoint(self) -> str:
        """返回健康检查接口地址。"""
        return self._health_endpoint

    async def check_health(self) -> None:
        """执行本地后端健康度检查。

        当且仅当健康度检查接口成功返回 (2xx) 时才正常退出，
        否则抛出异常立刻中断请求。

        Raises:
            RuntimeError: 当健康检查超时、连接失败或状态码非 2xx 时抛出。
        """
        headers = {"Connection": "close"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        session = await self._get_session()
        timeout = aiohttp.ClientTimeout(total=self._health_timeout_seconds)

        logger.info(
            "[OpenAIImage][local_gateway] 正在对本地后端执行健康度检查 endpoint=%s timeout=%ss",
            self._health_endpoint,
            self._health_timeout_seconds,
        )

        try:
            async with session.get(
                self._health_endpoint,
                headers=headers,
                proxy=self._proxy_url,
                timeout=timeout,
            ) as response:
                if hasattr(response, "read"):
                    body_bytes = await response.read()
                elif hasattr(response, "text"):
                    body_bytes = (await response.text()).encode("utf-8")
                else:
                    body_bytes = b""
                if 200 <= response.status < 300:
                    logger.info(
                        "[OpenAIImage][local_gateway] 本地后端健康度检查通过 status=%s",
                        response.status,
                    )
                    return

                try:
                    body_text = body_bytes[:200].decode("utf-8", errors="replace").strip()
                except Exception:
                    body_text = ""
                detail = f"status={response.status}"
                if body_text:
                    detail += f" body={body_text}"
                raise RuntimeError(f"本地图片服务健康检查未通过: {detail}")
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"本地图片服务健康检查超时（耗时超过 {self._health_timeout_seconds}s），服务不可达或未就绪"
            ) from exc
        except Exception as exc:
            try:
                err_msg = str(exc)
            except Exception:
                err_msg = ""
            raise RuntimeError(
                f"本地图片服务不可用或连接失败: {err_msg or type(exc).__name__}"
            ) from exc

    async def request_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        """向 Responses 接口发送请求前先执行健康检查。"""
        await self.check_health()
        return await super().request_response(payload)

    async def request_image_generation(self, payload: dict[str, Any]) -> dict[str, Any]:
        """向 Images generations 接口发送请求前先执行健康检查。"""
        await self.check_health()
        return await super().request_image_generation(payload)

    async def request_image_edit(
        self,
        data: dict[str, str],
        files: list[tuple[str, bytes, str]],
    ) -> dict[str, Any]:
        """向 Images edits 接口发送请求前先执行健康检查。"""
        await self.check_health()
        return await super().request_image_edit(data, files)
