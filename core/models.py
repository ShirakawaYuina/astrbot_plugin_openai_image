"""核心数据模型定义。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ParsedCommand:
    """命令解析后的结构化结果。"""

    count: int
    prompt: str
    size: str | None = None
    quality: str = "auto"
    moderation: str = "low"


@dataclass(slots=True)
class ParsedImageResponse:
    """图片接口响应解析结果。

    既兼容上游直接返回 base64 的情况，也兼容先返回远程图片 URL、
    再由插件主动下载落盘的情况。
    """

    mime_type: str
    extension: str
    image_bytes: bytes = b""
    image_url: str = ""
