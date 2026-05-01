"""图片缓存落盘。"""

from __future__ import annotations

import secrets
from datetime import datetime
from pathlib import Path

from .cache_cleaner import cleanup_cache


class ImageCacheStore:
    """负责将图片数据写入缓存目录。"""

    def __init__(self, cache_dir: Path, max_cache_images: int) -> None:
        self.cache_dir = Path(cache_dir)
        self.max_cache_images = max(1, int(max_cache_images))
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def save_image(self, image_bytes: bytes, extension: str) -> Path:
        """将图片写入缓存目录并执行缓存清理。"""

        suffix = str(extension or "").strip() or ".png"
        file_name = (
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(4)}{suffix}"
        )
        file_path = self.cache_dir / file_name
        file_path.write_bytes(image_bytes)
        cleanup_cache(self.cache_dir, self.max_cache_images)
        return file_path
