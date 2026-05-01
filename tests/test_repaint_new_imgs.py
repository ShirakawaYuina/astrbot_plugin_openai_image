from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "new_imgs" / "repaint_images.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("repaint_images", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_iter_source_images_only_reads_top_level_supported_images(tmp_path: Path):
    module = _load_module()
    (tmp_path / "first.png").write_bytes(b"first")
    (tmp_path / "second.webp").write_bytes(b"second")
    (tmp_path / "note.txt").write_text("ignore", encoding="utf-8")
    repaint_dir = tmp_path / "repaint"
    repaint_dir.mkdir()
    (repaint_dir / "nested.png").write_bytes(b"ignore")

    image_paths = module.iter_source_images(tmp_path)

    assert [path.name for path in image_paths] == ["first.png", "second.webp"]


def test_build_edit_form_data_contains_required_prompt_and_size():
    module = _load_module()

    form_data = module.build_edit_form_data(model="gpt-image-2")

    assert form_data == {
        "model": "gpt-image-2",
        "prompt": "高清重绘，去掉底部空白，增加背景信息，图像中不要出现文字",
        "size": "1024x1024",
        "response_format": "b64_json",
    }


def test_parse_first_image_bytes_decodes_b64_json():
    module = _load_module()

    image_bytes = module.parse_first_image_bytes({"data": [{"b64_json": "aGVsbG8="}]})

    assert image_bytes == b"hello"


@pytest.mark.asyncio
async def test_run_repaint_jobs_honors_concurrency_limit(tmp_path: Path):
    module = _load_module()
    source_paths = []
    for index in range(5):
        source_path = tmp_path / f"image_{index}.png"
        source_path.write_bytes(b"source")
        source_paths.append(source_path)

    active_count = 0
    max_active_count = 0
    started_names: list[str] = []

    async def fake_repaint_one_image(*args, **kwargs):
        nonlocal active_count, max_active_count

        active_count += 1
        max_active_count = max(max_active_count, active_count)
        started_names.append(kwargs["source_path"].name)
        await asyncio.sleep(0.01)
        kwargs["output_path"].write_bytes(b"result")
        active_count -= 1

    module.repaint_one_image = fake_repaint_one_image

    await module.run_repaint_jobs(
        session=object(),
        config={
            "endpoint": "https://cdn.jucode.top/v1/images/edits",
            "api_key": "demo-key",
            "model": "gpt-image-2",
        },
        source_paths=source_paths,
        output_dir=tmp_path / "repaint",
        overwrite=False,
        concurrency=2,
    )

    assert max_active_count == 2
    assert sorted(started_names) == [path.name for path in source_paths]


@pytest.mark.asyncio
async def test_run_repaint_jobs_continues_when_one_image_fails(tmp_path: Path):
    module = _load_module()
    source_paths = []
    for name in ["ok_1.png", "fail.png", "ok_2.png"]:
        source_path = tmp_path / name
        source_path.write_bytes(b"source")
        source_paths.append(source_path)

    async def fake_repaint_one_image(*args, **kwargs):
        if kwargs["source_path"].name == "fail.png":
            raise TimeoutError("request timeout")
        kwargs["output_path"].write_bytes(b"result")

    module.repaint_one_image = fake_repaint_one_image

    failed_paths = await module.run_repaint_jobs(
        session=object(),
        config={
            "endpoint": "https://cdn.jucode.top/v1/images/edits",
            "api_key": "demo-key",
            "model": "gpt-image-2",
        },
        source_paths=source_paths,
        output_dir=tmp_path / "repaint",
        overwrite=False,
        concurrency=2,
    )

    assert failed_paths == [tmp_path / "fail.png"]
    assert (tmp_path / "repaint" / "ok_1.png").read_bytes() == b"result"
    assert (tmp_path / "repaint" / "ok_2.png").read_bytes() == b"result"
