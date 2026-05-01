from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_module():
    return importlib.import_module("core.storage.image_cache_store")


def test_save_image_writes_file_with_expected_extension(tmp_path: Path):
    module = _load_module()

    store = module.ImageCacheStore(cache_dir=tmp_path, max_cache_images=10)
    saved_path = store.save_image(image_bytes=b"abc", extension=".png")

    assert saved_path.exists()
    assert saved_path.suffix == ".png"
    assert saved_path.read_bytes() == b"abc"
