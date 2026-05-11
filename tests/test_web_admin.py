from __future__ import annotations

import importlib
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
    (tmp_path / "note.txt").write_text("skip", encoding="utf-8")

    library = module.ImageLibrary(tmp_path)

    images = library.list_images()

    assert [item["name"] for item in images] == [
        "20260510_120001_b.webp",
        "20260510_120000_a.png",
    ]
    assert images[0]["mime_type"] == "image/webp"
    assert images[0]["url"] == "/api/images/20260510_120001_b.webp"


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
    assert "再次生成" not in module.ADMIN_HTML
    assert "用于编辑" not in module.ADMIN_HTML


def test_admin_html_supports_paste_reference_image_preview():
    module = _load_module()

    assert "pasteImagePreview" in module.ADMIN_HTML
    assert "handlePasteImage" in module.ADMIN_HTML
    assert "clipboardData.items" in module.ADMIN_HTML
    assert "referenceImageFile" in module.ADMIN_HTML


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
        data_url="data:image/png;base64,aGVsbG8=",
        size="auto",
        quality="medium",
        moderation="low",
    )
    result_path = await job()

    assert result_path == output_path
    assert plugin._edit_service.kwargs == {
        "model": "gpt-image-test",
        "prompt": "改成水彩风格",
        "data_url": "data:image/png;base64,aGVsbG8=",
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
