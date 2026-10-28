from __future__ import annotations

import importlib
import json
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from astrbot.api.web import PluginRequest, bind_request_context


ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT.parent
for candidate in (str(PARENT), str(ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)


def _load_module():
    return importlib.import_module("astrbot_plugin_openai_image.core.web_api")


def _write_png(
    path: Path,
    size: tuple[int, int] = (64, 48),
    color: str = "red",
) -> None:
    Image.new("RGB", size, color).save(path, format="PNG")


@contextmanager
def _bind_request(*, query: dict | None = None, body=None, path_params=None):
    """构造最小 PluginRequest，让 handler 能在测试中读取 query 和 JSON 请求体。"""

    query_items = list((query or {}).items())

    class FakeRequest:
        method = "POST"
        headers: dict = {}
        cookies: dict = {}
        content_type = "application/json"
        client = None
        url = SimpleNamespace(path="/api/v1/plugins/extensions/demo/images")

        def __init__(self) -> None:
            self.query_params = SimpleNamespace(multi_items=lambda: query_items)

        async def json(self):
            return {} if body is None else body

        async def form(self):
            raise AssertionError("测试不覆盖表单请求")

    plugin_request = PluginRequest(
        FakeRequest(),
        path_params=path_params or {},
        plugin_name="astrbot_plugin_openai_image",
        username="tester",
    )
    with bind_request_context(plugin_request):
        yield plugin_request


def _read_json(response) -> dict:
    return json.loads(bytes(response.body).decode("utf-8"))


def _build_studio_api(tmp_path: Path, plugin=None):
    module = _load_module()
    cache_dir = tmp_path / "images"
    cache_dir.mkdir(parents=True, exist_ok=True)
    api = module.ImageStudioApi(
        plugin=plugin or SimpleNamespace(),
        cache_dir=cache_dir,
        webui_settings_path=tmp_path / "webui_settings.json",
    )
    api.prompt_optimizer_settings = module.PromptOptimizerSettingsStore(
        tmp_path / "prompt_optimizer_settings.json"
    )
    return api, cache_dir


# --- ImageLibrary ---


def test_image_library_lists_only_supported_images(tmp_path: Path):
    module = _load_module()
    (tmp_path / "20260510_120000_a.png").write_bytes(b"png")
    (tmp_path / "20260510_120001_b.webp").write_bytes(b"webp")
    (tmp_path / "20260510_120001_b.webp.json").write_text(
        '{"prompt":"生成一张湖边小屋","size":"1024x1024","mode":"generate"}',
        encoding="utf-8",
    )
    (tmp_path / "note.txt").write_text("skip", encoding="utf-8")

    images = module.ImageLibrary(tmp_path).list_images()

    assert [item["name"] for item in images] == [
        "20260510_120001_b.webp",
        "20260510_120000_a.png",
    ]
    assert images[0]["mime_type"] == "image/webp"
    assert images[0]["prompt"] == "生成一张湖边小屋"
    assert images[0]["generation_size"] == "1024x1024"
    assert images[0]["mode"] == "generate"
    assert "url" not in images[0]


def test_image_library_skips_file_deleted_during_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module = _load_module()
    (tmp_path / "deleted.png").write_bytes(b"png")
    library = module.ImageLibrary(tmp_path)

    def fake_build_metadata(_image_path: Path):
        raise FileNotFoundError("并发清理已删除图片")

    monkeypatch.setattr(library, "_build_image_metadata", fake_build_metadata)

    assert library.list_images() == []


def test_image_library_rejects_path_traversal(tmp_path: Path):
    module = _load_module()
    library = module.ImageLibrary(tmp_path)

    with pytest.raises(FileNotFoundError):
        library.resolve_image_path("../secret.png")

    with pytest.raises(FileNotFoundError):
        library.delete_image_by_name("../secret.png")


def test_image_library_resolves_and_deletes_image(tmp_path: Path):
    module = _load_module()
    image_path = tmp_path / "demo.png"
    metadata_path = tmp_path / "demo.png.json"
    image_path.write_bytes(b"demo")
    metadata_path.write_text('{"prompt":"删除测试"}', encoding="utf-8")
    library = module.ImageLibrary(tmp_path)

    image = library.get_image_by_name("demo.png")

    assert image["name"] == "demo.png"
    assert image["prompt"] == "删除测试"
    assert library.delete_image_by_name("demo.png") == "demo.png"
    assert not image_path.exists()
    assert not metadata_path.exists()



# --- ImageStudioApi ---


def test_register_declares_expected_routes(tmp_path: Path):
    module = _load_module()
    registered: list[tuple[str, list[str]]] = []

    class FakeContext:
        def register_web_api(self, route, handler, methods, desc):
            registered.append((route, methods))

    api, _cache_dir = _build_studio_api(
        tmp_path, plugin=SimpleNamespace(context=FakeContext())
    )
    api.register()

    assert registered == [
        (f"/{module.PLUGIN_NAME}/images", ["GET"]),
        (f"/{module.PLUGIN_NAME}/image", ["GET"]),
        (f"/{module.PLUGIN_NAME}/image/delete", ["POST"]),
        (f"/{module.PLUGIN_NAME}/generate", ["POST"]),
        (f"/{module.PLUGIN_NAME}/edit", ["POST"]),
        (f"/{module.PLUGIN_NAME}/optimize-prompt", ["POST"]),
        (f"/{module.PLUGIN_NAME}/prompt-optimizer-settings", ["GET"]),
        (f"/{module.PLUGIN_NAME}/prompt-optimizer-settings", ["POST"]),
        (f"/{module.PLUGIN_NAME}/image-provider-settings", ["GET"]),
        (f"/{module.PLUGIN_NAME}/image-provider-settings", ["POST"]),
        (f"/{module.PLUGIN_NAME}/providers-json", ["GET"]),
        (f"/{module.PLUGIN_NAME}/providers-json", ["POST"]),
    ]


def test_every_bridge_call_matches_a_registered_route(tmp_path: Path):
    """用 Dashboard 真实的路由匹配函数校验页面与后端的契约。

    bridge 的 apiGet/apiPost 只能把参数放进 query string，endpoint 本身决定
    path。如果后端注册了带 `<name>` 路径参数的路由，而页面只调用
    apiGet("image", {name})，path 会是 `<plugin>/image` 而不是 `<plugin>/image/<name>`，
    Dashboard 直接返回“未找到该路由”，页面表现为预览一直停在“读取中”。
    """
    match_registered_web_api = pytest.importorskip(
        "astrbot.dashboard.api.plugins"
    )._match_registered_web_api

    registered: list[tuple[str, object, list[str], str]] = []

    class FakeContext:
        def register_web_api(self, route, handler, methods, desc):
            registered.append((route, handler, methods, desc))

    api, _cache_dir = _build_studio_api(
        tmp_path, plugin=SimpleNamespace(context=FakeContext())
    )
    api.register()

    app_js = (ROOT / "pages" / "studio" / "app.js").read_text(encoding="utf-8")
    calls = re.findall(r'bridge\.api(Get|Post)\("([^"]+)"', app_js)
    assert calls, "未能从 app.js 中解析出任何 bridge 调用"

    for action, endpoint in calls:
        plugin_path = f"{_load_module().PLUGIN_NAME}/{endpoint}"
        matched = match_registered_web_api(registered, plugin_path, action)
        assert matched is not None, (
            f"bridge.{action}('{endpoint}') 请求的 {plugin_path} 没有匹配的已注册路由"
        )


@pytest.mark.asyncio
async def test_list_images_filters_sorts_and_paginates(tmp_path: Path):
    api, cache_dir = _build_studio_api(tmp_path)
    for index in range(3):
        _write_png(cache_dir / f"image_{index}.png")
    (cache_dir / "image_1.png.json").write_text(
        '{"prompt":"海边日落"}', encoding="utf-8"
    )
    (cache_dir / "note.txt").write_text("skip", encoding="utf-8")

    with _bind_request(query={"sort": "name", "page": "1", "page_size": "2"}):
        payload = _read_json(await api.list_images())

    assert payload["total"] == 3
    assert payload["total_pages"] == 2
    assert payload["page"] == 1
    assert [item["name"] for item in payload["images"]] == [
        "image_0.png",
        "image_1.png",
    ]
    assert all(
        item["thumbnail"].startswith("data:image/webp;base64,")
        for item in payload["images"]
    )

    with _bind_request(query={"keyword": "海边"}):
        filtered = _read_json(await api.list_images())

    assert filtered["total"] == 1
    assert filtered["images"][0]["name"] == "image_1.png"
    assert filtered["images"][0]["prompt"] == "海边日落"

    with _bind_request(query={"type": ".jpg"}):
        typed = _read_json(await api.list_images())

    assert typed["total"] == 0


@pytest.mark.asyncio
async def test_list_images_prunes_orphan_thumbnails(tmp_path: Path):
    api, cache_dir = _build_studio_api(tmp_path)
    _write_png(cache_dir / "kept.png")
    orphan = api.thumbnails.thumbnail_dir / "gone.png.webp"
    orphan.write_bytes(b"orphan")

    with _bind_request():
        await api.list_images()

    assert not orphan.exists()


@pytest.mark.asyncio
async def test_get_image_returns_full_data_url_and_404(tmp_path: Path):
    api, cache_dir = _build_studio_api(tmp_path)
    _write_png(cache_dir / "demo.png")

    with _bind_request(query={"name": "demo.png"}):
        payload = _read_json(await api.get_image())

    assert payload["image"]["name"] == "demo.png"
    assert payload["image"]["data_url"].startswith("data:image/png;base64,")

    with _bind_request(query={"name": "missing.png"}):
        missing = await api.get_image()

    assert missing.status_code == 404

    with _bind_request():
        without_name = await api.get_image()

    assert without_name.status_code == 404


@pytest.mark.asyncio
async def test_delete_image_removes_file_metadata_and_thumbnail(tmp_path: Path):
    api, cache_dir = _build_studio_api(tmp_path)
    _write_png(cache_dir / "demo.png")
    (cache_dir / "demo.png.json").write_text('{"prompt":"删除测试"}', encoding="utf-8")
    api.thumbnails.data_url(cache_dir / "demo.png")

    with _bind_request(body={"name": "demo.png"}):
        payload = _read_json(await api.delete_image())

    assert payload == {"deleted": "demo.png"}
    assert not (cache_dir / "demo.png").exists()
    assert not (cache_dir / "demo.png.json").exists()
    assert not (api.thumbnails.thumbnail_dir / "demo.png.webp").exists()

    with _bind_request(body={"name": "demo.png"}):
        missing = await api.delete_image()

    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_generate_image_runs_plugin_task_service(tmp_path: Path):
    output_path = tmp_path / "result.png"
    _write_png(output_path)

    class FakeGenerateService:
        async def generate(self, **kwargs):
            self.kwargs = kwargs
            return output_path

    class FakeTaskService:
        async def run_task(self, *, mode, job_coro, stage_name):
            return {
                "success": True,
                "mode": mode,
                "payload": await job_coro(),
                "timings": {"elapsed_ms": 1200},
            }

    generate_service = FakeGenerateService()
    plugin = SimpleNamespace(
        _generate_service=generate_service,
        _task_service=FakeTaskService(),
        _ensure_ready=lambda: None,
        _get_configured_model=lambda: "gpt-image-test",
        _get_endpoint_type=lambda: "images",
        _resolve_output_size=lambda size: size or "1024x1024",
    )
    api, cache_dir = _build_studio_api(tmp_path, plugin=plugin)
    (cache_dir / "result.png").write_bytes(output_path.read_bytes())

    with _bind_request(
        body={
            "prompt": "生成一张湖边小屋",
            "size": "1024x1536",
            "quality": "high",
            "moderation": "auto",
        }
    ):
        payload = _read_json(await api.generate_image())

    assert payload["image"]["name"] == "result.png"
    assert payload["timings"] == {"elapsed_ms": 1200}
    assert generate_service.kwargs == {
        "model": "gpt-image-test",
        "prompt": "生成一张湖边小屋",
        "endpoint_type": "images",
        "size": "1024x1536",
        "quality": "high",
        "moderation": "auto",
    }


@pytest.mark.asyncio
async def test_generate_image_rejects_empty_prompt_and_reports_failure(tmp_path: Path):
    class FakeTaskService:
        async def run_task(self, *, mode, job_coro, stage_name):
            return {
                "success": False,
                "mode": mode,
                "error_stage": "request",
                "error_message": "provider failed",
                "timings": {"elapsed_ms": 90},
            }

    plugin = SimpleNamespace(
        _task_service=FakeTaskService(),
        _ensure_ready=lambda: None,
        _get_configured_model=lambda: "gpt-image-test",
        _get_endpoint_type=lambda: "images",
        _resolve_output_size=lambda size: size,
    )
    api, _cache_dir = _build_studio_api(tmp_path, plugin=plugin)

    with _bind_request(body={"prompt": "   "}):
        empty = await api.generate_image()
    assert empty.status_code == 400

    with _bind_request(body={"prompt": "湖边小屋"}):
        failed = await api.generate_image()

    assert failed.status_code == 500
    assert _read_json(failed)["message"] == "provider failed"


@pytest.mark.asyncio
async def test_edit_image_forwards_multiple_reference_data_urls(tmp_path: Path):
    output_path = tmp_path / "edited.png"
    _write_png(output_path)

    class FakeEditService:
        async def edit(self, **kwargs):
            self.kwargs = kwargs
            return output_path

    class FakeTaskService:
        async def run_task(self, *, mode, job_coro, stage_name):
            return {
                "success": True,
                "mode": mode,
                "payload": await job_coro(),
                "timings": {},
            }

    edit_service = FakeEditService()
    plugin = SimpleNamespace(
        _edit_service=edit_service,
        _task_service=FakeTaskService(),
        _ensure_ready=lambda: None,
        _get_configured_model=lambda: "gpt-image-test",
        _get_endpoint_type=lambda: "responses",
        _resolve_output_size=lambda size: size,
    )
    api, cache_dir = _build_studio_api(tmp_path, plugin=plugin)
    (cache_dir / "edited.png").write_bytes(output_path.read_bytes())

    with _bind_request(
        body={
            "prompt": "按两张参考图重绘",
            "size": "auto",
            "quality": "high",
            "moderation": "low",
            "data_urls": [
                "data:image/png;base64,Zmlyc3Q=",
                "data:image/webp;base64,c2Vjb25k",
            ],
        }
    ):
        payload = _read_json(await api.edit_image())

    assert payload["image"]["name"] == "edited.png"
    assert edit_service.kwargs["data_urls"] == [
        "data:image/png;base64,Zmlyc3Q=",
        "data:image/webp;base64,c2Vjb25k",
    ]


@pytest.mark.asyncio
async def test_edit_image_requires_reference_images(tmp_path: Path):
    api, _cache_dir = _build_studio_api(tmp_path)

    with _bind_request(body={"prompt": "改成水彩", "data_urls": []}):
        missing = await api.edit_image()

    assert missing.status_code == 400
    assert _read_json(missing)["message"] == "请上传待编辑图片"


@pytest.mark.asyncio
async def test_optimize_prompt_returns_optimized_text(tmp_path: Path, monkeypatch):
    module = _load_module()
    api, _cache_dir = _build_studio_api(tmp_path)
    api.prompt_optimizer_settings.save_provider_id("custom_provider")

    async def fake_optimize_prompt_text(*, context, settings_store, prompt, mode):
        assert settings_store is api.prompt_optimizer_settings
        assert prompt == "浅色自然光下的现代别墅"
        assert mode == "generate"
        return "浅色自然光下的现代别墅，广角构图，高细节材质"

    monkeypatch.setattr(module, "optimize_prompt_text", fake_optimize_prompt_text)

    with _bind_request(body={"prompt": "浅色自然光下的现代别墅", "mode": "generate"}):
        payload = _read_json(await api.optimize_prompt())

    assert payload == {"prompt": "浅色自然光下的现代别墅，广角构图，高细节材质"}


@pytest.mark.asyncio
async def test_optimize_prompt_reports_missing_configuration(tmp_path: Path):
    api, _cache_dir = _build_studio_api(tmp_path)

    with _bind_request(body={"prompt": "湖边小屋", "mode": "generate"}):
        response = await api.optimize_prompt()

    assert response.status_code == 500
    assert "未找到可用的 AstrBot 对话模型提供商" in _read_json(response)["message"]


@pytest.mark.asyncio
async def test_prompt_optimizer_settings_round_trip(tmp_path: Path):
    fake_provider = SimpleNamespace(
        meta=lambda: SimpleNamespace(id="openai_1", model="gpt-4o", type="openai"),
        get_model=lambda: "gpt-4o",
    )
    fake_context = SimpleNamespace(
        get_all_providers=lambda: [fake_provider],
        get_using_provider=lambda: fake_provider,
        get_provider_by_id=lambda pid: fake_provider if pid == "openai_1" else None,
    )
    api, _cache_dir = _build_studio_api(
        tmp_path, plugin=SimpleNamespace(context=fake_context)
    )

    with _bind_request(body={"provider_id": "openai_1"}):
        saved = _read_json(await api.save_prompt_optimizer_settings())

    assert saved["provider_id"] == "openai_1"
    assert saved["default_model"] == "gpt-4o"
    assert saved["providers"] == [
        {"id": "openai_1", "model": "gpt-4o", "type": "openai"}
    ]

    with _bind_request():
        loaded = _read_json(await api.get_prompt_optimizer_settings())

    assert loaded == saved


@pytest.mark.asyncio
async def test_image_provider_settings_round_trip(tmp_path: Path):
    from astrbot_plugin_openai_image.core.provider_config import ImageProviderConfig

    providers = [
        ImageProviderConfig(
            provider_id="default",
            name="默认供应商",
            base_url="https://default.com/v1",
            api_key="",
            proxy_url="",
            model="gpt-5.4-mini",
            endpoint_type="responses",
        ),
        ImageProviderConfig(
            provider_id="custom",
            name="自定义供应商",
            base_url="https://custom.com/v1",
            api_key="",
            proxy_url="",
            model="dall-e-3",
            endpoint_type="images",
        ),
    ]
    fake_plugin = SimpleNamespace(
        config={"active_provider_id": "default"},
        _load_configured_providers=lambda: providers,
        _get_active_image_provider=lambda: next(
            p for p in providers if p.provider_id == fake_plugin.config["active_provider_id"]
        ),
        _rebuild_runtime_dependencies=lambda: None,
        _sync_schema_provider_options=lambda: None,
    )
    api, _cache_dir = _build_studio_api(tmp_path, plugin=fake_plugin)

    with _bind_request():
        initial = _read_json(await api.get_image_provider_settings())
    assert initial["active_provider_id"] == "default"
    assert initial["active_model"] == "gpt-5.4-mini"
    assert len(initial["providers"]) == 2

    with _bind_request(body={"active_provider_id": "custom"}):
        saved = _read_json(await api.save_image_provider_settings())
    assert saved["active_provider_id"] == "custom"
    assert saved["active_model"] == "dall-e-3"
    # WebUI 独立选型：不修改插件全局 active_provider_id 配置
    assert fake_plugin.config["active_provider_id"] == "default"
    assert api.webui_settings.get_active_provider_id() == "custom"
    assert api.get_webui_active_image_provider().provider_id == "custom"

    with _bind_request(body={"active_provider_id": "nonexistent"}):
        err = _read_json(await api.save_image_provider_settings())
    assert "未找到指定的图片供应商" in err["message"]


@pytest.mark.asyncio
async def test_providers_json_round_trip(tmp_path: Path):
    from astrbot_plugin_openai_image.core.provider_config import (
        ImageProviderConfig,
        load_providers_from_file,
    )
    import astrbot_plugin_openai_image.core.provider_config as pc

    test_providers_file = tmp_path / "providers.json"
    orig_path_fn = pc.get_default_providers_path
    pc.get_default_providers_path = lambda: test_providers_file

    try:
        fake_plugin = SimpleNamespace(
            config={"active_provider_id": "default"},
            _load_configured_providers=lambda: load_providers_from_file(test_providers_file),
            _get_active_image_provider=lambda: fake_plugin._load_configured_providers()[0],
            _rebuild_runtime_dependencies=lambda: None,
            _sync_schema_provider_options=lambda: None,
        )
        api, _cache_dir = _build_studio_api(tmp_path, plugin=fake_plugin)

        with _bind_request():
            res = _read_json(await api.get_providers_json())
        assert "providers.json" in res["path"]
        assert "default" in res["content"]

        new_content = json.dumps(
            [
                {
                    "provider_id": "new_p",
                    "name": "新供应商",
                    "base_url": "https://new.com/v1",
                    "model": "flux",
                }
            ],
            ensure_ascii=False,
            indent=2,
        )
        with _bind_request(body={"content": new_content}):
            saved = _read_json(await api.save_providers_json())
        assert saved["active_provider_id"] == "new_p"
        assert len(saved["providers"]) == 1
        assert saved["providers"][0]["model"] == "flux"

        with _bind_request(body={"content": "{bad json"}):
            err = _read_json(await api.save_providers_json())
        assert "JSON 格式解析失败" in err["message"]
    finally:
        pc.get_default_providers_path = orig_path_fn


def test_prompt_optimizer_settings_store_saves_provider_id(tmp_path: Path):
    module = _load_module()
    settings_path = tmp_path / "prompt_optimizer_settings.json"
    store = module.PromptOptimizerSettingsStore(settings_path)

    assert store.get_provider_id() == ""

    saved = store.save_provider_id("deepseek_1")
    assert saved == {"provider_id": "deepseek_1"}
    assert store.get_provider_id() == "deepseek_1"
    assert json.loads(settings_path.read_text(encoding="utf-8")) == {
        "provider_id": "deepseek_1"
    }


@pytest.mark.asyncio
async def test_optimize_prompt_text_calls_provider_text_chat(tmp_path: Path):
    module = _load_module()
    settings_path = tmp_path / "prompt_optimizer_settings.json"
    store = module.PromptOptimizerSettingsStore(settings_path)

    chat_called = {}

    class FakeProvider:
        def __init__(self, model="gpt-4o", pid="openai_1"):
            self.model = model
            self.id = pid

        def get_model(self):
            return self.model

        def meta(self):
            return SimpleNamespace(id=self.id, model=self.model, type="openai")

        async def text_chat(self, prompt, system_prompt):
            chat_called["prompt"] = prompt
            chat_called["system_prompt"] = system_prompt
            return SimpleNamespace(
                role="assistant",
                completion_text="```\nA photorealistic wooden cabin in morning mist by the lake, cinematic lighting\n```",
            )

    provider = FakeProvider()
    context = SimpleNamespace(
        get_all_providers=lambda: [provider],
        get_using_provider=lambda: provider,
        get_provider_by_id=lambda pid: provider if pid == "openai_1" else None,
    )

    # 1. 默认未设置 provider_id 时，自动使用默认模型
    result = await module.optimize_prompt_text(
        context=context,
        settings_store=store,
        prompt="湖边木屋",
        mode="generate",
    )

    assert result == "A photorealistic wooden cabin in morning mist by the lake, cinematic lighting"
    assert "当前任务类型：文生图" in chat_called["prompt"]
    assert "湖边木屋" in chat_called["prompt"]
    assert chat_called["system_prompt"] == module.PROMPT_OPTIMIZER_SYSTEM_PROMPT

    # 2. 设置指定 provider_id
    store.save_provider_id("openai_1")
    result2 = await module.optimize_prompt_text(
        context=context,
        settings_store=store,
        prompt="湖边木屋",
        mode="generate",
    )
    assert result2 == "A photorealistic wooden cabin in morning mist by the lake, cinematic lighting"


@pytest.mark.asyncio
async def test_optimize_prompt_text_handles_error_response(tmp_path: Path):
    module = _load_module()
    settings_path = tmp_path / "prompt_optimizer_settings.json"
    store = module.PromptOptimizerSettingsStore(settings_path)

    class ErrorProvider:
        async def text_chat(self, prompt, system_prompt):
            return SimpleNamespace(role="err", completion_text="Rate limit exceeded")

    provider = ErrorProvider()
    context = SimpleNamespace(
        get_all_providers=lambda: [provider],
        get_using_provider=lambda: provider,
    )

    with pytest.raises(RuntimeError, match="Rate limit exceeded"):
        await module.optimize_prompt_text(
            context=context,
            settings_store=store,
            prompt="湖边木屋",
            mode="generate",
        )


# --- 日志 ---


def test_web_api_logs_request_with_safe_prompt_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module = _load_module()
    info_calls: list[tuple[str, tuple[object, ...]]] = []

    class FakeLogger:
        def info(self, message: str, *args: object) -> None:
            info_calls.append((message, args))

    monkeypatch.setattr(module, "logger", FakeLogger())
    api, _cache_dir = _build_studio_api(
        tmp_path,
        plugin=SimpleNamespace(
            _get_endpoint_type=lambda: "images",
            _get_configured_model=lambda: "gpt-image-test",
        ),
    )

    api._log_web_request(
        mode="generate",
        prompt=f"第一行\n{'猫' * 160}",
        size="1024x1536",
        quality="high",
        moderation="low",
    )

    message, args = info_calls[0]
    assert "[OpenAIImage][webui][%s] 收到请求" in message
    assert args[0] == "generate"
    assert "\n" not in str(args[1])
    assert str(args[1]) == f"第一行 {'猫' * 160}"
    assert args[-2:] == ("images", "gpt-image-test")


def test_web_api_logs_task_success_and_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module = _load_module()
    info_calls: list[tuple[str, tuple[object, ...]]] = []
    warning_calls: list[tuple[str, tuple[object, ...]]] = []

    class FakeLogger:
        def info(self, message: str, *args: object) -> None:
            info_calls.append((message, args))

        def warning(self, message: str, *args: object) -> None:
            warning_calls.append((message, args))

    monkeypatch.setattr(module, "logger", FakeLogger())
    api, _cache_dir = _build_studio_api(tmp_path)

    api._log_task_result(
        {
            "success": True,
            "mode": "web_generate",
            "payload": tmp_path / "result.png",
            "timings": {"elapsed_ms": 1200, "queue_wait_ms": 30},
        }
    )
    api._log_task_result(
        {
            "success": False,
            "mode": "web_edit",
            "error_stage": "web_edit",
            "error_message": "provider failed",
            "timings": {"elapsed_ms": 90, "queue_wait_ms": 5},
        }
    )

    assert info_calls[0] == (
        "[OpenAIImage][webui][%s] 任务完成 output=%s elapsed_ms=%s queue_wait_ms=%s",
        ("web_generate", "result.png", 1200, 30),
    )
    assert warning_calls[0] == (
        "[OpenAIImage][webui][%s] 任务失败 stage=%s error=%s elapsed_ms=%s queue_wait_ms=%s",
        ("web_edit", "web_edit", "provider failed", 90, 5),
    )


# --- 任务闭包 ---


@pytest.mark.asyncio
async def test_create_generation_job_uses_plugin_generate_service(tmp_path: Path):
    module = _load_module()
    output_path = tmp_path / "result.png"

    class FakeGenerateService:
        async def generate(self, **kwargs):
            self.kwargs = kwargs
            return output_path

    plugin = SimpleNamespace(
        _generate_service=FakeGenerateService(),
        _get_configured_model=lambda: "gpt-image-test",
        _get_endpoint_type=lambda: "images",
        _resolve_output_size=lambda size: size,
    )

    job = module.create_generation_job(
        plugin=plugin,
        prompt="生成一张湖边小屋",
        size="1024x1024",
        quality="high",
        moderation="auto",
    )

    assert await job() == output_path
    assert plugin._generate_service.kwargs == {
        "model": "gpt-image-test",
        "prompt": "生成一张湖边小屋",
        "endpoint_type": "images",
        "size": "1024x1024",
        "quality": "high",
        "moderation": "auto",
    }


@pytest.mark.asyncio
async def test_create_edit_job_uses_plugin_edit_service(tmp_path: Path):
    module = _load_module()
    output_path = tmp_path / "edited.png"

    class FakeEditService:
        async def edit(self, **kwargs):
            self.kwargs = kwargs
            return output_path

    plugin = SimpleNamespace(
        _edit_service=FakeEditService(),
        _get_configured_model=lambda: "gpt-image-test",
        _get_endpoint_type=lambda: "responses",
        _resolve_output_size=lambda size: size,
    )

    job = module.create_edit_job(
        plugin=plugin,
        prompt="改成水彩风格",
        data_urls=["data:image/png;base64,aGVsbG8=", "data:image/png;base64,d29ybGQ="],
        size="auto",
        quality="medium",
        moderation="low",
    )

    assert await job() == output_path
    assert plugin._edit_service.kwargs == {
        "model": "gpt-image-test",
        "prompt": "改成水彩风格",
        "data_urls": [
            "data:image/png;base64,aGVsbG8=",
            "data:image/png;base64,d29ybGQ=",
        ],
        "endpoint_type": "responses",
        "size": "auto",
        "quality": "medium",
        "moderation": "low",
    }


# --- 页面静态资源 ---


def test_page_assets_exist_and_use_bridge_only():
    page_root = ROOT / "pages" / "studio"
    index_html = (page_root / "index.html").read_text(encoding="utf-8")
    app_js = (page_root / "app.js").read_text(encoding="utf-8")
    style_css = (page_root / "style.css").read_text(encoding="utf-8")

    # 受限 iframe 无法直接 fetch 鉴权接口，页面只能通过 bridge 调后端。
    assert "window.AstrBotPluginPage" in app_js
    assert "await bridge.ready();" in app_js
    assert 'fetch("/api/' not in app_js
    assert "Authorization" not in app_js
    # 受限 iframe 没有 localStorage 和 IndexedDB，状态只能留在内存里。
    assert "localStorage" not in app_js
    assert "indexedDB" not in app_js

    assert "./style.css" in index_html
    assert './app.js' in index_html

    # 主题跟随 Dashboard，通过 data-theme 覆盖亮暗配色。
    assert '[data-theme="dark"]' in style_css
    for tab in ("create", "gallery", "settings"):
        assert f'data-tab="{tab}"' in index_html
    assert 'id="referenceChips"' in index_html
    assert "multiple" in index_html


def test_page_never_opens_a_new_window():
    """页面不得依赖 window.open 或 target=_blank。

    承载插件页面的 iframe sandbox 是 `allow-scripts allow-forms allow-downloads`，
    不含 allow-popups，因此 window.open() 必定返回 null，打开新标签页永远不可用。
    查看原图只能靠页内浮层实现。
    """
    page_root = ROOT / "pages" / "studio"
    index_html = (page_root / "index.html").read_text(encoding="utf-8")
    app_js = (page_root / "app.js").read_text(encoding="utf-8")

    # 用带括号的形式匹配，避免命中解释该限制的注释文本。
    assert "window.open(" not in app_js
    assert "window.location" not in app_js
    assert "_blank" not in index_html
    assert 'id="lightbox"' in index_html
    assert 'id="lightboxImage"' in index_html
    assert '$("lightbox").classList.remove("hidden")' in app_js


def test_lightbox_stage_scales_images_without_clipping():
    """浮层必须能完整显示竖图，并且放大后四个方向都滚得到。

    两条硬约束：
    1. `.lightbox` 必须显式声明 grid-template-rows: minmax(0, 1fr)。固定定位虽然
       让容器高度确定，但隐式 auto 轨道仍是不确定高度，grid item 的百分比高度会
       按 none 处理，竖图就会按原始像素高度溢出并被 iframe 裁掉。
    2. 滚动容器居中必须用 margin: auto。place-items/align-items 居中会让超出
       容器的图片左上角和右上角滚不到，只能看到中间部分。
    """
    style_css = (ROOT / "pages" / "studio" / "style.css").read_text(encoding="utf-8")
    app_js = (ROOT / "pages" / "studio" / "app.js").read_text(encoding="utf-8")
    lightbox = re.search(r"\.lightbox\s*\{(?P<body>[^}]+)\}", style_css)
    stage = re.search(r"\.lightbox-stage\s*\{(?P<body>[^}]+)\}", style_css)
    image_rule = re.search(r"\.lightbox-image\s*\{(?P<body>[^}]+)\}", style_css)
    assert lightbox is not None
    assert stage is not None
    assert image_rule is not None

    assert "grid-template-rows: minmax(0, 1fr);" in lightbox.group("body")

    stage_body = stage.group("body")
    image_body = image_rule.group("body")

    assert "overflow: auto;" in stage_body
    assert "min-height: 0;" in stage_body
    assert "place-items" not in stage_body
    assert "align-items" not in stage_body
    assert "margin: auto;" in image_body
    # flex 默认会压缩图片，JS 算出的显式宽高会被忽略。
    assert "flex: 0 0 auto;" in image_body
    # 百分比尺寸和 object-fit 都属于「适应屏幕」阶段的老做法，
    # 现在宽高由 JS 按缩放倍率写入，这些声明必须不存在。
    assert "width: 100%;" not in image_body
    assert "height: 100%;" not in image_body
    assert "object-fit" not in image_body
    # max-width/max-height 只能写成 none 用来清掉继承约束；
    # 一旦写成百分比就会重新把 JS 算出的尺寸 clamp 掉。
    assert "max-width: none;" in image_body
    assert "max-height: none;" in image_body
    assert "max-width: 100%" not in image_body
    assert "max-height: 100%" not in image_body
    # 宽高必须成对地从原始尺寸和倍率算出，比例才不会失真。
    assert (
        "${Math.round(lightbox.naturalWidth * scale)}px" in app_js
        and "${Math.round(" in app_js
    )
    assert "lightbox.naturalHeight * scale" in app_js


def test_lightbox_wheel_zoom_is_not_passive():
    """滚轮缩放必须注册为非 passive，否则 preventDefault 会被浏览器忽略。"""
    app_js = (ROOT / "pages" / "studio" / "app.js").read_text(encoding="utf-8")
    stage = (ROOT / "pages" / "studio" / "index.html").read_text(encoding="utf-8")

    assert 'id="lightboxStage"' in stage
    assert 'id="lightboxZoomLabel"' in stage
    assert re.search(r'addEventListener\(\s*"wheel"', app_js)
    assert "passive: false" in app_js
    # 缩放范围必须有上下限：下限保证图片不会比整屏还小，上限避免撑爆布局。
    assert "LIGHTBOX_MIN_ZOOM = 1;" in app_js
    assert "LIGHTBOX_MAX_ZOOM = 8;" in app_js
    # 未放大时不应该进入拖拽平移，否则会挡住点击关闭。
    assert "pointerdown" in app_js
    # 拖拽结束浏览器仍会派发 click，必须吞掉否则拖完就被误关。
    assert "suppressClick" in app_js


def test_lightbox_zoom_scale_comes_from_the_real_image():
    """缩放基准必须来自图片的真实像素尺寸，不能停留在初始的 0。

    lightbox 状态里的 naturalWidth/naturalHeight 初始为 0。若 layoutLightbox
    没有先把 img.naturalWidth/naturalHeight 写回状态，lightboxFitScale() 会命中
    `if (!naturalWidth) return 1` 的早退分支，applyLightboxZoom() 于是把
    `0 * 1` 写成 width/height，图片变成 0x0 完全不可见。
    """
    app_js = (ROOT / "pages" / "studio" / "app.js").read_text(encoding="utf-8")

    layout = re.search(
        r"function layoutLightbox\(\)\s*\{(?P<body>.*?)\n\}", app_js, re.S
    )
    assert layout is not None, "app.js 缺少 layoutLightbox"
    layout_body = layout.group("body")

    assert "lightbox.naturalWidth = " in layout_body
    assert "lightbox.naturalHeight = " in layout_body
    # 赋值必须发生在计算 fit 之前，否则 fit 读到的还是上一次的 0。
    assert layout_body.index("naturalWidth") < layout_body.index("lightboxFitScale()")

    # applyLightboxZoom 必须拒绝在尺寸未知时写样式，避免再次产出 0x0。
    apply_zoom = re.search(
        r"function applyLightboxZoom\(\)\s*\{(?P<body>.*?)\n\}", app_js, re.S
    )
    assert apply_zoom is not None
    assert re.search(
        r"if \(!lightbox\.naturalWidth \|\| !lightbox\.naturalHeight\) return", 
        apply_zoom.group("body"),
    )

    # 打开时先显示浮层再设置 src，关闭时清掉内联尺寸，避免沿用上一次的 0px。
    assert re.search(
        r"classList\.remove\(\"hidden\"\);(?s:.*?)src = image\.data_url;",
        app_js,
    )
    hide = re.search(r"function hideLightbox\(\)\s*\{(?P<body>.*?)\n\}", app_js, re.S)
    assert hide is not None
    assert 'removeAttribute("src")' in hide.group("body")
    assert "style.width" in hide.group("body")
    assert "style.height" in hide.group("body")


def test_page_i18n_file_covers_all_used_keys():
    i18n_data = json.loads(
        (ROOT / ".astrbot-plugin" / "i18n" / "zh-CN.json").read_text(encoding="utf-8")
    )
    page_strings = i18n_data["pages"]["studio"]

    assert page_strings["title"]
    assert page_strings["description"]

    app_js = (ROOT / "pages" / "studio" / "app.js").read_text(encoding="utf-8")
    index_html = (ROOT / "pages" / "studio" / "index.html").read_text(encoding="utf-8")
    used_keys = set(re.findall(r'(?<![\w.])t\(\s*"([^"]+)"', app_js))
    used_keys |= set(re.findall(r'data-i18n(?:-placeholder)?="([^"]+)"', index_html))

    assert used_keys == set(page_strings)


def test_standalone_web_admin_config_is_removed():
    assert not (ROOT / "core" / "web_admin.py").exists()
    conf_schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    assert not [key for key in conf_schema if key.startswith("web_admin_")]


def test_webui_settings_store_unit(tmp_path: Path):
    module = _load_module()
    settings_file = tmp_path / "webui_settings.json"
    store = module.WebUISettingsStore(settings_file)

    assert store.get_active_provider_id() == ""
    assert store.load() == {}

    saved = store.save_active_provider_id("local_sd")
    assert saved == {"active_provider_id": "local_sd"}
    assert store.get_active_provider_id() == "local_sd"
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {
        "active_provider_id": "local_sd"
    }


@pytest.mark.asyncio
async def test_webui_generate_uses_webui_selected_provider_independently(tmp_path: Path):
    from astrbot_plugin_openai_image.core.provider_config import ImageProviderConfig

    output_path = tmp_path / "webui_generated.png"
    _write_png(output_path)

    providers = [
        ImageProviderConfig(
            provider_id="default",
            name="默认供应商",
            base_url="https://default.com/v1",
            api_key="",
            proxy_url="",
            model="gpt-default",
            endpoint_type="responses",
        ),
        ImageProviderConfig(
            provider_id="webui_custom",
            name="WebUI独享供应商",
            base_url="https://custom.com/v1",
            api_key="",
            proxy_url="",
            model="flux-pro",
            endpoint_type="images",
        ),
    ]

    class FakeTaskService:
        async def run_task(self, *, mode, job_coro, stage_name):
            return {
                "success": True,
                "mode": mode,
                "payload": await job_coro(),
                "timings": {},
            }

    class FakeCacheStore:
        def save_image(self, image_bytes, extension):
            return output_path

        def save_image_metadata(self, image_path, metadata):
            pass

    recorded_calls = {}

    class FakeCustomGateway:
        async def request_image_generation(self, payload):
            recorded_calls.update(payload)
            return {"data": [{"b64_json": "Zmlyc3Q="}]}

        async def close(self):
            recorded_calls["closed"] = True

    fake_gateway = FakeCustomGateway()

    class FakePluginService:
        async def generate(self, **kwargs):
            raise AssertionError("插件全局服务不应被调用")

    fake_plugin = SimpleNamespace(
        config={"active_provider_id": "default"},
        _load_configured_providers=lambda: providers,
        _get_active_image_provider=lambda: providers[0],
        _get_configured_model=lambda: "gpt-default",
        _get_endpoint_type=lambda: "responses",
        _resolve_output_size=lambda size: size or "1024x1024",
        _ensure_ready=lambda: None,
        _task_service=FakeTaskService(),
        _generate_service=FakePluginService(),
        _cache_store=FakeCacheStore(),
        _create_image_gateway=lambda prov: fake_gateway if prov.provider_id == "webui_custom" else None,
    )

    api, cache_dir = _build_studio_api(tmp_path, plugin=fake_plugin)
    (cache_dir / "webui_generated.png").write_bytes(output_path.read_bytes())
    api.webui_settings.save_active_provider_id("webui_custom")

    with _bind_request(body={"prompt": "太空漫游", "size": "1024x1024"}):
        res = _read_json(await api.generate_image())

    assert res["image"]["name"] == "webui_generated.png"
    # 验证插件全局配置与 active_provider 完全不受影响
    assert fake_plugin.config["active_provider_id"] == "default"
    assert fake_plugin._get_active_image_provider().model == "gpt-default"
    # 验证 WebUI 生图使用的是 WebUI 独立选择的供应商参数
    assert recorded_calls["model"] == "flux-pro"
    assert recorded_calls["prompt"] == "太空漫游"

    await api.close()
    assert recorded_calls.get("closed") is True


@pytest.mark.asyncio
async def test_webui_edit_uses_webui_selected_provider_independently(tmp_path: Path):
    from astrbot_plugin_openai_image.core.provider_config import ImageProviderConfig

    output_path = tmp_path / "webui_edited.png"
    _write_png(output_path)

    providers = [
        ImageProviderConfig(
            provider_id="default",
            name="默认供应商",
            base_url="https://default.com/v1",
            api_key="",
            proxy_url="",
            model="gpt-default",
            endpoint_type="responses",
        ),
        ImageProviderConfig(
            provider_id="webui_custom",
            name="WebUI独享供应商",
            base_url="https://custom.com/v1",
            api_key="",
            proxy_url="",
            model="dall-e-3",
            endpoint_type="images",
        ),
    ]

    class FakeTaskService:
        async def run_task(self, *, mode, job_coro, stage_name):
            return {
                "success": True,
                "mode": mode,
                "payload": await job_coro(),
                "timings": {},
            }

    class FakeCacheStore:
        def save_image(self, image_bytes, extension):
            return output_path

        def save_image_metadata(self, image_path, metadata):
            pass

    recorded_calls = {}

    class FakeCustomGateway:
        async def request_image_edit(self, data, files):
            recorded_calls.update(data)
            recorded_calls["files"] = files
            return {"data": [{"b64_json": "Zmlyc3Q="}]}

        async def close(self):
            recorded_calls["closed"] = True

    fake_gateway = FakeCustomGateway()

    fake_plugin = SimpleNamespace(
        config={"active_provider_id": "default"},
        _load_configured_providers=lambda: providers,
        _get_active_image_provider=lambda: providers[0],
        _get_configured_model=lambda: "gpt-default",
        _get_endpoint_type=lambda: "responses",
        _resolve_output_size=lambda size: size or "1024x1024",
        _ensure_ready=lambda: None,
        _task_service=FakeTaskService(),
        _edit_service=SimpleNamespace(),
        _cache_store=FakeCacheStore(),
        _create_image_gateway=lambda prov: fake_gateway if prov.provider_id == "webui_custom" else None,
    )

    api, cache_dir = _build_studio_api(tmp_path, plugin=fake_plugin)
    (cache_dir / "webui_edited.png").write_bytes(output_path.read_bytes())
    api.webui_settings.save_active_provider_id("webui_custom")

    with _bind_request(
        body={
            "prompt": "风格迁移",
            "data_urls": ["data:image/png;base64,Zmlyc3Q="],
        }
    ):
        res = _read_json(await api.edit_image())

    assert res["image"]["name"] == "webui_edited.png"
    assert fake_plugin.config["active_provider_id"] == "default"
    assert recorded_calls["model"] == "dall-e-3"
    assert recorded_calls["prompt"] == "风格迁移"
    assert len(recorded_calls["files"]) == 1


def test_ensure_generate_aspect_ratio_preserves_existing():
    module = _load_module()
    # 包含 16:9
    text1 = "A cyber city, cinematic lighting, aspect ratio 16:9"
    assert module._ensure_generate_aspect_ratio(text1) == text1

    # 包含 1:1
    text2 = "A cute kitten, 1:1 aspect ratio"
    assert module._ensure_generate_aspect_ratio(text2) == text2

    # 包含 9:16
    text3 = "Portrait of a warrior, aspect ratio 9:16"
    assert module._ensure_generate_aspect_ratio(text3) == text3


def test_ensure_generate_aspect_ratio_appends_when_missing():
    module = _load_module()
    # 默认 1:1
    assert (
        module._ensure_generate_aspect_ratio("A cyber city")
        == "A cyber city, aspect ratio 1:1"
    )

    # 横屏输入 -> 16:9
    assert (
        module._ensure_generate_aspect_ratio("A cyber city", user_input="赛博朋克城市 横屏")
        == "A cyber city, aspect ratio 16:9"
    )

    # 竖屏输入 -> 9:16
    assert (
        module._ensure_generate_aspect_ratio("A girl portrait", user_input="少女头像 竖屏")
        == "A girl portrait, aspect ratio 9:16"
    )

    # 根据 size 推断 -> 16:9
    assert (
        module._ensure_generate_aspect_ratio("A landscape", size="1792x1024")
        == "A landscape, aspect ratio 16:9"
    )


@pytest.mark.asyncio
async def test_optimize_prompt_text_require_aspect_ratio(tmp_path: Path):
    module = _load_module()
    settings_path = tmp_path / "prompt_optimizer_settings.json"
    store = module.PromptOptimizerSettingsStore(settings_path)

    chat_called = {}

    class FakeProvider:
        async def text_chat(self, prompt, system_prompt):
            chat_called["prompt"] = prompt
            return SimpleNamespace(
                role="assistant",
                completion_text="Photorealistic mountain lake at dusk",
            )

    provider = FakeProvider()
    context = SimpleNamespace(
        get_all_providers=lambda: [provider],
        get_using_provider=lambda: provider,
        get_provider_by_id=lambda pid: provider,
    )

    # require_aspect_ratio=True 时必须带有比例
    res = await module.optimize_prompt_text(
        context=context,
        settings_store=store,
        prompt="山间湖泊 横屏",
        mode="generate",
        require_aspect_ratio=True,
    )
    assert "特别要求：提示词中必须包含画面比例" in chat_called["prompt"]
    assert res == "Photorealistic mountain lake at dusk, aspect ratio 16:9"

    # require_aspect_ratio=False 时不强制追加比例
    res_no_ratio = await module.optimize_prompt_text(
        context=context,
        settings_store=store,
        prompt="山间湖泊 横屏",
        mode="generate",
        require_aspect_ratio=False,
    )
    assert res_no_ratio == "Photorealistic mountain lake at dusk"


def test_clean_english_prompt_filters_preamble_and_chinese_decorations():
    module = _load_module()
    raw = (
        "好的，这是为您优化后的英文提示词：\n"
        "```markdown\n"
        "A cute golden retriever puppy sitting on grass, soft lighting, 8k resolution.\n"
        "```\n"
        "（注：光影采用了柔和漫射光）"
    )
    cleaned = module._clean_english_prompt(raw)
    assert cleaned == "A cute golden retriever puppy sitting on grass, soft lighting, 8k resolution."
    assert "好的" not in cleaned
    assert "（注" not in cleaned


def test_clean_english_prompt_converts_chinese_punctuation():
    module = _load_module()
    raw = "A photorealistic cat，running in a green field。Cinematic lighting：golden hour！"
    cleaned = module._clean_english_prompt(raw)
    assert cleaned == "A photorealistic cat, running in a green field. Cinematic lighting: golden hour!"


@pytest.mark.asyncio
async def test_optimize_prompt_text_translates_when_model_returns_chinese(tmp_path: Path):
    module = _load_module()
    settings_path = tmp_path / "prompt_optimizer_settings.json"
    store = module.PromptOptimizerSettingsStore(settings_path)

    calls = []

    class TranslatingProvider:
        async def text_chat(self, prompt, system_prompt):
            calls.append(prompt)
            if len(calls) == 1:
                # 第一次返回了中文提示词
                return SimpleNamespace(
                    role="assistant",
                    completion_text="湖边木屋，晨雾弥漫，电影质感光影",
                )
            # 第二次翻译返回了纯英文提示词
            return SimpleNamespace(
                role="assistant",
                completion_text="A wooden cabin by the lake in morning mist, cinematic lighting",
            )

    provider = TranslatingProvider()
    context = SimpleNamespace(
        get_all_providers=lambda: [provider],
        get_using_provider=lambda: provider,
        get_provider_by_id=lambda pid: provider,
    )

    res = await module.optimize_prompt_text(
        context=context,
        settings_store=store,
        prompt="湖边木屋",
        mode="generate",
    )
    assert len(calls) == 2
    assert "Translate the following image prompt into English only" in calls[1]
    assert res == "A wooden cabin by the lake in morning mist, cinematic lighting"


@pytest.mark.asyncio
async def test_optimize_prompt_text_uses_custom_system_prompt(tmp_path: Path):
    module = _load_module()
    settings_path = tmp_path / "prompt_optimizer_settings.json"
    store = module.PromptOptimizerSettingsStore(settings_path)

    chat_called = {}

    class CustomSysPromptProvider:
        async def text_chat(self, prompt, system_prompt):
            chat_called["system_prompt"] = system_prompt
            return SimpleNamespace(
                role="assistant",
                completion_text="An oil painting of a lonely lighthouse",
            )

    provider = CustomSysPromptProvider()
    context = SimpleNamespace(
        get_all_providers=lambda: [provider],
        get_using_provider=lambda: provider,
        get_provider_by_id=lambda pid: provider,
    )

    custom_sys = "You are a specialist in classic oil paintings."
    res = await module.optimize_prompt_text(
        context=context,
        settings_store=store,
        system_prompt=custom_sys,
        prompt="灯塔",
        mode="generate",
    )
    assert chat_called["system_prompt"] == custom_sys
    assert res == "An oil painting of a lonely lighthouse"





@pytest.mark.asyncio
async def test_create_generation_job_applies_local_prefix_for_local_provider():
    module = _load_module()

    generated_prompt = {}

    class FakeService:
        async def generate(self, model, prompt, **kwargs):
            generated_prompt["prompt"] = prompt
            return Path("dummy.png")

    fake_provider = SimpleNamespace(
        provider_id="local_sd",
        model="sdxl",
        endpoint_type="responses",
    )
    fake_plugin = SimpleNamespace(
        config={"local_model_prompt_prefix": "abc"},
        _get_active_image_provider=lambda: fake_provider,
        _maybe_apply_local_prompt_prefix=lambda prompt, provider=None: (
            "abc，" + prompt if (provider and provider.provider_id.startswith("local")) else prompt
        ),
    )

    job = module.create_generation_job(
        plugin=fake_plugin,
        prompt="一只猫",
        size=None,
        quality="auto",
        moderation="low",
        generate_service=FakeService(),
        provider=fake_provider,
    )

    res = await job()
    assert res == Path("dummy.png")
    assert generated_prompt["prompt"] == "abc，一只猫"
