"""MIME 类型与文件扩展名工具。"""

from __future__ import annotations


def extension_from_mime_type(mime_type: str) -> str:
    """根据图片 MIME 类型返回文件扩展名。"""

    normalized = str(mime_type or "").strip().lower()
    mapping = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/webp": ".webp",
    }
    if normalized not in mapping:
        raise ValueError(f"不支持的图片 MIME 类型: {mime_type}")
    return mapping[normalized]
