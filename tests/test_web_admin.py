from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from yarl import URL


ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT.parent
for candidate in (str(PARENT), str(ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)


def _load_module():
    return importlib.import_module("astrbot_plugin_openai_image.core.web_admin")


def test_admin_settings_disabled_when_password_empty():
    module = _load_module()

    settings = module.WebAdminSettings.from_config(
        {
            "web_admin_enabled": True,
            "web_admin_port": 7890,
            "web_admin_password": "",
        }
    )

    assert settings.enabled is False
    assert settings.port == 7890
    assert settings.requested_enabled is True


def test_admin_settings_accepts_custom_host_port_and_password():
    module = _load_module()

    settings = module.WebAdminSettings.from_config(
        {
            "web_admin_enabled": True,
            "web_admin_host": "0.0.0.0",
            "web_admin_port": "7001",
            "web_admin_password": "secret",
        }
    )

    assert settings.enabled is True
    assert settings.host == "0.0.0.0"
    assert settings.port == 7001
    assert settings.password == "secret"


def test_image_library_lists_only_supported_images(tmp_path: Path):
    module = _load_module()
    (tmp_path / "20260510_120000_a.png").write_bytes(b"png")
    (tmp_path / "20260510_120001_b.webp").write_bytes(b"webp")
    (tmp_path / "20260510_120001_b.webp.json").write_text(
        '{"prompt":"生成一张湖边小屋","size":"1024x1024","mode":"generate"}',
        encoding="utf-8",
    )
    (tmp_path / "note.txt").write_text("skip", encoding="utf-8")

    library = module.ImageLibrary(tmp_path)

    images = library.list_images()

    assert [item["name"] for item in images] == [
        "20260510_120001_b.webp",
        "20260510_120000_a.png",
    ]
    assert images[0]["mime_type"] == "image/webp"
    assert images[0]["url"] == "/api/images/20260510_120001_b.webp"
    assert images[0]["prompt"] == "生成一张湖边小屋"
    assert images[0]["generation_size"] == "1024x1024"
    assert images[0]["mode"] == "generate"


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


def test_image_library_resolves_existing_image(tmp_path: Path):
    module = _load_module()
    image_path = tmp_path / "demo.png"
    image_path.write_bytes(b"demo")
    library = module.ImageLibrary(tmp_path)

    resolved_path = library.resolve_image_path("demo.png")

    assert resolved_path == image_path


def test_image_library_finds_metadata_by_name(tmp_path: Path):
    module = _load_module()
    (tmp_path / "demo.png").write_bytes(b"demo")
    library = module.ImageLibrary(tmp_path)

    image = library.get_image_by_name("demo.png")

    assert image["name"] == "demo.png"
    assert image["url"] == "/api/images/demo.png"
    assert image["prompt"] == ""
    assert image["generation_size"] == ""


def test_image_library_deletes_image_and_sidecar_metadata(tmp_path: Path):
    module = _load_module()
    image_path = tmp_path / "demo.png"
    metadata_path = tmp_path / "demo.png.json"
    image_path.write_bytes(b"demo")
    metadata_path.write_text('{"prompt":"删除测试"}', encoding="utf-8")
    library = module.ImageLibrary(tmp_path)

    removed_name = library.delete_image_by_name("demo.png")

    assert removed_name == "demo.png"
    assert not image_path.exists()
    assert not metadata_path.exists()


def test_image_library_rejects_delete_path_traversal(tmp_path: Path):
    module = _load_module()
    library = module.ImageLibrary(tmp_path)

    with pytest.raises(FileNotFoundError):
        library.delete_image_by_name("../secret.png")


def test_server_accepts_bearer_or_cookie_token(tmp_path: Path):
    module = _load_module()
    server = module.WebAdminServer(
        plugin=SimpleNamespace(),
        settings=module.WebAdminSettings(
            enabled=True,
            host="127.0.0.1",
            port=7865,
            password="secret",
        ),
        cache_dir=tmp_path,
    )
    server._tokens.add("token-1")

    assert server._is_authorized("Bearer token-1", {}) is True
    assert server._is_authorized("", {"openai_image_admin_token": "token-1"}) is True
    assert server._is_authorized("Bearer token-2", {}) is False


def test_web_admin_logs_request_with_safe_prompt_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module = _load_module()
    info_calls: list[tuple[str, tuple[object, ...]]] = []

    class FakeLogger:
        def info(self, message: str, *args: object) -> None:
            info_calls.append((message, args))

    monkeypatch.setattr(module, "logger", FakeLogger())
    server = module.WebAdminServer(
        plugin=SimpleNamespace(
            _get_endpoint_type=lambda: "images",
            _get_configured_model=lambda: "gpt-image-test",
        ),
        settings=module.WebAdminSettings(
            enabled=True,
            host="127.0.0.1",
            port=7865,
            password="secret",
        ),
        cache_dir=tmp_path,
    )

    server._log_web_request(
        mode="generate",
        prompt=f"第一行\n{'猫' * 160}",
        size="1024x1536",
        quality="high",
        moderation="low",
    )

    assert info_calls
    message, args = info_calls[0]
    assert "[OpenAIImage][web][%s] 收到请求" in message
    assert args[0] == "generate"
    assert "\n" not in str(args[1])
    assert str(args[1]).endswith("...")
    assert len(str(args[1])) <= module.LOG_PROMPT_MAX_LENGTH + 3
    assert args[-2:] == ("images", "gpt-image-test")


def test_web_admin_logs_task_success_and_failure(
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
    server = module.WebAdminServer(
        plugin=SimpleNamespace(),
        settings=module.WebAdminSettings(
            enabled=True,
            host="127.0.0.1",
            port=7865,
            password="secret",
        ),
        cache_dir=tmp_path,
    )

    server._log_task_result(
        {
            "success": True,
            "mode": "web_generate",
            "payload": tmp_path / "result.png",
            "timings": {"elapsed_ms": 1200, "queue_wait_ms": 30},
        }
    )
    server._log_task_result(
        {
            "success": False,
            "mode": "web_edit",
            "error_stage": "web_edit",
            "error_message": "provider failed",
            "timings": {"elapsed_ms": 90, "queue_wait_ms": 5},
        }
    )

    assert info_calls[0][0] == (
        "[OpenAIImage][web][%s] 任务完成 output=%s elapsed_ms=%s queue_wait_ms=%s"
    )
    assert info_calls[0][1] == ("web_generate", "result.png", 1200, 30)
    assert warning_calls[0][0] == (
        "[OpenAIImage][web][%s] 任务失败 stage=%s error=%s elapsed_ms=%s queue_wait_ms=%s"
    )
    assert warning_calls[0][1] == ("web_edit", "web_edit", "provider failed", 90, 5)


@pytest.mark.asyncio
async def test_image_handler_accepts_http_only_cookie_token(tmp_path: Path):
    module = _load_module()
    (tmp_path / "demo.png").write_bytes(b"demo")
    server = module.WebAdminServer(
        plugin=SimpleNamespace(),
        settings=module.WebAdminSettings(
            enabled=True,
            host="127.0.0.1",
            port=7865,
            password="secret",
        ),
        cache_dir=tmp_path,
    )
    server._tokens.add("token-1")
    app = server._create_app()
    runner = module.web.AppRunner(app)
    await runner.setup()
    site = module.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        import aiohttp

        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True)
        ) as session:
            session.cookie_jar.update_cookies(
                {"openai_image_admin_token": "token-1"},
                response_url=URL(f"http://127.0.0.1:{port}/"),
            )
            async with session.get(
                f"http://127.0.0.1:{port}/api/images/demo.png"
            ) as response:
                assert response.status == 200
                assert await response.read() == b"demo"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_delete_image_handler_removes_server_file_and_metadata(tmp_path: Path):
    module = _load_module()
    (tmp_path / "demo.png").write_bytes(b"demo")
    (tmp_path / "demo.png.json").write_text('{"prompt":"删除测试"}', encoding="utf-8")
    server = module.WebAdminServer(
        plugin=SimpleNamespace(),
        settings=module.WebAdminSettings(
            enabled=True,
            host="127.0.0.1",
            port=7865,
            password="secret",
        ),
        cache_dir=tmp_path,
    )
    server._tokens.add("token-1")
    app = server._create_app()
    runner = module.web.AppRunner(app)
    await runner.setup()
    site = module.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.delete(
                f"http://127.0.0.1:{port}/api/images/demo.png",
                headers={"Authorization": "Bearer token-1"},
            ) as response:
                assert response.status == 200
                assert await response.json() == {"deleted": "demo.png"}
    finally:
        await runner.cleanup()

    assert not (tmp_path / "demo.png").exists()
    assert not (tmp_path / "demo.png.json").exists()


@pytest.mark.asyncio
async def test_edit_handler_accepts_multiple_reference_images(tmp_path: Path):
    module = _load_module()
    output_path = tmp_path / "edited.png"
    output_path.write_bytes(b"edited")

    class FakeEditService:
        async def edit(self, **kwargs):
            self.kwargs = kwargs
            return output_path

    class FakeTaskService:
        async def run_task(self, *, mode, job_coro, stage_name):
            result_path = await job_coro()
            return {
                "success": True,
                "mode": mode,
                "payload": result_path,
                "timings": {},
            }

    plugin = SimpleNamespace(
        _edit_service=FakeEditService(),
        _task_service=FakeTaskService(),
        _ensure_ready=lambda: None,
        _get_configured_model=lambda: "gpt-image-test",
        _get_negative_prompt=lambda: "",
        _get_endpoint_type=lambda: "responses",
        _resolve_output_size=lambda size: size,
    )
    server = module.WebAdminServer(
        plugin=plugin,
        settings=module.WebAdminSettings(
            enabled=True,
            host="127.0.0.1",
            port=7865,
            password="secret",
        ),
        cache_dir=tmp_path,
    )
    server._tokens.add("token-1")
    app = server._create_app()
    runner = module.web.AppRunner(app)
    await runner.setup()
    site = module.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        import aiohttp

        form = aiohttp.FormData()
        form.add_field("prompt", "按两张参考图重绘")
        form.add_field("size", "auto")
        form.add_field("quality", "high")
        form.add_field("moderation", "low")
        form.add_field(
            "image",
            b"first",
            filename="first.png",
            content_type="image/png",
        )
        form.add_field(
            "image",
            b"second",
            filename="second.webp",
            content_type="image/webp",
        )

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}/api/edit",
                data=form,
                headers={"Authorization": "Bearer token-1"},
            ) as response:
                assert response.status == 200
                data = await response.json()
                assert data["image"]["name"] == "edited.png"
    finally:
        await runner.cleanup()

    assert plugin._edit_service.kwargs["data_urls"] == [
        "data:image/png;base64,Zmlyc3Q=",
        "data:image/webp;base64,c2Vjb25k",
    ]


def test_admin_html_escapes_image_names_and_avoids_url_tokens():
    module = _load_module()

    assert "function escapeHtml" in module.ADMIN_HTML
    assert "function escapeAttribute" in module.ADMIN_HTML
    assert "?token=" not in module.ADMIN_HTML


def test_admin_html_uses_separate_generate_edit_pages_and_size_presets():
    module = _load_module()

    assert "id=\"generatePanel\"" in module.ADMIN_HTML
    assert "id=\"editPanel\"" in module.ADMIN_HTML
    assert "id=\"generateForm\"" in module.ADMIN_HTML
    assert "id=\"editForm\"" in module.ADMIN_HTML
    assert "id=\"generateSizePreset\"" in module.ADMIN_HTML
    assert "id=\"generateCustomSize\"" in module.ADMIN_HTML
    assert "id=\"editSizePreset\"" in module.ADMIN_HTML
    assert "id=\"editCustomSize\"" in module.ADMIN_HTML
    assert "value=\"custom\"" in module.ADMIN_HTML
    assert "function resolveSizeValue" in module.ADMIN_HTML
    assert "id=\"taskPanel\"" not in module.ADMIN_HTML
    assert "id=\"taskForm\"" not in module.ADMIN_HTML
    assert "data-scroll-target" not in module.ADMIN_HTML
    assert "function showPanel" in module.ADMIN_HTML
    assert "data-panel=\"generatePanel\"" in module.ADMIN_HTML
    assert "data-panel=\"editPanel\"" in module.ADMIN_HTML


def test_admin_html_preview_actions_match_requested_gallery_flow():
    module = _load_module()

    assert "id=\"copyImageBtn\"" in module.ADMIN_HTML
    assert "addEventListener(\"dblclick\"" in module.ADMIN_HTML
    assert "copySelectedImage" in module.ADMIN_HTML
    assert "id=\"deleteImageBtn\"" in module.ADMIN_HTML
    assert "deleteSelectedImage" in module.ADMIN_HTML
    assert 'method: "DELETE"' in module.ADMIN_HTML
    assert "clearImageCacheRecords" in module.ADMIN_HTML
    assert "再次生成" not in module.ADMIN_HTML
    assert "用于编辑" not in module.ADMIN_HTML


def test_admin_html_gallery_uses_fluid_grid_without_cropping_images():
    module = _load_module()
    gallery_rule = re.search(r"\.gallery\s*\{(?P<body>[^}]+)\}", module.ADMIN_HTML)
    thumb_rule = re.search(r"\.thumb\s*\{(?P<body>[^}]+)\}", module.ADMIN_HTML)

    assert gallery_rule is not None
    assert thumb_rule is not None
    gallery_body = gallery_rule.group("body")
    thumb_body = thumb_rule.group("body")
    assert "column-width:" in gallery_body
    assert "column-gap:" in gallery_body
    assert "display: grid;" not in gallery_body
    assert "gallery-empty" in module.ADMIN_HTML
    assert "grid-column:1/-1" not in module.ADMIN_HTML
    assert "height: auto;" in thumb_body
    assert "object-fit: contain;" in thumb_body
    assert "object-fit: cover;" not in thumb_body
    assert "aspect-ratio:" not in thumb_body


def test_admin_html_selects_gallery_image_without_rerendering_gallery():
    module = _load_module()
    select_image_section = module.ADMIN_HTML.split("async function selectImage", 1)[1].split(
        "function renderResultImages", 1
    )[0]

    assert "function updateGallerySelection" in module.ADMIN_HTML
    assert "updateGallerySelection();" in select_image_section
    assert "renderGallery();" not in select_image_section


def test_admin_html_supports_paste_reference_image_preview():
    module = _load_module()

    assert "referenceThumbs" in module.ADMIN_HTML
    assert "handlePasteImage" in module.ADMIN_HTML
    assert "clipboardData.items" in module.ADMIN_HTML
    assert "referenceImageFiles" in module.ADMIN_HTML


def test_admin_html_places_workspace_result_above_action_panel():
    module = _load_module()

    assert "class=\"workflow-layout\"" in module.ADMIN_HTML
    assert "class=\"workspace-result-panel\"" in module.ADMIN_HTML
    assert "class=\"action-panel\"" in module.ADMIN_HTML
    assert 'id="generateForm" class="action-panel action-panel-single"' in module.ADMIN_HTML
    assert "id=\"generateResultBox\"" in module.ADMIN_HTML
    assert "id=\"editResultBox\"" in module.ADMIN_HTML
    assert "生成的图片会显示在这里" in module.ADMIN_HTML


def test_admin_html_supports_multiple_reference_thumbnails_on_edit_page():
    module = _load_module()

    assert "multiple" in module.ADMIN_HTML
    assert "id=\"referenceThumbs\"" in module.ADMIN_HTML
    assert "class=\"reference-thumbs\"" in module.ADMIN_HTML
    assert "renderReferenceThumbnails" in module.ADMIN_HTML
    assert "body.append(\"image\", file);" in module.ADMIN_HTML


def test_admin_html_uses_browser_local_image_cache_and_settings():
    module = _load_module()

    assert "indexedDB" in module.ADMIN_HTML
    assert "function openImageCache" in module.ADMIN_HTML
    assert "function getCachedThumbnailUrl" in module.ADMIN_HTML
    assert "function getCachedOriginalUrl" in module.ADMIN_HTML
    assert "function refreshCacheInfo" in module.ADMIN_HTML
    assert "id=\"localCacheDirectory\"" not in module.ADMIN_HTML
    assert "缓存文件夹" not in module.ADMIN_HTML
    assert "浏览器安全限制" not in module.ADMIN_HTML
    assert "完整本机路径" not in module.ADMIN_HTML
    assert "缓存目录地址" not in module.ADMIN_HTML
    assert "type=\"text\" readonly" not in module.ADMIN_HTML
    assert "id=\"selectCacheDirectoryBtn\"" not in module.ADMIN_HTML
    assert "showDirectoryPicker" not in module.ADMIN_HTML
    assert "function chooseCacheDirectory" not in module.ADMIN_HTML
    assert "openaiImageCacheDirectory" not in module.ADMIN_HTML
    assert "localCacheDirectoryHandle" not in module.ADMIN_HTML
    assert "id=\"cacheDirectoryPicker\"" not in module.ADMIN_HTML
    assert "webkitdirectory" not in module.ADMIN_HTML
    assert "handleFallbackDirectoryPick" not in module.ADMIN_HTML
    assert "不支持选择可写文件夹" not in module.ADMIN_HTML
    assert "id=\"cacheInfo\"" in module.ADMIN_HTML
    assert "id=\"clearLocalCacheBtn\"" in module.ADMIN_HTML
    assert "保存缓存设置" not in module.ADMIN_HTML
    assert "默认端口" not in module.ADMIN_HTML
    assert "默认监听" not in module.ADMIN_HTML
    assert "远程访问" not in module.ADMIN_HTML


def test_admin_html_keeps_sidebars_fixed_on_desktop_scroll():
    module = _load_module()

    assert ".sidebar,\n    .preview {" in module.ADMIN_HTML
    assert "position: sticky;" in module.ADMIN_HTML
    assert "height: 100vh;" in module.ADMIN_HTML
    assert "overflow-y: auto;" in module.ADMIN_HTML
    assert ".app-shell { display: block; }" in module.ADMIN_HTML


def test_admin_html_hides_preview_sidebar_on_settings_page():
    module = _load_module()

    assert "function updatePreviewVisibility" in module.ADMIN_HTML
    assert 'document.body.classList.toggle("settings-active", panelId === "settingsPanel");' in module.ADMIN_HTML
    assert "body.settings-active .preview" in module.ADMIN_HTML
    assert "body.settings-active .app-shell" in module.ADMIN_HTML


def test_admin_html_centers_preview_image_and_shows_prompt_size_metadata():
    module = _load_module()
    preview_image_rule = re.search(r"\.preview-box img\s*\{(?P<body>[^}]+)\}", module.ADMIN_HTML)

    assert "display: grid;" in module.ADMIN_HTML
    assert "place-items: center;" in module.ADMIN_HTML
    assert preview_image_rule is not None
    rule_body = preview_image_rule.group("body")
    assert "width: 100%;" in rule_body
    assert "height: 100%;" in rule_body
    assert "object-fit: contain;" in rule_body
    assert "max-width: 100%;" not in rule_body
    assert "max-height: 100%;" not in rule_body
    assert "id=\"detailPrompt\"" in module.ADMIN_HTML
    assert "id=\"detailGenerationSize\"" in module.ADMIN_HTML
    assert "formatGenerationSize(image)" in module.ADMIN_HTML
    assert "formatPrompt(image.prompt)" in module.ADMIN_HTML


def test_admin_html_preview_sidebar_disables_horizontal_scrolling():
    module = _load_module()

    assert "overflow-x: hidden;" in module.ADMIN_HTML
    assert "min-width: 0;" in module.ADMIN_HTML
    assert "overflow-wrap: anywhere;" in module.ADMIN_HTML
    assert ".preview .settings-actions" in module.ADMIN_HTML
    assert ".preview { position: static; height: auto; overflow-x: hidden; overflow-y: visible; }" in module.ADMIN_HTML


def test_admin_html_caches_thumbnails_before_original_images():
    module = _load_module()

    assert "function thumbnailCacheKey(image)" in module.ADMIN_HTML
    assert "function originalCacheKey(image)" in module.ADMIN_HTML
    assert "kind: \"thumbnail\"" in module.ADMIN_HTML
    assert "kind: \"original\"" in module.ADMIN_HTML
    assert "thumb.src = await getCachedThumbnailUrl(image);" in module.ADMIN_HTML
    assert "const originalUrl = await getCachedOriginalUrl(state.selected);" in module.ADMIN_HTML
    assert "previewBox\").innerHTML = `<img src=\"${escapeAttribute(imageUrl(image))}\"" in module.ADMIN_HTML
    assert "const cached = await readCachedImage(originalCacheKey(state.selected));" not in module.ADMIN_HTML
    assert "key: originalCacheKey(state.selected)" not in module.ADMIN_HTML
    assert "const cachedUrl = await getCachedOriginalUrl(image);" not in module.ADMIN_HTML
    assert "thumb.src = await getCachedImageUrl(image);" not in module.ADMIN_HTML


def test_admin_html_does_not_auto_select_first_history_image():
    module = _load_module()

    assert "function clearPreview" in module.ADMIN_HTML
    assert "selectImage(state.images[0].name)" not in module.ADMIN_HTML
    assert 'if (preferredName) {' in module.ADMIN_HTML


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
        _get_negative_prompt=lambda: "低清晰度",
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
    result_path = await job()

    assert result_path == output_path
    assert plugin._generate_service.kwargs == {
        "model": "gpt-image-test",
        "prompt": "生成一张湖边小屋",
        "negative_prompt": "低清晰度",
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
        _get_negative_prompt=lambda: "低清晰度",
        _get_endpoint_type=lambda: "responses",
        _resolve_output_size=lambda size: size,
    )

    job = module.create_edit_job(
        plugin=plugin,
        prompt="改成水彩风格",
        data_urls=[
            "data:image/png;base64,aGVsbG8=",
            "data:image/png;base64,d29ybGQ=",
        ],
        size="auto",
        quality="medium",
        moderation="low",
    )
    result_path = await job()

    assert result_path == output_path
    assert plugin._edit_service.kwargs == {
        "model": "gpt-image-test",
        "prompt": "改成水彩风格",
        "data_urls": [
            "data:image/png;base64,aGVsbG8=",
            "data:image/png;base64,d29ybGQ=",
        ],
        "negative_prompt": "低清晰度",
        "endpoint_type": "responses",
        "size": "auto",
        "quality": "medium",
        "moderation": "low",
    }


@pytest.mark.asyncio
async def test_multipart_image_to_data_url_reads_uploaded_image():
    module = _load_module()

    class FakePart:
        headers = {"Content-Type": "image/png"}

        async def read(self):
            return b"hello"

    data_url = await module._multipart_image_to_data_url(FakePart())

    assert data_url == "data:image/png;base64,aGVsbG8="
