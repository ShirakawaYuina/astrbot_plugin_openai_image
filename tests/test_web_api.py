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
    api.prompt_optimizer_settings.save_public_payload(
        {
            "model": "gpt-test",
            "base_url": "https://api.example.com/v1",
            "api_key": "secret-key",
        }
    )

    async def fake_optimize_prompt_text(*, settings_store, prompt, mode):
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
    assert "提示词优化模型名称" in _read_json(response)["message"]


@pytest.mark.asyncio
async def test_prompt_optimizer_settings_round_trip_hides_api_key(tmp_path: Path):
    api, _cache_dir = _build_studio_api(tmp_path)

    with _bind_request(
        body={
            "model": "gpt-test",
            "base_url": "https://api.example.com/v1",
            "api_key": "secret-key",
        }
    ):
        saved = _read_json(await api.save_prompt_optimizer_settings())

    assert saved == {
        "model": "gpt-test",
        "base_url": "https://api.example.com/v1",
        "has_api_key": True,
    }

    with _bind_request():
        loaded = _read_json(await api.get_prompt_optimizer_settings())

    assert loaded == saved
    assert "api_key" not in loaded


def test_prompt_optimizer_settings_require_all_fields():
    module = _load_module()

    with pytest.raises(ValueError, match="模型名称"):
        module.PromptOptimizerSettings.from_config({})

    with pytest.raises(ValueError, match="Base URL"):
        module.PromptOptimizerSettings.from_config({"prompt_optimizer_model": "gpt"})

    with pytest.raises(ValueError, match="API Key"):
        module.PromptOptimizerSettings.from_config(
            {
                "prompt_optimizer_model": "gpt",
                "prompt_optimizer_base_url": "https://api.example.com/v1",
            }
        )


def test_prompt_optimizer_settings_store_keeps_existing_api_key(tmp_path: Path):
    module = _load_module()
    settings_path = tmp_path / "prompt_optimizer_settings.json"
    store = module.PromptOptimizerSettingsStore(settings_path)

    saved = store.save_public_payload(
        {
            "model": " gpt-test ",
            "base_url": " https://api.example.com/v1 ",
            "api_key": " secret-key ",
        }
    )

    assert saved == {
        "model": "gpt-test",
        "base_url": "https://api.example.com/v1",
        "has_api_key": True,
    }
    assert json.loads(settings_path.read_text(encoding="utf-8")) == {
        "prompt_optimizer_model": "gpt-test",
        "prompt_optimizer_base_url": "https://api.example.com/v1",
        "prompt_optimizer_api_key": "secret-key",
    }

    saved_without_key = store.save_public_payload(
        {
            "model": "gpt-next",
            "base_url": "https://api.next.example.com/v1",
            "api_key": "",
        }
    )

    assert saved_without_key["has_api_key"] is True
    assert store.load()["prompt_optimizer_api_key"] == "secret-key"


def test_prompt_optimizer_endpoint_and_response_parser():
    module = _load_module()

    assert (
        module._resolve_chat_completions_endpoint("https://api.example.com/v1")
        == "https://api.example.com/v1/chat/completions"
    )
    assert (
        module._resolve_chat_completions_endpoint(
            "https://api.example.com/v1/chat/completions"
        )
        == "https://api.example.com/v1/chat/completions"
    )
    assert (
        module._extract_optimized_prompt(
            {"choices": [{"message": {"content": "  优化后的提示词  "}}]}
        )
        == "优化后的提示词"
    )
    assert (
        module._extract_optimized_prompt(
            {"choices": [{"message": {"content": [{"text": "第一段"}, {"text": "第二段"}]}}]}
        )
        == "第一段\n第二段"
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
    assert str(args[1]).endswith("...")
    assert len(str(args[1])) <= module.LOG_PROMPT_MAX_LENGTH + 3
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
    for tab in ("gallery", "generate", "edit", "settings"):
        assert f'data-tab="{tab}"' in index_html
    assert 'id="referenceThumbs"' in index_html
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
