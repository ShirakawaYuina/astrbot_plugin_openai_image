from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT.parent
for candidate in (str(PARENT), str(ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)


def _load_module():
    return importlib.import_module(
        "astrbot_plugin_openai_image.core.storage.thumbnail_store"
    )


def _write_png(
    path: Path,
    size: tuple[int, int] = (64, 48),
    color: str = "red",
) -> None:
    Image.new("RGB", size, color).save(path, format="PNG")


def test_thumbnail_store_generates_and_reuses_webp_data_url(tmp_path: Path):
    module = _load_module()
    _write_png(tmp_path / "demo.png", size=(1600, 1200))
    store = module.ThumbnailStore(tmp_path / "thumbs", max_side=64)

    data_url = store.data_url(tmp_path / "demo.png")

    assert data_url.startswith("data:image/webp;base64,")
    thumbnail_path = tmp_path / "thumbs" / "demo.png.webp"
    assert thumbnail_path.is_file()
    with Image.open(thumbnail_path) as thumbnail:
        assert thumbnail.size == (64, 48)

    # 第二次调用必须命中磁盘缓存，不能重复解码原图。
    mtime_before = thumbnail_path.stat().st_mtime_ns
    assert store.data_url(tmp_path / "demo.png").startswith("data:image/webp;base64,")
    assert thumbnail_path.stat().st_mtime_ns == mtime_before


def test_thumbnail_store_regenerates_after_source_change(tmp_path: Path):
    module = _load_module()
    image_path = tmp_path / "demo.png"
    _write_png(image_path, size=(256, 256), color="red")
    store = module.ThumbnailStore(tmp_path / "thumbs", max_side=64)
    store.data_url(image_path)
    thumbnail_path = tmp_path / "thumbs" / "demo.png.webp"
    with Image.open(thumbnail_path) as thumbnail:
        assert thumbnail.size == (64, 64)
        assert thumbnail.convert("RGB").getpixel((0, 0))[0] > 200

    # 手动回拨缩略图时间戳，稳定复现原图更新、缓存过期的情况。
    os.utime(thumbnail_path, (0, 0))
    _write_png(image_path, size=(256, 256), color="blue")
    store.data_url(image_path)

    with Image.open(thumbnail_path) as thumbnail:
        assert thumbnail.convert("RGB").getpixel((0, 0))[2] > 200


def test_thumbnail_store_returns_empty_for_broken_image(tmp_path: Path):
    module = _load_module()
    (tmp_path / "broken.png").write_bytes(b"not-an-image")
    store = module.ThumbnailStore(tmp_path / "thumbs")

    assert store.data_url(tmp_path / "broken.png") == ""


def test_thumbnail_store_discards_and_prunes_orphans(tmp_path: Path):
    module = _load_module()
    _write_png(tmp_path / "keep.png")
    _write_png(tmp_path / "drop.png")
    store = module.ThumbnailStore(tmp_path / "thumbs")
    store.data_url(tmp_path / "keep.png")
    store.data_url(tmp_path / "drop.png")

    store.discard("drop.png")
    assert not (tmp_path / "thumbs" / "drop.png.webp").exists()

    store.prune({"keep.png"})
    assert (tmp_path / "thumbs" / "keep.png.webp").is_file()
    assert not (tmp_path / "thumbs" / "drop.png.webp").exists()


def test_thumbnail_store_prune_removes_interrupted_temp_files(tmp_path: Path):
    module = _load_module()
    _write_png(tmp_path / "keep.png")
    store = module.ThumbnailStore(tmp_path / "thumbs")
    store.data_url(tmp_path / "keep.png")
    temporary_file = tmp_path / "thumbs" / "keep.png.webp.deadbeef.tmp"
    temporary_file.write_bytes(b"partial")

    store.prune({"keep.png"})

    assert (tmp_path / "thumbs" / "keep.png.webp").is_file()
    assert not temporary_file.exists()
