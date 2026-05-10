"""网页后台管理服务。"""

from __future__ import annotations

import base64
import mimetypes
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web

from astrbot.api import logger

from .storage.cache_cleaner import IMAGE_SUFFIXES

DEFAULT_ADMIN_HOST = "127.0.0.1"
DEFAULT_ADMIN_PORT = 7865
TOKEN_BYTES = 24
AUTH_COOKIE_NAME = "openai_image_admin_token"
IMAGE_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


@dataclass(slots=True)
class WebAdminSettings:
    """网页后台启动配置。

    enabled 会同时受开关和密码影响：密码为空时强制关闭，避免后台在无鉴权状态下暴露。
    """

    enabled: bool
    host: str
    port: int
    password: str
    requested_enabled: bool = False

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> WebAdminSettings:
        """从插件配置读取后台参数，并对端口和密码做最小安全归一化。"""

        password = str(config.get("web_admin_password", "") or "").strip()
        requested_enabled = bool(config.get("web_admin_enabled", False))
        enabled = requested_enabled and bool(password)
        return cls(
            enabled=enabled,
            host=str(config.get("web_admin_host", DEFAULT_ADMIN_HOST) or "").strip()
            or DEFAULT_ADMIN_HOST,
            port=_normalize_port(config.get("web_admin_port", DEFAULT_ADMIN_PORT)),
            password=password,
            requested_enabled=requested_enabled,
        )


class ImageLibrary:
    """读取插件缓存图库，并限制文件访问只发生在缓存目录内。"""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def list_images(self) -> list[dict[str, Any]]:
        """按更新时间倒序返回可在后台展示的图片元数据。"""

        image_items: list[dict[str, Any]] = []
        for image_path in self.cache_dir.iterdir():
            if not self._is_supported_image(image_path):
                continue

            try:
                image_items.append(self._build_image_metadata(image_path))
            except FileNotFoundError:
                # 图片缓存会在生成新图后自动清理旧文件；跳过被并发删除的条目，避免图库偶发 500。
                continue

        return sorted(
            image_items,
            key=lambda item: (int(item["modified_at"]), str(item["name"])),
            reverse=True,
        )

    def get_image_by_name(self, file_name: str) -> dict[str, Any]:
        """按文件名返回单张图片元数据，供生成/编辑完成后刷新选中态。"""

        return self._build_image_metadata(self.resolve_image_path(file_name))

    def resolve_image_path(self, file_name: str) -> Path:
        """解析图片文件名，拒绝目录穿越和非图片后缀。"""

        clean_name = Path(str(file_name or "")).name
        if clean_name != str(file_name or ""):
            raise FileNotFoundError("图片文件不存在")

        image_path = (self.cache_dir / clean_name).resolve(strict=False)
        cache_root = self.cache_dir.resolve(strict=False)
        if cache_root != image_path.parent:
            raise FileNotFoundError("图片文件不存在")
        if not self._is_supported_image(image_path):
            raise FileNotFoundError("图片文件不存在")
        return image_path

    @staticmethod
    def _is_supported_image(path: Path) -> bool:
        """判断路径是否为后台允许展示的图片文件。"""

        return path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES

    @staticmethod
    def _build_image_metadata(image_path: Path) -> dict[str, Any]:
        """将缓存图片路径转换为前端需要的稳定字段。"""

        stat_result = image_path.stat()
        return {
            "name": image_path.name,
            "url": f"/api/images/{image_path.name}",
            "mime_type": _guess_mime_type(image_path),
            "size_bytes": stat_result.st_size,
            "modified_at": int(stat_result.st_mtime),
        }


def create_generation_job(
    *,
    plugin: Any,
    prompt: str,
    size: str | None,
    quality: str,
    moderation: str,
) -> Callable[[], Awaitable[Path]]:
    """创建文生图任务闭包，供网页 API 与任务调度服务组合。"""

    async def _job() -> Path:
        return await plugin._generate_service.generate(
            model=plugin._get_configured_model(),
            prompt=prompt,
            negative_prompt=plugin._get_negative_prompt(),
            endpoint_type=plugin._get_endpoint_type(),
            size=plugin._resolve_output_size(size),
            quality=quality,
            moderation=moderation,
        )

    return _job


def create_edit_job(
    *,
    plugin: Any,
    prompt: str,
    data_url: str,
    size: str | None,
    quality: str,
    moderation: str,
) -> Callable[[], Awaitable[Path]]:
    """创建图片编辑任务闭包，统一复用插件既有编辑服务。"""

    async def _job() -> Path:
        return await plugin._edit_service.edit(
            model=plugin._get_configured_model(),
            prompt=prompt,
            data_url=data_url,
            negative_prompt=plugin._get_negative_prompt(),
            endpoint_type=plugin._get_endpoint_type(),
            size=plugin._resolve_output_size(size),
            quality=quality,
            moderation=moderation,
        )

    return _job


class WebAdminServer:
    """插件内置的 aiohttp 网页后台。"""

    def __init__(
        self, plugin: Any, settings: WebAdminSettings, cache_dir: Path
    ) -> None:
        self.plugin = plugin
        self.settings = settings
        self.library = ImageLibrary(cache_dir)
        self._tokens: set[str] = set()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self) -> None:
        """启动后台 HTTP 服务。"""

        if not self.settings.enabled:
            if self.settings.requested_enabled:
                logger.warning(
                    "[OpenAIImage][web] 后台未启动：请先设置登录密码，避免无密码暴露"
                )
            return

        app = self._create_app()
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner,
            host=self.settings.host,
            port=self.settings.port,
        )
        await self._site.start()
        logger.info(
            "[OpenAIImage][web] 后台已启动 url=http://%s:%s",
            self.settings.host,
            self.settings.port,
        )

    async def stop(self) -> None:
        """停止后台 HTTP 服务并释放端口。"""

        if self._runner is not None:
            await self._runner.cleanup()
        self._site = None
        self._runner = None
        self._tokens.clear()

    def _create_app(self) -> web.Application:
        """创建 aiohttp 应用并注册静态页面与 JSON API。"""

        app = web.Application(client_max_size=16 * 1024 * 1024)
        app.router.add_get("/", self._handle_index)
        app.router.add_post("/api/login", self._handle_login)
        app.router.add_get("/api/images", self._handle_list_images)
        app.router.add_get("/api/images/{file_name}", self._handle_get_image)
        app.router.add_post("/api/generate", self._handle_generate)
        app.router.add_post("/api/edit", self._handle_edit)
        return app

    async def _handle_index(self, _request: web.Request) -> web.Response:
        """返回单页后台界面。"""

        return web.Response(text=ADMIN_HTML, content_type="text/html")

    async def _handle_login(self, request: web.Request) -> web.Response:
        """校验登录密码，成功后签发内存态访问令牌。"""

        payload = await request.json()
        password = str(payload.get("password", "") or "")
        if not secrets.compare_digest(password, self.settings.password):
            return web.json_response({"error": "密码错误"}, status=401)

        token = secrets.token_urlsafe(TOKEN_BYTES)
        self._tokens.add(token)
        response = web.json_response({"token": token})
        response.set_cookie(
            AUTH_COOKIE_NAME,
            token,
            httponly=True,
            samesite="Strict",
            path="/",
        )
        return response

    async def _handle_list_images(self, request: web.Request) -> web.Response:
        """返回历史图库列表。"""

        auth_response = self._require_auth(request)
        if auth_response is not None:
            return auth_response
        return web.json_response({"images": self.library.list_images()})

    async def _handle_get_image(self, request: web.Request) -> web.StreamResponse:
        """读取缓存图片文件，供缩略图和大图预览共同使用。"""

        auth_response = self._require_auth(request, allow_cookie_token=True)
        if auth_response is not None:
            return auth_response

        try:
            image_path = self.library.resolve_image_path(
                request.match_info.get("file_name", "")
            )
        except FileNotFoundError:
            return web.json_response({"error": "图片不存在"}, status=404)

        return web.FileResponse(image_path, headers={"Cache-Control": "no-store"})

    async def _handle_generate(self, request: web.Request) -> web.Response:
        """处理网页文生图请求。"""

        auth_response = self._require_auth(request)
        if auth_response is not None:
            return auth_response

        payload = await request.json()
        prompt = str(payload.get("prompt", "") or "").strip()
        if not prompt:
            return web.json_response({"error": "提示词不能为空"}, status=400)

        self.plugin._ensure_ready()
        task_result = await self.plugin._task_service.run_task(
            mode="web_generate",
            job_coro=create_generation_job(
                plugin=self.plugin,
                prompt=prompt,
                size=_optional_text(payload.get("size")),
                quality=_option_text(payload.get("quality"), "auto"),
                moderation=_option_text(payload.get("moderation"), "low"),
            ),
            stage_name="web_generate",
        )
        return self._task_response(task_result)

    async def _handle_edit(self, request: web.Request) -> web.Response:
        """处理网页图片编辑请求。"""

        auth_response = self._require_auth(request)
        if auth_response is not None:
            return auth_response

        reader = await request.multipart()
        fields: dict[str, str] = {}
        image_data_url = ""
        async for part in reader:
            if part.name == "image":
                image_data_url = await _multipart_image_to_data_url(part)
            else:
                fields[str(part.name)] = (await part.text()).strip()

        prompt = fields.get("prompt", "").strip()
        if not prompt:
            return web.json_response({"error": "提示词不能为空"}, status=400)
        if not image_data_url:
            return web.json_response({"error": "请上传待编辑图片"}, status=400)

        self.plugin._ensure_ready()
        task_result = await self.plugin._task_service.run_task(
            mode="web_edit",
            job_coro=create_edit_job(
                plugin=self.plugin,
                prompt=prompt,
                data_url=image_data_url,
                size=_optional_text(fields.get("size")),
                quality=_option_text(fields.get("quality"), "auto"),
                moderation=_option_text(fields.get("moderation"), "low"),
            ),
            stage_name="web_edit",
        )
        return self._task_response(task_result)

    def _require_auth(
        self,
        request: web.Request,
        *,
        allow_cookie_token: bool = False,
    ) -> web.Response | None:
        """校验 Bearer Token，失败时返回 401 响应。"""

        auth_header = str(request.headers.get("Authorization", "") or "")
        cookie_tokens = dict(request.cookies) if allow_cookie_token else {}
        if not self._is_authorized(auth_header, cookie_tokens):
            return web.json_response({"error": "请先登录"}, status=401)
        return None

    def _is_authorized(self, auth_header: str, cookie_tokens: dict[str, str]) -> bool:
        """判断请求是否携带有效令牌。

        普通 API 使用 Authorization 头；图片预览由 img 标签加载，无法附带自定义头，
        因此图片接口额外接受 HttpOnly Cookie，避免把令牌暴露到 URL。
        """

        scheme, _, bearer_token = str(auth_header or "").partition(" ")
        if scheme.lower() == "bearer" and bearer_token in self._tokens:
            return True
        cookie_token = str(cookie_tokens.get(AUTH_COOKIE_NAME, "") or "")
        return bool(cookie_token and cookie_token in self._tokens)

    def _task_response(self, task_result: dict[str, Any]) -> web.Response:
        """将任务服务的统一结果转换为网页 API 响应。"""

        if not task_result.get("success"):
            return web.json_response(
                {
                    "error": str(task_result.get("error_message") or "图片任务失败"),
                    "timings": task_result.get("timings", {}),
                },
                status=500,
            )

        payload_path = Path(task_result["payload"])
        return web.json_response(
            {
                "image": self.library.get_image_by_name(payload_path.name),
                "timings": task_result.get("timings", {}),
            }
        )


def _normalize_port(value: Any) -> int:
    """归一化端口号，越界或非法时回退到默认端口。"""

    try:
        port = int(value)
    except (TypeError, ValueError):
        return DEFAULT_ADMIN_PORT
    if 1 <= port <= 65535:
        return port
    return DEFAULT_ADMIN_PORT


def _guess_mime_type(path: Path) -> str:
    """根据文件名推断图片 MIME 类型。"""

    return (
        IMAGE_MIME_TYPES.get(path.suffix.lower())
        or mimetypes.guess_type(path.name)[0]
        or "application/octet-stream"
    )


def _optional_text(value: Any) -> str | None:
    """读取可选字符串字段，空白值统一视为未配置。"""

    clean_value = str(value or "").strip()
    return clean_value or None


def _option_text(value: Any, default: str) -> str:
    """读取下拉选项字段，空白时回退默认值。"""

    return str(value or default).strip().lower() or default


async def _multipart_image_to_data_url(part: Any) -> str:
    """将网页上传图片转为编辑服务需要的 data URL。"""

    image_bytes = await part.read()
    if not image_bytes:
        return ""
    mime_type = str(part.headers.get("Content-Type", "") or "").strip() or "image/png"
    base64_data = base64.b64encode(image_bytes).decode("utf-8")
    return f"data:{mime_type};base64,{base64_data}"


ADMIN_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OpenAI 图片后台</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f9ff;
      --panel: #ffffff;
      --panel-soft: #f8fbff;
      --line: #dfe7f3;
      --line-strong: #cbd8ea;
      --text: #172033;
      --muted: #6f7d94;
      --primary: #3d73f6;
      --primary-soft: #eaf1ff;
      --success: #2ca66f;
      --danger: #c24141;
      --shadow: 0 18px 45px rgba(48, 76, 126, 0.11);
      font-family: Inter, "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: linear-gradient(135deg, #f7fbff 0%, #eef5ff 46%, #fbfdff 100%);
      color: var(--text);
    }
    button, input, textarea, select {
      font: inherit;
    }
    button {
      border: 0;
      cursor: pointer;
    }
    .app-shell {
      display: grid;
      grid-template-columns: 240px minmax(420px, 1fr) 360px;
      min-height: 100vh;
    }
    .sidebar {
      padding: 24px 18px;
      background: rgba(248, 251, 255, 0.88);
      border-right: 1px solid var(--line);
      backdrop-filter: blur(16px);
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 34px;
    }
    .brand-mark {
      display: grid;
      place-items: center;
      width: 42px;
      height: 42px;
      border-radius: 14px;
      background: linear-gradient(135deg, #4a7dff, #76b7ff);
      color: #fff;
      box-shadow: 0 14px 30px rgba(61, 115, 246, 0.28);
    }
    .brand-title { font-weight: 800; letter-spacing: 0; }
    .brand-subtitle { margin-top: 2px; color: var(--muted); font-size: 12px; }
    .nav-group { margin-top: 22px; }
    .nav-label {
      margin: 0 10px 10px;
      color: #8795aa;
      font-size: 12px;
      font-weight: 700;
    }
    .nav-item {
      display: flex;
      align-items: center;
      gap: 12px;
      width: 100%;
      min-height: 46px;
      padding: 0 14px;
      border-radius: 10px;
      color: #344058;
      background: transparent;
      text-align: left;
      transition: background 160ms ease, color 160ms ease;
    }
    .nav-item.active,
    .nav-item:hover {
      background: var(--primary-soft);
      color: var(--primary);
    }
    .nav-icon {
      width: 20px;
      height: 20px;
      stroke: currentColor;
      stroke-width: 2;
      fill: none;
      stroke-linecap: round;
      stroke-linejoin: round;
    }
    .usage-card {
      margin-top: auto;
      position: fixed;
      left: 20px;
      bottom: 22px;
      width: 198px;
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.78);
      box-shadow: var(--shadow);
    }
    .usage-card strong { display: block; margin-bottom: 8px; }
    .usage-card span { color: var(--muted); font-size: 13px; }
    .main {
      padding: 22px 24px 26px;
      border-right: 1px solid var(--line);
    }
    .topbar {
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 26px;
    }
    .search {
      position: relative;
      flex: 1;
      max-width: 560px;
    }
    .search input {
      width: 100%;
      height: 46px;
      padding: 0 46px;
      border: 1px solid var(--line);
      border-radius: 11px;
      background: rgba(255, 255, 255, 0.82);
      color: var(--text);
      outline: none;
      box-shadow: 0 8px 24px rgba(48, 76, 126, 0.06);
    }
    .search svg {
      position: absolute;
      left: 16px;
      top: 13px;
      width: 20px;
      height: 20px;
      color: var(--muted);
    }
    .status-pill {
      display: flex;
      align-items: center;
      gap: 8px;
      min-height: 38px;
      padding: 0 14px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: rgba(255,255,255,0.8);
      color: #4b5870;
      font-size: 13px;
      white-space: nowrap;
    }
    .status-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--success);
    }
    .section-head {
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 16px;
    }
    h1, h2, h3, p { margin-top: 0; }
    h1 { margin-bottom: 6px; font-size: 26px; line-height: 1.2; }
    .muted { color: var(--muted); }
    .filters {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 18px;
    }
    .control {
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #fff;
      color: #263247;
      padding: 0 12px;
      outline: none;
    }
    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      min-height: 40px;
      padding: 0 14px;
      border-radius: 10px;
      background: #eef3fb;
      color: #25324a;
      transition: transform 160ms ease, background 160ms ease, box-shadow 160ms ease;
    }
    .btn:hover { transform: translateY(-1px); }
    .btn.primary {
      background: var(--primary);
      color: #fff;
      box-shadow: 0 12px 24px rgba(61, 115, 246, 0.25);
    }
    .btn:disabled {
      opacity: 0.56;
      cursor: not-allowed;
      transform: none;
    }
    .gallery {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(178px, 1fr));
      gap: 14px;
      min-height: 300px;
    }
    .image-card {
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      box-shadow: 0 10px 26px rgba(48, 76, 126, 0.07);
      transition: border 160ms ease, transform 160ms ease;
    }
    .image-card.selected {
      border-color: var(--primary);
      outline: 2px solid rgba(61, 115, 246, 0.18);
    }
    .image-card:hover { transform: translateY(-2px); }
    .thumb {
      width: 100%;
      aspect-ratio: 4 / 3;
      object-fit: cover;
      display: block;
      background: #edf3fb;
    }
    .card-meta {
      padding: 10px 10px 12px;
    }
    .card-name {
      overflow: hidden;
      white-space: nowrap;
      text-overflow: ellipsis;
      font-weight: 700;
      font-size: 13px;
    }
    .card-sub {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      margin-top: 7px;
      color: var(--muted);
      font-size: 12px;
    }
    .workspace {
      margin-top: 24px;
      padding: 18px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.8);
      box-shadow: var(--shadow);
    }
    .tabs {
      display: flex;
      gap: 20px;
      border-bottom: 1px solid var(--line);
      margin: -2px -4px 16px;
      padding: 0 4px;
    }
    .tab {
      min-height: 40px;
      background: transparent;
      color: var(--muted);
      border-bottom: 2px solid transparent;
      border-radius: 0;
      padding: 0;
      font-weight: 700;
    }
    .tab.active {
      color: var(--primary);
      border-bottom-color: var(--primary);
    }
    .form-grid {
      display: grid;
      grid-template-columns: minmax(260px, 1fr) 320px;
      gap: 22px;
    }
    label {
      display: block;
      margin-bottom: 8px;
      color: #314059;
      font-size: 13px;
      font-weight: 700;
    }
    textarea {
      width: 100%;
      min-height: 116px;
      resize: vertical;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 10px;
      outline: none;
      background: #fff;
      color: var(--text);
      line-height: 1.55;
    }
    .field-row {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin-top: 12px;
    }
    .field-row .control { width: 100%; height: 40px; }
    .style-strip {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 10px;
      margin-top: 28px;
    }
    .style-tile {
      height: 68px;
      border: 2px solid transparent;
      border-radius: 10px;
      background-size: cover;
    }
    .style-tile.active { border-color: var(--primary); }
    .preview {
      padding: 22px;
      background: rgba(255, 255, 255, 0.88);
    }
    .preview-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 18px;
    }
    .preview-box {
      overflow: hidden;
      border-radius: 8px;
      border: 1px solid var(--line);
      background: #edf3fb;
      aspect-ratio: 4 / 3;
    }
    .preview-box img {
      width: 100%;
      height: 100%;
      object-fit: contain;
      display: block;
    }
    .empty-preview,
    .empty-gallery {
      display: grid;
      place-items: center;
      min-height: 220px;
      border: 1px dashed var(--line-strong);
      border-radius: 12px;
      color: var(--muted);
      background: rgba(255,255,255,0.48);
      text-align: center;
      padding: 20px;
    }
    .detail-title {
      margin: 18px 0 8px;
      font-size: 18px;
      line-height: 1.35;
      word-break: break-all;
    }
    .detail-row {
      display: flex;
      justify-content: space-between;
      gap: 14px;
      padding: 10px 0;
      border-bottom: 1px solid var(--line);
      color: var(--muted);
      font-size: 13px;
    }
    .actions {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 10px;
      margin-top: 18px;
    }
    .login-mask {
      position: fixed;
      inset: 0;
      display: grid;
      place-items: center;
      padding: 20px;
      background: rgba(240, 246, 255, 0.92);
      z-index: 20;
    }
    .login-card {
      width: min(420px, 100%);
      padding: 28px;
      border-radius: 16px;
      background: #fff;
      box-shadow: var(--shadow);
      border: 1px solid var(--line);
    }
    .login-card h2 { margin-bottom: 8px; }
    .login-card input {
      width: 100%;
      height: 44px;
      margin: 14px 0 12px;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 0 12px;
      outline: none;
    }
    .toast {
      position: fixed;
      right: 24px;
      bottom: 24px;
      max-width: 380px;
      padding: 12px 14px;
      border-radius: 10px;
      background: #172033;
      color: #fff;
      box-shadow: var(--shadow);
      z-index: 30;
    }
    .hidden { display: none !important; }
    @media (max-width: 1180px) {
      .app-shell { grid-template-columns: 86px minmax(360px, 1fr) 320px; }
      .brand div:last-child,
      .nav-item span,
      .nav-label,
      .usage-card { display: none; }
      .sidebar { padding: 22px 14px; }
      .nav-item { justify-content: center; padding: 0; }
      .brand { justify-content: center; }
    }
    @media (max-width: 860px) {
      .app-shell { display: block; }
      .sidebar {
        position: sticky;
        top: 0;
        z-index: 5;
        display: flex;
        align-items: center;
        gap: 14px;
        padding: 12px;
      }
      .nav-group { display: flex; margin: 0; gap: 8px; }
      .main, .preview { padding: 16px; border: 0; }
      .form-grid { grid-template-columns: 1fr; }
      .field-row { grid-template-columns: repeat(2, 1fr); }
      .topbar, .section-head { align-items: stretch; flex-direction: column; }
      .status-pill { width: fit-content; }
    }
  </style>
</head>
<body>
  <div id="loginMask" class="login-mask">
    <form id="loginForm" class="login-card">
      <h2>登录图片后台</h2>
      <p class="muted">请输入插件配置中的后台登录密码。</p>
      <label for="passwordInput">登录密码</label>
      <input id="passwordInput" type="password" autocomplete="current-password" required>
      <button class="btn primary" type="submit" style="width:100%;">登录</button>
    </form>
  </div>

  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-mark">
          <svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3l7 4v6c0 4-3 7-7 8-4-1-7-4-7-8V7l7-4z"/><path d="M9 12l2 2 4-5"/></svg>
        </div>
        <div>
          <div class="brand-title">图灵 AI</div>
          <div class="brand-subtitle">图像生成管理平台</div>
        </div>
      </div>
      <div class="nav-group">
        <div class="nav-label">内容创作</div>
        <button class="nav-item active" type="button" data-mode="gallery" title="历史图库">
          <svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M8 13l2.5-2.5L14 14l2-2 3 3"/><circle cx="8" cy="9" r="1"/></svg>
          <span>历史图库</span>
        </button>
        <button class="nav-item" type="button" data-focus-tab="generate" title="生图">
          <svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v4"/><path d="M12 17v4"/><path d="M3 12h4"/><path d="M17 12h4"/><path d="M6 6l2.8 2.8"/><path d="M15.2 15.2L18 18"/><path d="M18 6l-2.8 2.8"/><path d="M8.8 15.2L6 18"/></svg>
          <span>生图</span>
        </button>
        <button class="nav-item" type="button" data-focus-tab="edit" title="编辑">
          <svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z"/></svg>
          <span>编辑</span>
        </button>
      </div>
      <div class="nav-group">
        <div class="nav-label">系统管理</div>
        <button class="nav-item" type="button" data-mode="settings" title="设置">
          <svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 01-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 01-4 0v-.2a1.7 1.7 0 00-1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 01-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 010-4h.2a1.7 1.7 0 001.5-1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 012.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 014 0v.2a1.7 1.7 0 001 1.5h.1a1.7 1.7 0 001.8-.3l.1-.1a2 2 0 012.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8v.1a1.7 1.7 0 001.5 1h.2a2 2 0 010 4h-.2a1.7 1.7 0 00-1.5 1z"/></svg>
          <span>设置</span>
        </button>
      </div>
      <div class="usage-card">
        <strong>本地缓存</strong>
        <span id="cacheSummary">等待加载</span>
      </div>
    </aside>

    <main class="main">
      <div class="topbar">
        <div class="search">
          <svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>
          <input id="searchInput" type="search" placeholder="搜索图片文件名...">
        </div>
        <div class="status-pill"><span class="status-dot"></span><span>系统状态正常</span></div>
      </div>

      <section id="galleryPanel">
        <div class="section-head">
          <div>
            <h1>历史图库</h1>
            <p class="muted"><span id="imageCount">0</span> 张缓存图片</p>
          </div>
          <button id="refreshBtn" class="btn" type="button">
            <svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M21 12a9 9 0 11-2.64-6.36"/><path d="M21 3v6h-6"/></svg>
            刷新
          </button>
        </div>
        <div class="filters">
          <select id="typeFilter" class="control" aria-label="图片类型">
            <option value="">全部类型</option>
            <option value=".png">PNG</option>
            <option value=".jpg">JPG</option>
            <option value=".jpeg">JPEG</option>
            <option value=".webp">WEBP</option>
          </select>
          <select id="sortFilter" class="control" aria-label="排序">
            <option value="newest">最新优先</option>
            <option value="oldest">最旧优先</option>
            <option value="name">名称排序</option>
          </select>
        </div>
        <div id="gallery" class="gallery"></div>
      </section>

      <section id="settingsPanel" class="workspace hidden">
        <h2>后台设置</h2>
        <p class="muted">端口、监听地址和登录密码请在 AstrBot 插件配置中修改，保存后重载插件生效。</p>
        <div class="detail-row"><span>默认端口</span><strong>7865</strong></div>
        <div class="detail-row"><span>默认监听</span><strong>127.0.0.1</strong></div>
        <div class="detail-row"><span>远程访问</span><strong>监听地址改为 0.0.0.0</strong></div>
      </section>

      <section class="workspace">
        <div class="tabs">
          <button id="generateTab" class="tab active" type="button" data-tab="generate">生图</button>
          <button id="editTab" class="tab" type="button" data-tab="edit">编辑</button>
        </div>
        <form id="generateForm" class="form-grid">
          <div>
            <label for="generatePrompt">提示词</label>
            <textarea id="generatePrompt" placeholder="描述你想生成的图片，例如：浅色自然光下的现代别墅，干净构图，高细节。"></textarea>
            <div class="field-row">
              <div><label for="generateCount">数量</label><input id="generateCount" class="control" type="number" min="1" max="4" value="1"></div>
              <div><label for="generateSize">尺寸</label><input id="generateSize" class="control" type="text" placeholder="auto"></div>
              <div><label for="generateQuality">质量</label><select id="generateQuality" class="control"><option>auto</option><option>low</option><option>medium</option><option>high</option></select></div>
              <div><label for="generateModeration">审核</label><select id="generateModeration" class="control"><option>low</option><option>auto</option></select></div>
            </div>
            <button id="generateSubmit" class="btn primary" type="submit" style="width:100%; margin-top:16px;">
              <svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v4"/><path d="M12 17v4"/><path d="M3 12h4"/><path d="M17 12h4"/><path d="M6 6l2.8 2.8"/><path d="M15.2 15.2L18 18"/><path d="M18 6l-2.8 2.8"/><path d="M8.8 15.2L6 18"/></svg>
              生成图片
            </button>
          </div>
          <div>
            <label>风格参考</label>
            <div class="style-strip">
              <div class="style-tile active" style="background:linear-gradient(135deg,#dbeafe,#9cc9ff);"></div>
              <div class="style-tile" style="background:linear-gradient(135deg,#fee2e2,#fef3c7);"></div>
              <div class="style-tile" style="background:linear-gradient(135deg,#dcfce7,#e0f2fe);"></div>
              <div class="style-tile" style="background:linear-gradient(135deg,#f3e8ff,#dbeafe);"></div>
            </div>
          </div>
        </form>

        <form id="editForm" class="form-grid hidden">
          <div>
            <label for="editPrompt">编辑提示词</label>
            <textarea id="editPrompt" placeholder="描述修改方式，例如：保持构图不变，改成柔和水彩风格。"></textarea>
            <div class="field-row">
              <div style="grid-column:span 2;"><label for="editImage">输入图片</label><input id="editImage" class="control" type="file" accept="image/png,image/jpeg,image/webp"></div>
              <div><label for="editSize">尺寸</label><input id="editSize" class="control" type="text" placeholder="auto"></div>
              <div><label for="editQuality">质量</label><select id="editQuality" class="control"><option>auto</option><option>low</option><option>medium</option><option>high</option></select></div>
            </div>
            <button id="editSubmit" class="btn primary" type="submit" style="width:100%; margin-top:16px;">
              <svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z"/></svg>
              编辑图片
            </button>
          </div>
          <div class="empty-gallery">上传图片后，结果会自动进入历史图库。</div>
        </form>
      </section>
    </main>

    <aside class="preview">
      <div class="preview-head">
        <h2>预览</h2>
        <button id="openImageBtn" class="btn" type="button" disabled>打开原图</button>
      </div>
      <div id="previewBox" class="preview-box empty-preview">请选择一张图片</div>
      <h3 id="detailTitle" class="detail-title">暂无选中图片</h3>
      <div class="detail-row"><span>格式</span><strong id="detailType">-</strong></div>
      <div class="detail-row"><span>大小</span><strong id="detailSize">-</strong></div>
      <div class="detail-row"><span>更新时间</span><strong id="detailTime">-</strong></div>
      <div class="actions">
        <button id="reusePromptBtn" class="btn" type="button">再次生成</button>
        <button id="useForEditBtn" class="btn" type="button">用于编辑</button>
      </div>
    </aside>
  </div>

  <div id="toast" class="toast hidden"></div>

  <script>
    const state = { token: localStorage.getItem("openaiImageAdminToken") || "", images: [], selected: null, activeTab: "generate" };
    const $ = (id) => document.getElementById(id);

    function authHeaders(extra = {}) {
      return { ...extra, Authorization: `Bearer ${state.token}` };
    }

    function showToast(message) {
      const toast = $("toast");
      toast.textContent = message;
      toast.classList.remove("hidden");
      window.clearTimeout(showToast.timer);
      showToast.timer = window.setTimeout(() => toast.classList.add("hidden"), 3600);
    }

    async function apiFetch(url, options = {}) {
      const response = await fetch(url, {
        ...options,
        headers: authHeaders(options.headers || {}),
      });
      if (response.status === 401) {
        localStorage.removeItem("openaiImageAdminToken");
        state.token = "";
        $("loginMask").classList.remove("hidden");
        throw new Error("请先登录");
      }
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "请求失败");
      return data;
    }

    function formatBytes(value) {
      if (!value) return "0 B";
      const units = ["B", "KB", "MB", "GB"];
      let size = value;
      let index = 0;
      while (size >= 1024 && index < units.length - 1) {
        size /= 1024;
        index += 1;
      }
      return `${size.toFixed(index ? 1 : 0)} ${units[index]}`;
    }

    function escapeHtml(value) {
      return String(value || "").replace(/[&<>"']/g, (char) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      }[char]));
    }

    function escapeAttribute(value) {
      return escapeHtml(value).replace(/`/g, "&#96;");
    }

    function imageUrl(image) {
      return `${image.url}?v=${image.modified_at}`;
    }

    function filteredImages() {
      const keyword = $("searchInput").value.trim().toLowerCase();
      const type = $("typeFilter").value;
      const sort = $("sortFilter").value;
      const images = state.images.filter((image) => {
        const name = image.name.toLowerCase();
        return (!keyword || name.includes(keyword)) && (!type || name.endsWith(type));
      });
      if (sort === "oldest") images.sort((a, b) => a.modified_at - b.modified_at);
      if (sort === "name") images.sort((a, b) => a.name.localeCompare(b.name));
      return images;
    }

    function renderGallery() {
      const gallery = $("gallery");
      const images = filteredImages();
      $("imageCount").textContent = state.images.length;
      $("cacheSummary").textContent = `${state.images.length} 张图片，占用 ${formatBytes(state.images.reduce((sum, item) => sum + item.size_bytes, 0))}`;
      if (!images.length) {
        gallery.innerHTML = '<div class="empty-gallery" style="grid-column:1/-1;">暂无图片，先在下方生成一张。</div>';
        return;
      }
      gallery.innerHTML = images.map((image) => {
        const safeName = escapeHtml(image.name);
        const safeAttrName = escapeAttribute(image.name);
        const safeUrl = escapeAttribute(imageUrl(image));
        return `
        <button class="image-card ${state.selected && state.selected.name === image.name ? "selected" : ""}" type="button" data-name="${safeAttrName}">
          <img class="thumb" src="${safeUrl}" alt="${safeAttrName}" loading="lazy">
          <div class="card-meta">
            <div class="card-name" title="${safeAttrName}">${safeName}</div>
            <div class="card-sub"><span>${formatBytes(image.size_bytes)}</span><span>${new Date(image.modified_at * 1000).toLocaleString()}</span></div>
          </div>
        </button>
      `;
      }).join("");
      gallery.querySelectorAll(".image-card").forEach((card) => {
        card.addEventListener("click", () => selectImage(card.dataset.name));
      });
    }

    function selectImage(name) {
      const image = state.images.find((item) => item.name === name);
      if (!image) return;
      state.selected = image;
      $("previewBox").classList.remove("empty-preview");
      $("previewBox").innerHTML = `<img src="${escapeAttribute(imageUrl(image))}" alt="${escapeAttribute(image.name)}">`;
      $("detailTitle").textContent = image.name;
      $("detailType").textContent = image.mime_type;
      $("detailSize").textContent = formatBytes(image.size_bytes);
      $("detailTime").textContent = new Date(image.modified_at * 1000).toLocaleString();
      $("openImageBtn").disabled = false;
      renderGallery();
    }

    async function loadImages(preferredName = "") {
      const data = await apiFetch("/api/images");
      state.images = data.images || [];
      if (preferredName) {
        selectImage(preferredName);
      } else if (!state.selected && state.images.length) {
        selectImage(state.images[0].name);
      } else {
        renderGallery();
      }
    }

    function setActiveTab(tab) {
      state.activeTab = tab;
      $("generateTab").classList.toggle("active", tab === "generate");
      $("editTab").classList.toggle("active", tab === "edit");
      $("generateForm").classList.toggle("hidden", tab !== "generate");
      $("editForm").classList.toggle("hidden", tab !== "edit");
    }

    $("loginForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const response = await fetch("/api/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ password: $("passwordInput").value }),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "登录失败");
        state.token = data.token;
        localStorage.setItem("openaiImageAdminToken", state.token);
        $("loginMask").classList.add("hidden");
        await loadImages();
      } catch (error) {
        showToast(error.message);
      }
    });

    $("refreshBtn").addEventListener("click", () => loadImages().catch((error) => showToast(error.message)));
    $("searchInput").addEventListener("input", renderGallery);
    $("typeFilter").addEventListener("change", renderGallery);
    $("sortFilter").addEventListener("change", renderGallery);
    $("generateTab").addEventListener("click", () => setActiveTab("generate"));
    $("editTab").addEventListener("click", () => setActiveTab("edit"));
    document.querySelectorAll("[data-focus-tab]").forEach((button) => {
      button.addEventListener("click", () => setActiveTab(button.dataset.focusTab));
    });
    document.querySelectorAll("[data-mode]").forEach((button) => {
      button.addEventListener("click", () => {
        document.querySelectorAll(".nav-item").forEach((item) => item.classList.remove("active"));
        button.classList.add("active");
        $("settingsPanel").classList.toggle("hidden", button.dataset.mode !== "settings");
        $("galleryPanel").classList.toggle("hidden", button.dataset.mode === "settings");
      });
    });
    $("openImageBtn").addEventListener("click", () => {
      if (state.selected) window.open(imageUrl(state.selected), "_blank", "noopener");
    });
    $("reusePromptBtn").addEventListener("click", () => {
      setActiveTab("generate");
      $("generatePrompt").focus();
    });
    $("useForEditBtn").addEventListener("click", () => {
      setActiveTab("edit");
      $("editPrompt").focus();
    });

    $("generateForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const count = Math.max(1, Math.min(4, Number($("generateCount").value || 1)));
      const submit = $("generateSubmit");
      submit.disabled = true;
      try {
        let lastImage = "";
        for (let index = 0; index < count; index += 1) {
          const data = await apiFetch("/api/generate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              prompt: $("generatePrompt").value,
              size: $("generateSize").value,
              quality: $("generateQuality").value,
              moderation: $("generateModeration").value,
            }),
          });
          lastImage = data.image && data.image.name;
        }
        await loadImages(lastImage);
        showToast("图片生成完成");
      } catch (error) {
        showToast(error.message);
      } finally {
        submit.disabled = false;
      }
    });

    $("editForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const file = $("editImage").files[0];
      if (!file) {
        showToast("请先选择待编辑图片");
        return;
      }
      const body = new FormData();
      body.append("prompt", $("editPrompt").value);
      body.append("size", $("editSize").value);
      body.append("quality", $("editQuality").value);
      body.append("moderation", "low");
      body.append("image", file);
      const submit = $("editSubmit");
      submit.disabled = true;
      try {
        const data = await apiFetch("/api/edit", { method: "POST", body });
        await loadImages(data.image && data.image.name);
        showToast("图片编辑完成");
      } catch (error) {
        showToast(error.message);
      } finally {
        submit.disabled = false;
      }
    });

    if (state.token) {
      $("loginMask").classList.add("hidden");
      loadImages().catch(() => $("loginMask").classList.remove("hidden"));
    }
  </script>
</body>
</html>
"""
