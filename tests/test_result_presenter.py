from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api.message_components import Image


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_module():
    return importlib.import_module("core.presenters.result_presenter")


def _make_event(
    platform_name: str = "aiocqhttp",
    *,
    group_id: str = "123456",
    sender_id: str = "654321",
):
    bot = SimpleNamespace(send_group_msg=AsyncMock(), send_private_msg=AsyncMock())
    return SimpleNamespace(
        bot=bot,
        get_platform_name=lambda: platform_name,
        get_group_id=lambda: group_id,
        get_sender_id=lambda: sender_id,
        send=AsyncMock(),
        plain_result=lambda text: text,
        chain_result=lambda chain: chain,
    )


@pytest.mark.asyncio
async def test_present_image_result_rejects_non_onebot_platform(tmp_path: Path):
    module = _load_module()
    event = _make_event(platform_name="telegram")
    presenter = module.ResultPresenter()

    image_path = tmp_path / "demo.png"
    image_path.write_bytes(b"abc")

    with pytest.raises(RuntimeError, match="仅支持通过 OneBot v11 返回 QQ 图片消息"):
        await presenter.send_images(event, [image_path])


@pytest.mark.asyncio
async def test_present_image_result_sends_filesystem_images_on_onebot(tmp_path: Path):
    module = _load_module()
    event = _make_event(platform_name="aiocqhttp")
    presenter = module.ResultPresenter()

    image_path = tmp_path / "demo.png"
    image_path.write_bytes(b"abc")

    await presenter.send_images(event, [image_path])

    event.send.assert_awaited_once()
    sent_chain = event.send.await_args.args[0]
    assert len(sent_chain) == 1
    assert isinstance(sent_chain[0], Image)
    assert getattr(sent_chain[0], "path", "") == str(image_path)


@pytest.mark.asyncio
async def test_present_image_result_sends_registered_url_to_onebot_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    module = _load_module()
    event = _make_event(platform_name="aiocqhttp")
    presenter = module.ResultPresenter(
        send_mode="url",
        url_base="http://astrbot:6185/",
    )
    token_service = SimpleNamespace(register_file=AsyncMock(return_value="demo-token"))
    monkeypatch.setattr(module, "file_token_service", token_service, raising=False)

    image_path = tmp_path / "demo.png"
    image_path.write_bytes(b"abc")

    await presenter.send_images(event, [image_path])

    token_service.register_file.assert_awaited_once_with(str(image_path))
    event.send.assert_not_awaited()
    event.bot.send_group_msg.assert_awaited_once_with(
        group_id=123456,
        message=[
            {
                "type": "image",
                "data": {
                    "file": "http://astrbot:6185/api/file/demo-token",
                },
            }
        ],
    )


@pytest.mark.asyncio
async def test_present_image_result_sends_registered_url_to_onebot_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    module = _load_module()
    event = _make_event(platform_name="aiocqhttp", group_id="", sender_id="654321")
    presenter = module.ResultPresenter(
        send_mode="url",
        url_base="http://astrbot:6185",
    )
    token_service = SimpleNamespace(register_file=AsyncMock(return_value="demo-token"))
    monkeypatch.setattr(module, "file_token_service", token_service, raising=False)

    image_path = tmp_path / "demo.png"
    image_path.write_bytes(b"abc")

    await presenter.send_images(event, [image_path])

    event.send.assert_not_awaited()
    event.bot.send_group_msg.assert_not_awaited()
    event.bot.send_private_msg.assert_awaited_once_with(
        user_id=654321,
        message=[
            {
                "type": "image",
                "data": {
                    "file": "http://astrbot:6185/api/file/demo-token",
                },
            }
        ],
    )


@pytest.mark.asyncio
async def test_present_image_result_url_mode_requires_reachable_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    module = _load_module()
    event = _make_event(platform_name="aiocqhttp")
    presenter = module.ResultPresenter(send_mode="url")
    monkeypatch.setattr(
        module,
        "astrbot_config",
        SimpleNamespace(get=lambda *_: ""),
        raising=False,
    )

    image_path = tmp_path / "demo.png"
    image_path.write_bytes(b"abc")

    with pytest.raises(RuntimeError, match="image_send_url_base"):
        await presenter.send_images(event, [image_path])
