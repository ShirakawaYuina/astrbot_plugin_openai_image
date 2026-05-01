"""将图片结果回传到 AstrBot 事件。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from astrbot.api.message_components import Image
from astrbot.core import astrbot_config, file_token_service

SEND_MODE_BASE64 = "base64"
SEND_MODE_URL = "url"


class ResultPresenter:
    """负责将本地图片通过 OneBot v11 事件回传到 QQ。"""

    def __init__(self, send_mode: str = SEND_MODE_BASE64, url_base: str = "") -> None:
        self.send_mode = self._normalize_send_mode(send_mode)
        self.url_base = str(url_base or "").strip()

    async def send_images(self, event: Any, image_paths: list[Path]) -> None:
        """发送图片消息，当前仅支持 OneBot v11。"""

        platform_name = str(event.get_platform_name() or "").strip()
        if platform_name != "aiocqhttp":
            raise RuntimeError("仅支持通过 OneBot v11 返回 QQ 图片消息")

        if self.send_mode == SEND_MODE_URL:
            await self._send_images_by_url(event, image_paths)
            return

        image_chain = [
            Image.fromFileSystem(str(Path(image_path))) for image_path in image_paths
        ]
        await event.send(event.chain_result(image_chain))

    async def _send_images_by_url(self, event: Any, image_paths: list[Path]) -> None:
        """注册本地图片 URL 后直接调用 OneBot，绕过 AstrBot 的 base64 转换。"""

        image_messages = [
            {
                "type": "image",
                "data": {
                    "file": await self._register_image_url(Path(image_path)),
                },
            }
            for image_path in image_paths
        ]
        if not image_messages:
            return

        bot = getattr(event, "bot", None)
        if bot is None:
            raise RuntimeError("URL 发送模式需要 aiocqhttp 事件暴露 bot 实例")

        group_id = str(event.get_group_id() or "").strip()
        if group_id.isdigit():
            await bot.send_group_msg(group_id=int(group_id), message=image_messages)
            return

        sender_id = str(event.get_sender_id() or "").strip()
        if sender_id.isdigit():
            await bot.send_private_msg(user_id=int(sender_id), message=image_messages)
            return

        raise RuntimeError("URL 发送模式缺少有效的群号或用户 ID")

    async def _register_image_url(self, image_path: Path) -> str:
        """将图片注册到 AstrBot 文件服务，返回 NapCat 可访问的下载 URL。"""

        callback_base = self._resolve_url_base()
        token = await file_token_service.register_file(str(image_path))
        return f"{callback_base}/api/file/{token}"

    def _resolve_url_base(self) -> str:
        """读取插件专用 URL 前缀，未配置时回退到 AstrBot 全局回调地址。"""

        configured_base = (
            self.url_base
            or str(astrbot_config.get("callback_api_base", "") or "").strip()
        )
        clean_base = configured_base.rstrip("/")
        if not clean_base:
            raise RuntimeError(
                "URL 发送模式需要配置 image_send_url_base 或 AstrBot callback_api_base"
            )
        return clean_base

    @staticmethod
    def _normalize_send_mode(send_mode: str) -> str:
        """规范化发送模式，未知配置回退到默认 base64 以保持兼容。"""

        clean_mode = str(send_mode or "").strip().lower()
        if clean_mode == SEND_MODE_URL:
            return SEND_MODE_URL
        return SEND_MODE_BASE64
