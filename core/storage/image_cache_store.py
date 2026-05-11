"""图片缓存落盘。"""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

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

    def save_image_metadata(self, image_path: Path, metadata: dict[str, Any]) -> None:
        """为图片写入同名元数据文件，记录提示词、尺寸等后台展示信息。"""

        resolved_image_path = Path(image_path)
        metadata_path = resolved_image_path.with_name(
            f"{resolved_image_path.name}.json"
        )
        safe_metadata = {
            "prompt": str(metadata.get("prompt", "") or ""),
            "size": str(metadata.get("size", "") or ""),
            "mode": str(metadata.get("mode", "") or ""),
        }
        metadata_path.write_text(
            json.dumps(safe_metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
