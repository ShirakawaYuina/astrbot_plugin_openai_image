from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_module():
    return importlib.import_module("core.storage.cache_cleaner")


def test_cleanup_removes_oldest_image_files_only(tmp_path: Path):
    module = _load_module()

    oldest = tmp_path / "old.png"
    middle = tmp_path / "middle.jpg"
    newest = tmp_path / "new.webp"
    keep_text = tmp_path / "note.txt"

    oldest.write_bytes(b"1")
    time.sleep(0.02)
    middle.write_bytes(b"2")
    time.sleep(0.02)
    newest.write_bytes(b"3")
    keep_text.write_text("keep", encoding="utf-8")

    removed = module.cleanup_cache(cache_dir=tmp_path, max_cache_images=2)

    assert removed == [oldest]
    assert not oldest.exists()
    assert middle.exists()
    assert newest.exists()
    assert keep_text.exists()
