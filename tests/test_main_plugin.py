from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT.parent
for candidate in (str(PARENT), str(ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)


def _load_module():
    return importlib.import_module("astrbot_plugin_openai_image.main")


async def _collect_asyncgen(asyncgen):
    results = []
    async for item in asyncgen:
        results.append(item)
    return results


def test_rebuild_runtime_dependencies_passes_image_send_config_to_presenter():
    module = _load_module()
    plugin = module.OpenAIImagePlugin(
        context=SimpleNamespace(),
        config={
            "base_url": "https://example.com/v1",
            "image_send_mode": "url",
            "image_send_url_base": "http://astrbot:6185",
        },
    )

    plugin._rebuild_runtime_dependencies()

    assert plugin.presenter.send_mode == "url"
    assert plugin.presenter.url_base == "http://astrbot:6185"


def test_rebuild_runtime_dependencies_uses_dropdown_selected_provider():
    module = _load_module()
    plugin = module.OpenAIImagePlugin(
        context=SimpleNamespace(),
        config={
            "active_provider_id": "primary",
            "image_providers": [
                {
                    "__template_key": "openai_compatible",
                    "provider_id": "backup",
                    "name": "备用供应商",
                    "base_url": "https://backup.example.com/v1",
                    "api_key": "backup-key",
                },
                {
                    "__template_key": "openai_compatible",
                    "provider_id": "primary",
                    "name": "主供应商",
                    "base_url": "https://primary.example.com/v1",
                    "api_key": "primary-key",
                },
            ],
        },
    )

    plugin._rebuild_runtime_dependencies()

    active_provider = plugin._get_active_image_provider()
    assert active_provider.name == "主供应商"
    assert active_provider.base_url == "https://primary.example.com/v1"
    assert active_provider.api_key == "primary-key"


def test_rebuild_runtime_dependencies_prepares_web_admin_server():
    module = _load_module()
    plugin = module.OpenAIImagePlugin(
        context=SimpleNamespace(),
        config={
            "image_providers": [
                {
                    "__template_key": "openai_compatible",
                    "provider_id": "default",
                    "name": "默认供应商",
                    "base_url": "https://example.com/v1",
                    "api_key": "demo-key",
                }
            ],
            "web_admin_enabled": True,
            "web_admin_port": 7001,
            "web_admin_password": "secret",
        },
    )

    plugin._rebuild_runtime_dependencies()

    assert plugin._web_admin_server is not None
    assert plugin._web_admin_server.settings.enabled is True
    assert plugin._web_admin_server.settings.port == 7001


def _make_event(
    message_text: str = "",
    message_components=None,
    *,
    is_admin: bool = False,
    platform_name: str = "aiocqhttp",
):
    sent_messages: list[str] = []

    async def _send(message):
        sent_messages.append(str(message))

    event = SimpleNamespace(
        message_str=message_text,
        message_obj=SimpleNamespace(message=message_components or []),
        plain_result=lambda text: text,
        chain_result=lambda chain: chain,
        send=AsyncMock(side_effect=_send),
        should_call_llm=lambda _: None,
        stop_event=lambda: None,
        get_platform_name=lambda: platform_name,
        is_admin=lambda: is_admin,
    )
    event._sent_messages = sent_messages
    return event


@pytest.mark.asyncio
async def test_generate_command_returns_error_when_prompt_empty():
    module = _load_module()
    plugin = module.OpenAIImagePlugin(context=SimpleNamespace(), config={})
    plugin._rebuild_runtime_dependencies = lambda: None
    plugin._image_gateway = object()
    plugin._generate_service = object()

    event = _make_event()
    await plugin._handle_generate_command(event, raw_prompt="")

    event.send.assert_awaited_once()
    assert "参数错误" in event._sent_messages[0]


@pytest.mark.asyncio
async def test_generate_command_recovers_prompt_from_full_message_when_filter_passes_count_only():
    module = _load_module()
    plugin = module.OpenAIImagePlugin(context=SimpleNamespace(), config={})
    plugin._handle_generate_command = AsyncMock()
    event = _make_event(message_text="/oaiimg 4 赤子,信徒。生成以上人格的人格形象图片")

    await plugin.generate_image_command(event, prompt="4")

    plugin._handle_generate_command.assert_awaited_once_with(
        event,
        raw_prompt="4 赤子,信徒。生成以上人格的人格形象图片",
    )


@pytest.mark.asyncio
async def test_generate_command_keeps_parameters_in_prompt_for_non_bot_admin():
    module = _load_module()
    plugin = module.OpenAIImagePlugin(context=SimpleNamespace(), config={})
    plugin._execute_generate_flow = AsyncMock(
        return_value={
            "status": "success",
            "summary": "已生成 1 张图片",
        }
    )
    event = _make_event(is_admin=False)

    await plugin._handle_generate_command(
        event,
        raw_prompt="2 --size portrait -q high -m auto 生成一只猫",
    )

    event.send.assert_awaited_once()
    assert "收到请求" in event._sent_messages[0]
    plugin._execute_generate_flow.assert_awaited_once()
    call_kwargs = plugin._execute_generate_flow.await_args.kwargs
    assert call_kwargs["count"] == 1
    assert call_kwargs["size"] is None
    assert call_kwargs["quality"] == "auto"
    assert call_kwargs["moderation"] == "low"
    assert call_kwargs["prompt"] == "2 --size portrait -q high -m auto 生成一只猫"


@pytest.mark.asyncio
async def test_generate_command_allows_multi_image_for_bot_admin():
    module = _load_module()
    plugin = module.OpenAIImagePlugin(context=SimpleNamespace(), config={})
    plugin._execute_generate_flow = AsyncMock(
        return_value={
            "status": "success",
            "summary": "已生成 2 张图片",
        }
    )
    event = _make_event(is_admin=True)

    await plugin._handle_generate_command(event, raw_prompt="2 生成两只猫")

    event.send.assert_awaited_once()
    assert "收到请求" in event._sent_messages[0]
    plugin._execute_generate_flow.assert_awaited_once()
    assert plugin._execute_generate_flow.await_args.kwargs["count"] == 2


@pytest.mark.asyncio
async def test_generate_command_allows_multi_image_when_admin_only_config_disabled():
    module = _load_module()
    plugin = module.OpenAIImagePlugin(
        context=SimpleNamespace(),
        config={"multi_image_command_admin_only": False},
    )
    plugin._execute_generate_flow = AsyncMock(
        return_value={
            "status": "success",
            "summary": "已生成 2 张图片",
        }
    )
    event = _make_event(is_admin=False)

    await plugin._handle_generate_command(event, raw_prompt="2 生成两只猫")

    event.send.assert_awaited_once()
    assert "收到请求" in event._sent_messages[0]
    plugin._execute_generate_flow.assert_awaited_once()
    assert plugin._execute_generate_flow.await_args.kwargs["count"] == 2


@pytest.mark.asyncio
async def test_edit_command_keeps_parameters_in_prompt_for_non_bot_admin():
    module = _load_module()
    plugin = module.OpenAIImagePlugin(context=SimpleNamespace(), config={})
    plugin._execute_edit_flow = AsyncMock(
        return_value={
            "status": "success",
            "summary": "已编辑 1 张图片",
        }
    )
    event = _make_event(
        message_components=[SimpleNamespace(type="image", file="demo.png")],
        is_admin=False,
    )

    results = await _collect_asyncgen(
        plugin._handle_edit_command(
            event,
            raw_prompt="2 --size square -q medium -m auto 改成统一心理学海报",
        )
    )

    assert len(results) == 1
    assert results[0].chain[0].text == '收到请求，prompt="2 --size square -q medium -m auto 改成统一心理学海报"，正在生成中...'
    plugin._execute_edit_flow.assert_awaited_once()
    call_kwargs = plugin._execute_edit_flow.await_args.kwargs
    assert call_kwargs["count"] == 1
    assert call_kwargs["size"] is None
    assert call_kwargs["quality"] == "auto"
    assert call_kwargs["moderation"] == "low"
    assert call_kwargs["prompt"] == "2 --size square -q medium -m auto 改成统一心理学海报"


@pytest.mark.asyncio
async def test_generate_command_sends_pending_message_before_execute_flow():
    module = _load_module()
    plugin = module.OpenAIImagePlugin(context=SimpleNamespace(), config={})
    plugin._execute_generate_flow = AsyncMock(
        return_value={
            "status": "success",
            "summary": "已生成 1 张图片",
        }
    )
    event = _make_event()

    await plugin._handle_generate_command(event, raw_prompt="生成一只猫")

    event.send.assert_awaited_once()
    assert event._sent_messages[0] == '收到请求，prompt="生成一只猫"，正在生成中...'
    plugin._execute_generate_flow.assert_awaited_once()


@pytest.mark.asyncio
async def test_edit_command_recovers_prompt_from_full_message_when_filter_passes_count_only():
    module = _load_module()
    plugin = module.OpenAIImagePlugin(context=SimpleNamespace(), config={})
    plugin._recorded_edit_raw_prompt = ""

    async def _record_empty_asyncgen(event, raw_prompt):
        event
        plugin._recorded_edit_raw_prompt = raw_prompt
        if False:
            yield None

    plugin._handle_edit_command = _record_empty_asyncgen
    event = _make_event(message_text="/oaiedit 2 改成统一心理学海报")

    await _collect_asyncgen(plugin.edit_image_command(event, prompt="2"))

    assert plugin._recorded_edit_raw_prompt == "2 改成统一心理学海报"


@pytest.mark.asyncio
async def test_edit_command_requires_image():
    module = _load_module()
    plugin = module.OpenAIImagePlugin(context=SimpleNamespace(), config={})
    plugin._rebuild_runtime_dependencies = lambda: None
    plugin._image_gateway = object()
    plugin._generate_service = object()

    event = _make_event()
    await _collect_asyncgen(plugin._handle_edit_command(event, raw_prompt="改成真人版"))

    event.send.assert_awaited_once()
    assert "未检测到图片" in event._sent_messages[0]


@pytest.mark.asyncio
async def test_edit_command_sends_pending_message_before_execute_flow():
    module = _load_module()
    plugin = module.OpenAIImagePlugin(context=SimpleNamespace(), config={})
    plugin._send_edit_pending_message = AsyncMock()
    plugin._execute_edit_flow = AsyncMock(
        return_value={
            "status": "success",
            "summary": "已编辑 1 张图片",
        }
    )

    event = _make_event(
        message_components=[SimpleNamespace(type="image", file="demo.png")]
    )

    results = await _collect_asyncgen(
        plugin._handle_edit_command(event, raw_prompt="真人化图像")
    )

    assert len(results) == 1
    assert results[0].chain[0].text == '收到请求，prompt="真人化图像"，正在生成中...'
    plugin._execute_edit_flow.assert_awaited_once()


def test_plugin_no_longer_exposes_prompt_enhancement_helper():
    module = _load_module()

    assert not hasattr(module.OpenAIImagePlugin, "_maybe_enhance_prompt")


@pytest.mark.asyncio
async def test_generate_llm_tool_returns_summary_text():
    module = _load_module()
    plugin = module.OpenAIImagePlugin(context=SimpleNamespace(), config={})
    event = _make_event()
    plugin._execute_generate_flow = AsyncMock(
        return_value={
            "status": "success",
            "summary": "已生成 1 张图片",
        }
    )

    result = await plugin.openai_generate_image_tool(
        event,
        prompt="生成一只小猫",
        count=1,
    )

    plugin._execute_generate_flow.assert_awaited_once()
    assert "已生成 1 张图片" == result


@pytest.mark.asyncio
async def test_edit_llm_tool_returns_error_when_no_image_found():
    module = _load_module()
    plugin = module.OpenAIImagePlugin(context=SimpleNamespace(), config={})
    event = _make_event()

    result = await plugin.openai_edit_image_tool(
        event,
        prompt="改成真人版",
        count=1,
    )

    assert "未检测到图片" in result


@pytest.mark.asyncio
async def test_figure_command_saves_reference_image_to_plugin_data(tmp_path: Path):
    module = _load_module()
    plugin = module.OpenAIImagePlugin(context=SimpleNamespace(), config={})
    module.get_astrbot_plugin_data_path = lambda: str(tmp_path)
    image_component = SimpleNamespace(
        type="image",
        convert_to_base64=AsyncMock(return_value="ZmFrZS1maWd1cmU="),
    )
    event = _make_event(message_components=[image_component])

    await plugin._handle_figure_command(event)

    figure_path = (
        tmp_path
        / module.PLUGIN_NAME
        / module.FIGURE_IMAGE_DIR_NAME
        / module.FIGURE_IMAGE_FILE_NAME
    )
    assert figure_path.read_bytes() == b"fake-figure"
    event.send.assert_awaited_once()
    assert "机器人形象图已更新" in event._sent_messages[0]


@pytest.mark.asyncio
async def test_figure_command_requires_image():
    module = _load_module()
    plugin = module.OpenAIImagePlugin(context=SimpleNamespace(), config={})
    event = _make_event()

    await plugin._handle_figure_command(event)

    event.send.assert_awaited_once()
    assert "/oaifigure" in event._sent_messages[0]


@pytest.mark.asyncio
async def test_robot_figure_llm_tool_requires_saved_figure(tmp_path: Path):
    module = _load_module()
    plugin = module.OpenAIImagePlugin(context=SimpleNamespace(), config={})
    module.get_astrbot_plugin_data_path = lambda: str(tmp_path)
    event = _make_event()

    result = await plugin.openai_edit_robot_figure_image_tool(
        event,
        prompt="画一张机器人头像",
        count=1,
    )

    assert "尚未设置机器人形象图" in result


@pytest.mark.asyncio
async def test_execute_figure_edit_flow_uses_saved_figure_image(tmp_path: Path):
    module = _load_module()
    plugin = module.OpenAIImagePlugin(context=SimpleNamespace(), config={})
    module.get_astrbot_plugin_data_path = lambda: str(tmp_path)
    figure_path = plugin._get_figure_image_path()
    figure_path.parent.mkdir(parents=True, exist_ok=True)
    figure_path.write_bytes(b"fake-figure")
    plugin._image_gateway = object()
    plugin._generate_service = object()
    plugin._edit_service = object()
    plugin._task_service = object()
    plugin._run_edit_jobs = AsyncMock(return_value=[])
    plugin._finalize_results = AsyncMock(
        return_value={
            "status": "success",
            "summary": "已编辑 0 张图片",
        }
    )
    event = _make_event()

    result = await plugin._execute_figure_edit_flow(
        event=event,
        prompt="生成机器人立绘",
        count=1,
        size="1024x1024",
        send_user_message=False,
    )

    assert result["summary"] == "已编辑 0 张图片"
    plugin._run_edit_jobs.assert_awaited_once()
    data_urls = plugin._run_edit_jobs.await_args.kwargs["data_urls"]
    assert data_urls == ["data:image/png;base64,ZmFrZS1maWd1cmU="]
    assert plugin._run_edit_jobs.await_args.kwargs["size"] == "1024x1024"


@pytest.mark.asyncio
async def test_execute_edit_flow_no_longer_sends_pending_message_directly():
    module = _load_module()
    plugin = module.OpenAIImagePlugin(context=SimpleNamespace(), config={})
    plugin._image_gateway = object()
    plugin._generate_service = object()
    plugin._edit_service = object()
    plugin._task_service = object()
    plugin._build_image_data_url = AsyncMock(
        return_value="data:image/png;base64,ZmFrZQ=="
    )
    plugin._run_edit_jobs = AsyncMock(return_value=[])
    plugin._finalize_results = AsyncMock(
        return_value={
            "status": "success",
            "summary": "已编辑 0 张图片",
        }
    )
    event = _make_event(
        message_components=[SimpleNamespace(type="image", file="demo.png")]
    )

    await plugin._execute_edit_flow(
        event=event,
        prompt="真人化图像",
        count=1,
        send_user_message=True,
    )

    assert event._sent_messages == []
    plugin._finalize_results.assert_awaited_once()
    plugin._run_edit_jobs.assert_awaited_once()
    assert plugin._run_edit_jobs.await_args.kwargs["data_urls"] == [
        "data:image/png;base64,ZmFrZQ=="
    ]


@pytest.mark.asyncio
async def test_execute_generate_flow_passes_configured_negative_prompt():
    module = _load_module()
    plugin = module.OpenAIImagePlugin(
        context=SimpleNamespace(),
        config={"negative_prompt": "  低清晰度、文字水印  "},
    )
    plugin._image_gateway = object()
    plugin._generate_service = SimpleNamespace(
        generate=AsyncMock(return_value=Path("generated.png"))
    )
    plugin._task_service = SimpleNamespace()

    async def _run_task(**kwargs):
        result_path = await kwargs["job_coro"]()
        return {"success": True, "payload": str(result_path), "timings": {}}

    plugin._task_service.run_task = AsyncMock(side_effect=_run_task)
    plugin._finalize_results = AsyncMock(
        return_value={
            "status": "success",
            "summary": "已生成 1 张图片",
        }
    )

    await plugin._execute_generate_flow(
        event=_make_event(),
        prompt="生成一只猫",
        count=1,
        send_user_message=False,
    )

    plugin._generate_service.generate.assert_awaited_once_with(
        model="gpt-5.4-mini",
        prompt="生成一只猫",
        negative_prompt="低清晰度、文字水印",
        endpoint_type="responses",
        size=None,
        quality="auto",
        moderation="low",
    )


@pytest.mark.asyncio
async def test_execute_generate_flow_uses_images_default_model_when_configured():
    module = _load_module()
    plugin = module.OpenAIImagePlugin(
        context=SimpleNamespace(),
        config={"endpoint_type": "images"},
    )
    plugin._image_gateway = object()
    plugin._generate_service = SimpleNamespace(
        generate=AsyncMock(return_value=Path("generated.png"))
    )
    plugin._task_service = SimpleNamespace()

    async def _run_task(**kwargs):
        result_path = await kwargs["job_coro"]()
        return {"success": True, "payload": str(result_path), "timings": {}}

    plugin._task_service.run_task = AsyncMock(side_effect=_run_task)
    plugin._finalize_results = AsyncMock(
        return_value={
            "status": "success",
            "summary": "已生成 1 张图片",
        }
    )

    await plugin._execute_generate_flow(
        event=_make_event(),
        prompt="生成一只猫",
        count=1,
        send_user_message=False,
    )

    plugin._generate_service.generate.assert_awaited_once_with(
        model="gpt-image-2",
        prompt="生成一只猫",
        negative_prompt="",
        endpoint_type="images",
        size=None,
        quality="auto",
        moderation="low",
    )


@pytest.mark.asyncio
async def test_execute_generate_flow_passes_command_size_override():
    module = _load_module()
    plugin = module.OpenAIImagePlugin(
        context=SimpleNamespace(),
        config={"image_size": "1024x1024"},
    )
    plugin._image_gateway = object()
    plugin._generate_service = SimpleNamespace(
        generate=AsyncMock(return_value=Path("generated.png"))
    )
    plugin._task_service = SimpleNamespace()

    async def _run_task(**kwargs):
        result_path = await kwargs["job_coro"]()
        return {"success": True, "payload": str(result_path), "timings": {}}

    plugin._task_service.run_task = AsyncMock(side_effect=_run_task)
    plugin._finalize_results = AsyncMock(
        return_value={
            "status": "success",
            "summary": "已生成 1 张图片",
        }
    )

    await plugin._execute_generate_flow(
        event=_make_event(),
        prompt="生成一只猫",
        count=1,
        size="1024x1536",
        send_user_message=False,
    )

    plugin._generate_service.generate.assert_awaited_once_with(
        model="gpt-5.4-mini",
        prompt="生成一只猫",
        negative_prompt="",
        endpoint_type="responses",
        size="1024x1536",
        quality="auto",
        moderation="low",
    )


@pytest.mark.asyncio
async def test_execute_generate_flow_passes_quality_and_moderation_override():
    module = _load_module()
    plugin = module.OpenAIImagePlugin(context=SimpleNamespace(), config={})
    plugin._image_gateway = object()
    plugin._generate_service = SimpleNamespace(
        generate=AsyncMock(return_value=Path("generated.png"))
    )
    plugin._task_service = SimpleNamespace()

    async def _run_task(**kwargs):
        result_path = await kwargs["job_coro"]()
        return {"success": True, "payload": str(result_path), "timings": {}}

    plugin._task_service.run_task = AsyncMock(side_effect=_run_task)
    plugin._finalize_results = AsyncMock(
        return_value={
            "status": "success",
            "summary": "已生成 1 张图片",
        }
    )

    await plugin._execute_generate_flow(
        event=_make_event(),
        prompt="生成一只猫",
        count=1,
        quality="high",
        moderation="auto",
        send_user_message=False,
    )

    plugin._generate_service.generate.assert_awaited_once_with(
        model="gpt-5.4-mini",
        prompt="生成一只猫",
        negative_prompt="",
        endpoint_type="responses",
        size=None,
        quality="high",
        moderation="auto",
    )


@pytest.mark.asyncio
async def test_run_generate_jobs_sends_each_success_image_individually(tmp_path: Path):
    module = _load_module()
    plugin = module.OpenAIImagePlugin(context=SimpleNamespace(), config={})
    first_path = tmp_path / "first.png"
    second_path = tmp_path / "second.png"
    plugin._generate_service = SimpleNamespace(
        generate=AsyncMock(side_effect=[first_path, second_path])
    )
    plugin._task_service = SimpleNamespace()
    plugin.presenter = SimpleNamespace(send_images=AsyncMock())

    async def _run_task(**kwargs):
        result_path = await kwargs["job_coro"]()
        return {"success": True, "payload": str(result_path), "timings": {}}

    plugin._task_service.run_task = AsyncMock(side_effect=_run_task)
    event = _make_event()

    task_results = await plugin._run_generate_jobs(
        task_id="task-demo",
        count=2,
        prompt="生成两张图",
        event=event,
        send_each_result=True,
    )

    assert [result["payload"] for result in task_results] == [
        str(first_path),
        str(second_path),
    ]
    assert plugin.presenter.send_images.await_count == 2
    assert plugin.presenter.send_images.await_args_list[0].args == (event, [first_path])
    assert plugin.presenter.send_images.await_args_list[1].args == (
        event,
        [second_path],
    )


@pytest.mark.asyncio
async def test_finalize_results_skips_batch_send_when_results_already_sent(
    tmp_path: Path,
):
    module = _load_module()
    plugin = module.OpenAIImagePlugin(context=SimpleNamespace(), config={})
    plugin.presenter = SimpleNamespace(send_images=AsyncMock())

    result = await plugin._finalize_results(
        event=_make_event(),
        task_id="task-demo",
        mode="generate",
        task_results=[
            {
                "success": True,
                "payload": str(tmp_path / "generated.png"),
                "timings": {},
            }
        ],
        command_start=0,
        send_user_message=True,
        results_already_sent=True,
    )

    plugin.presenter.send_images.assert_not_awaited()
    assert result["status"] == "success"
    assert result["summary"] == "已生成 1 张图片"


@pytest.mark.asyncio
async def test_run_edit_jobs_uses_images_default_model_when_configured():
    module = _load_module()
    plugin = module.OpenAIImagePlugin(
        context=SimpleNamespace(),
        config={"endpoint_type": "images"},
    )
    plugin._edit_service = SimpleNamespace(
        edit=AsyncMock(return_value=Path("edited.png"))
    )
    plugin._task_service = SimpleNamespace()

    async def _run_task(**kwargs):
        result_path = await kwargs["job_coro"]()
        return {"success": True, "payload": str(result_path), "timings": {}}

    plugin._task_service.run_task = AsyncMock(side_effect=_run_task)

    await plugin._run_edit_jobs(
        task_id="task-demo",
        count=1,
        prompt="改成星见雅",
        data_urls=["data:image/png;base64,ZmFrZQ=="],
    )

    plugin._edit_service.edit.assert_awaited_once_with(
        model="gpt-image-2",
        prompt="改成星见雅",
        data_urls=["data:image/png;base64,ZmFrZQ=="],
        negative_prompt="",
        endpoint_type="images",
        size=None,
        quality="auto",
        moderation="low",
    )


@pytest.mark.asyncio
async def test_execute_edit_flow_passes_all_detected_images_to_edit_jobs():
    module = _load_module()
    plugin = module.OpenAIImagePlugin(context=SimpleNamespace(), config={})
    plugin._image_gateway = object()
    plugin._generate_service = object()
    plugin._edit_service = object()
    plugin._task_service = object()
    plugin._build_image_data_url = AsyncMock(
        side_effect=[
            "data:image/png;base64,Zmlyc3Q=",
            "data:image/jpeg;base64,c2Vjb25k",
        ]
    )
    plugin._run_edit_jobs = AsyncMock(return_value=[])
    plugin._finalize_results = AsyncMock(
        return_value={
            "status": "success",
            "summary": "已编辑 0 张图片",
        }
    )
    event = _make_event(
        message_components=[
            SimpleNamespace(type="image", file="first.png"),
            SimpleNamespace(type="image", file="second.jpg"),
        ]
    )

    await plugin._execute_edit_flow(
        event=event,
        prompt="融合两张图",
        count=1,
        send_user_message=False,
    )

    plugin._run_edit_jobs.assert_awaited_once()
    assert plugin._run_edit_jobs.await_args.kwargs["data_urls"] == [
        "data:image/png;base64,Zmlyc3Q=",
        "data:image/jpeg;base64,c2Vjb25k",
    ]


@pytest.mark.asyncio
async def test_execute_edit_flow_skips_pending_message_for_llm_tool():
    module = _load_module()
    plugin = module.OpenAIImagePlugin(context=SimpleNamespace(), config={})
    plugin._image_gateway = object()
    plugin._generate_service = object()
    plugin._edit_service = object()
    plugin._task_service = object()
    plugin._build_image_data_url = AsyncMock(
        return_value="data:image/png;base64,ZmFrZQ=="
    )
    plugin._run_edit_jobs = AsyncMock(return_value=[])
    plugin._finalize_results = AsyncMock(
        return_value={
            "status": "success",
            "summary": "已编辑 0 张图片",
        }
    )
    event = _make_event(
        message_components=[SimpleNamespace(type="image", file="demo.png")]
    )

    await plugin._execute_edit_flow(
        event=event,
        prompt="真人化图像",
        count=1,
        send_user_message=False,
    )

    assert event._sent_messages == []
    plugin._finalize_results.assert_awaited_once()


def test_llm_tool_docstring_declares_args_for_prompt_and_options():
    module = _load_module()

    generate_doc = module.OpenAIImagePlugin.openai_generate_image_tool.__doc__ or ""
    edit_doc = module.OpenAIImagePlugin.openai_edit_image_tool.__doc__ or ""
    figure_doc = (
        module.OpenAIImagePlugin.openai_edit_robot_figure_image_tool.__doc__ or ""
    )

    assert "Args:" in generate_doc
    assert "prompt(string)" in generate_doc
    assert "count(number)" in generate_doc
    assert "use_english_enhancement" not in generate_doc

    assert "Args:" in edit_doc
    assert "prompt(string)" in edit_doc
    assert "count(number)" in edit_doc
    assert "use_english_enhancement" not in edit_doc

    assert "Args:" in figure_doc
    assert "prompt(string)" in figure_doc
    assert "机器人形象" in figure_doc


def test_plugin_registers_command_and_tool_names():
    module = _load_module()

    source = Path(module.__file__).read_text(encoding="utf-8")

    assert '@filter.command("oaiimg")' in source
    assert '@filter.command("oaiedit")' in source
    assert '@filter.command("oaiqlogo")' in source
    assert '@filter.command("oaifigure")' in source
    assert '@filter.llm_tool(name="openai_generate_image")' in source
    assert '@filter.llm_tool(name="openai_edit_image")' in source
    assert '@filter.llm_tool(name="openai_edit_robot_figure_image")' in source
    assert '@filter.command("opimg")' not in source
    assert '@filter.command("opedit")' not in source


@pytest.mark.asyncio
async def test_qlogo_command_requires_at_mention():
    module = _load_module()
    plugin = module.OpenAIImagePlugin(context=SimpleNamespace(), config={})
    plugin._rebuild_runtime_dependencies = lambda: None
    plugin._image_gateway = object()
    plugin._generate_service = object()

    event = _make_event()
    await plugin._handle_qlogo_command(event, raw_prompt="改成动漫头像")

    event.send.assert_awaited_once()
    assert "请在命令中 @ 一位 QQ 用户" in event._sent_messages[0]


@pytest.mark.asyncio
async def test_qlogo_command_keeps_parameters_in_prompt_for_non_bot_admin():
    module = _load_module()
    plugin = module.OpenAIImagePlugin(context=SimpleNamespace(), config={})
    plugin._execute_avatar_edit_flow = AsyncMock(
        return_value={
            "status": "success",
            "summary": "已编辑 1 张图片",
        }
    )
    event = _make_event(
        message_text="/oaiqlogo @user 2 -q high -m auto 改成动漫头像",
        message_components=[SimpleNamespace(type="at", qq="123456")],
        is_admin=False,
    )

    await plugin._handle_qlogo_command(
        event,
        raw_prompt="2 -q high -m auto 改成动漫头像",
    )

    event.send.assert_awaited_once()
    assert "收到请求" in event._sent_messages[0]
    plugin._execute_avatar_edit_flow.assert_awaited_once()
    call_kwargs = plugin._execute_avatar_edit_flow.await_args.kwargs
    assert call_kwargs["count"] == 1
    assert call_kwargs["quality"] == "auto"
    assert call_kwargs["moderation"] == "low"
    assert call_kwargs["prompt"] == "2 -q high -m auto 改成动漫头像"


@pytest.mark.asyncio
async def test_qlogo_command_uses_first_at_target_and_executes_edit_flow():
    module = _load_module()
    plugin = module.OpenAIImagePlugin(context=SimpleNamespace(), config={})
    plugin._execute_avatar_edit_flow = AsyncMock(
        return_value={
            "status": "success",
            "summary": "已编辑 1 张图片",
        }
    )
    event = _make_event(
        message_text="/oaiqlogo @user 改成动漫头像",
        message_components=[
            SimpleNamespace(type="at", qq="123456"),
            SimpleNamespace(type="at", qq="654321"),
        ],
    )

    await plugin._handle_qlogo_command(event, raw_prompt="改成动漫头像")

    event.send.assert_awaited_once()
    assert "收到请求" in event._sent_messages[0]
    plugin._execute_avatar_edit_flow.assert_awaited_once()
    call_kwargs = plugin._execute_avatar_edit_flow.await_args.kwargs
    assert call_kwargs["qq_id"] == "123456"
    assert call_kwargs["prompt"] == "改成动漫头像"
    assert call_kwargs["count"] == 1
    assert call_kwargs["quality"] == "auto"
    assert call_kwargs["moderation"] == "low"
    assert call_kwargs["send_user_message"] is True


@pytest.mark.asyncio
async def test_qlogo_command_passes_quality_and_moderation_override():
    module = _load_module()
    plugin = module.OpenAIImagePlugin(context=SimpleNamespace(), config={})
    plugin._execute_avatar_edit_flow = AsyncMock(
        return_value={
            "status": "success",
            "summary": "已编辑 1 张图片",
        }
    )
    event = _make_event(
        message_text="/oaiqlogo @user -q high -m auto 改成动漫头像",
        message_components=[SimpleNamespace(type="at", qq="123456")],
        is_admin=True,
    )

    await plugin._handle_qlogo_command(
        event,
        raw_prompt="-q high -m auto 改成动漫头像",
    )

    call_kwargs = plugin._execute_avatar_edit_flow.await_args.kwargs
    assert call_kwargs["prompt"] == "改成动漫头像"
    assert call_kwargs["quality"] == "high"
    assert call_kwargs["moderation"] == "auto"


@pytest.mark.asyncio
async def test_edit_command_sends_pending_message_for_reply_image():
    module = _load_module()
    plugin = module.OpenAIImagePlugin(context=SimpleNamespace(), config={})
    plugin._execute_edit_flow = AsyncMock(
        return_value={
            "status": "success",
            "summary": "已编辑 1 张图片",
        }
    )
    event = _make_event(
        platform_name="other",
        message_components=[
            SimpleNamespace(
                type="reply",
                chain=[SimpleNamespace(type="image", file="reply.png")],
            )
        ],
    )

    results = await _collect_asyncgen(
        plugin._handle_edit_command(event, raw_prompt="改成水彩风格")
    )

    assert results == []
    event.send.assert_awaited_once()
    assert event._sent_messages[0] == '收到请求，prompt="改成水彩风格"，正在生成中...'
    plugin._execute_edit_flow.assert_awaited_once()
