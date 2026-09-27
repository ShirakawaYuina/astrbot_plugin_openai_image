"""WebUI 图库缩略图生成与缓存。"""

from __future__ import annotations

import base64
import io
import secrets
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from astrbot.api import logger

THUMBNAIL_MAX_SIDE = 768
THUMBNAIL_QUALITY = 80
THUMBNAIL_MIME_TYPE = "image/webp"


class ThumbnailStore:
    """按需把缓存原图缩成 webp 缩略图并落盘复用。

    内置页面运行在受限 iframe 中，无法直接用 <img src> 加载鉴权图片，
    只能由后端内联 base64 数据，因此缩略图必须在服务端生成，
    否则几十张 2K 原图会让首屏 JSON 膨胀到上百 MB。
    """

    def __init__(
        self,
        thumbnail_dir: Path,
        *,
        max_side: int = THUMBNAIL_MAX_SIDE,
        quality: int = THUMBNAIL_QUALITY,
    ) -> None:
        self.thumbnail_dir = Path(thumbnail_dir)
        self.thumbnail_dir.mkdir(parents=True, exist_ok=True)
        self.max_side = max(64, int(max_side))
        self.quality = max(1, min(95, int(quality)))

    def data_url(self, image_path: Path) -> str:
        """返回缩略图 data URL，原图无法解码时返回空字符串。

        Args:
            image_path: 缓存目录下的原图路径。

        Returns:
            可直接赋值给 img.src 的 webp data URL；生成失败时返回空字符串。
        """
        thumbnail_path = self.thumbnail_dir / f"{Path(image_path).name}.webp"
        try:
            if not self._is_cache_fresh(thumbnail_path, image_path):
                self._write_thumbnail(image_path, thumbnail_path)
            return self._build_data_url(thumbnail_path)
        except (OSError, UnidentifiedImageError, ValueError) as exc:
            # 单张图片损坏不应该让整个图库接口 500，这里记录后跳过该缩略图。
            logger.warning(
                "[OpenAIImage][webui] 缩略图生成失败 name=%s error=%s",
                Path(image_path).name,
                exc,
            )
            return ""

    def discard(self, image_name: str) -> None:
        """删除指定原图对应的缩略图缓存。"""
        thumbnail_path = self.thumbnail_dir / f"{Path(image_name).name}.webp"
        if thumbnail_path.is_file():
            thumbnail_path.unlink(missing_ok=True)

    def prune(self, valid_names: set[str]) -> None:
        """清理原图已不存在但缩略图残留的文件，以及中断写入留下的临时文件。

        Args:
            valid_names: 当前缓存目录中仍然存在的原图文件名集合。
        """
        for thumbnail_path in self.thumbnail_dir.iterdir():
            if not thumbnail_path.is_file():
                continue
            if thumbnail_path.name.endswith(".webp") and (
                thumbnail_path.name.removesuffix(".webp") in valid_names
            ):
                continue
            try:
                thumbnail_path.unlink(missing_ok=True)
            except OSError:
                # 临时文件可能正被并发请求写入，删不掉就留给下一次列表请求处理。
                continue

    def _is_cache_fresh(self, thumbnail_path: Path, image_path: Path) -> bool:
        """判断缩略图是否比原图新，避免原图更新后继续展示旧缩略图。"""
        if not thumbnail_path.is_file():
            return False
        return thumbnail_path.stat().st_mtime >= image_path.stat().st_mtime

    def _write_thumbnail(self, image_path: Path, thumbnail_path: Path) -> None:
        """用 Pillow 把原图缩放到最长边上限后写成 webp。"""
        with Image.open(image_path) as source_image:
            thumbnail_image = source_image.convert("RGB")
            thumbnail_image.thumbnail(
                (self.max_side, self.max_side), Image.Resampling.LANCZOS
            )
            buffer = io.BytesIO()
            thumbnail_image.save(buffer, format="WEBP", quality=self.quality)

        # 临时文件名带随机后缀，避免并发请求写同一个中间文件导致读到半截内容。
        temporary_path = thumbnail_path.with_name(
            f"{thumbnail_path.name}.{secrets.token_hex(4)}.tmp"
        )
        temporary_path.write_bytes(buffer.getvalue())
        temporary_path.replace(thumbnail_path)

    @staticmethod
    def _build_data_url(thumbnail_path: Path) -> str:
        """把缩略图文件编码为 webp data URL。"""
        encoded = base64.b64encode(thumbnail_path.read_bytes()).decode("ascii")
        return f"data:{THUMBNAIL_MIME_TYPE};base64,{encoded}"
