from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot.core.message.components import ComponentType


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_module():
    return importlib.import_module("core.utils.image_extract")


def _image_component(path: str, mime_type: str = "image/png"):
    return SimpleNamespace(path=path, type="image", mime_type=mime_type)


def _reply_component(images):
    return SimpleNamespace(type="reply", chain=images)


def test_extract_prefers_reply_image_over_current_message():
    module = _load_module()

    event = SimpleNamespace(
        message_obj=SimpleNamespace(
            message=[
                _reply_component([_image_component("reply.png")]),
                _image_component("current.png"),
            ]
        )
    )

    extracted = module.extract_first_image_component(event)

    assert getattr(extracted, "path", "") == "reply.png"


def test_extract_uses_current_message_image_when_reply_has_none():
    module = _load_module()

    event = SimpleNamespace(
        message_obj=SimpleNamespace(
            message=[
                _reply_component([]),
                _image_component("current.png"),
            ]
        )
    )

    extracted = module.extract_first_image_component(event)

    assert getattr(extracted, "path", "") == "current.png"


def test_extract_image_components_returns_all_reply_images_before_current_message():
    module = _load_module()

    event = SimpleNamespace(
        message_obj=SimpleNamespace(
            message=[
                _reply_component(
                    [
                        _image_component("reply-1.png"),
                        _image_component("reply-2.png"),
                    ]
                ),
                _image_component("current.png"),
            ]
        )
    )

    extracted = module.extract_image_components(event)

    assert [getattr(component, "path", "") for component in extracted] == [
        "reply-1.png",
        "reply-2.png",
    ]


def test_extract_image_components_returns_all_current_images_when_reply_has_none():
    module = _load_module()

    event = SimpleNamespace(
        message_obj=SimpleNamespace(
            message=[
                _reply_component([]),
                _image_component("current-1.png"),
                _image_component("current-2.png"),
            ]
        )
    )

    extracted = module.extract_image_components(event)

    assert [getattr(component, "path", "") for component in extracted] == [
        "current-1.png",
        "current-2.png",
    ]


def test_extract_returns_none_when_no_image_exists():
    module = _load_module()

    event = SimpleNamespace(
        message_obj=SimpleNamespace(
            message=[],
        )
    )

    assert module.extract_first_image_component(event) is None


def test_extract_supports_astrbot_component_type_enum_in_current_message():
    module = _load_module()

    event = SimpleNamespace(
        message_obj=SimpleNamespace(
            message=[
                SimpleNamespace(
                    path="current-enum.png",
                    type=ComponentType.Image,
                    mime_type="image/png",
                )
            ]
        )
    )

    extracted = module.extract_first_image_component(event)

    assert getattr(extracted, "path", "") == "current-enum.png"


def test_extract_supports_astrbot_component_type_enum_in_reply_chain():
    module = _load_module()

    event = SimpleNamespace(
        message_obj=SimpleNamespace(
            message=[
                SimpleNamespace(
                    type=ComponentType.Reply,
                    chain=[
                        SimpleNamespace(
                            path="reply-enum.png",
                            type=ComponentType.Image,
                            mime_type="image/png",
                        )
                    ],
                )
            ]
        )
    )

    extracted = module.extract_first_image_component(event)

    assert getattr(extracted, "path", "") == "reply-enum.png"


def test_extract_first_at_target_prefers_first_mention():
    module = _load_module()

    event = SimpleNamespace(
        message_obj=SimpleNamespace(
            message=[
                SimpleNamespace(type="at", qq="123456"),
                SimpleNamespace(type="at", qq="654321"),
            ]
        )
    )

    extracted = module.extract_first_at_target(event)

    assert extracted == "123456"


def test_extract_first_at_target_returns_none_when_missing():
    module = _load_module()

    event = SimpleNamespace(
        message_obj=SimpleNamespace(
            message=[
                SimpleNamespace(type="text", text="hello"),
            ]
        )
    )

    assert module.extract_first_at_target(event) is None
