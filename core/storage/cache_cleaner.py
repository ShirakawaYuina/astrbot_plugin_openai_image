"""缓存目录清理。"""

from __future__ import annotations

from pathlib import Path

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def cleanup_cache(cache_dir: Path, max_cache_images: int) -> list[Path]:
    """清理超出数量上限的旧图片缓存。"""

    normalized_limit = max(0, int(max_cache_images))
    cache_path = Path(cache_dir)
    image_files = sorted(
        [
            path
            for path in cache_path.iterdir()
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
        ],
        key=lambda item: item.stat().st_mtime,
    )
    if len(image_files) <= normalized_limit:
        return []

    removed_files: list[Path] = []
    for path in image_files[: len(image_files) - normalized_limit]:
        path.unlink()
        metadata_path = path.with_name(f"{path.name}.json")
        if metadata_path.is_file():
            metadata_path.unlink()
        removed_files.append(path)
    return removed_files
