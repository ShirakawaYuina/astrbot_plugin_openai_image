"""网页后台管理服务。"""

from __future__ import annotations

import base64
import json
import mimetypes
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from aiohttp import web

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .storage.cache_cleaner import IMAGE_SUFFIXES

PLUGIN_DATA_DIR_NAME = "astrbot_plugin_openai_image"
DEFAULT_ADMIN_HOST = "127.0.0.1"
DEFAULT_ADMIN_PORT = 7865
PROMPT_OPTIMIZER_SETTINGS_FILE_NAME = "prompt_optimizer_settings.json"
TOKEN_BYTES = 24
AUTH_COOKIE_NAME = "openai_image_admin_token"
LOG_PROMPT_MAX_LENGTH = 120
IMAGE_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
PROMPT_OPTIMIZER_MODE_LABELS = {
    "generate": "文生图",
    "edit": "图片编辑",
}
PROMPT_OPTIMIZER_SYSTEM_PROMPT = (
    "你是专业的图像生成提示词优化师。请把用户的中文提示词扩写为更适合图像模型理解的提示词，"
    "强化主体、场景、构图、光线、材质、风格、镜头和细节层次。必须保留用户原始意图，"
    "不要添加与原意冲突的元素，不要输出解释、标题、编号或 Markdown，只输出优化后的提示词正文。"
)


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


@dataclass(frozen=True, slots=True)
class PromptOptimizerSettings:
    """提示词优化模型配置。

    该功能走独立的 OpenAI 兼容 Chat Completions 接口，避免和图片接口供应商混用；
    任一关键字段缺失时显式报错，提醒用户到插件设置页补齐配置。
    """

    model: str
    base_url: str
    api_key: str
    timeout_seconds: int

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> PromptOptimizerSettings:
        """从网页后台保存的配置读取提示词优化模型参数，并校验必填字段。"""

        model = str(config.get("prompt_optimizer_model", "") or "").strip()
        base_url = (
            str(config.get("prompt_optimizer_base_url", "") or "").strip().rstrip("/")
        )
        api_key = str(config.get("prompt_optimizer_api_key", "") or "").strip()
        if not model:
            raise ValueError("请先在插件设置中配置提示词优化模型名称")
        if not base_url:
            raise ValueError("请先在插件设置中配置提示词优化 Base URL")
        if not api_key:
            raise ValueError("请先在插件设置中配置提示词优化 API Key")

        return cls(
            model=model,
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=max(
                5, int(config.get("request_timeout_seconds", 180) or 180)
            ),
        )


class PromptOptimizerSettingsStore:
    """读写网页后台专用的提示词优化配置。"""

    def __init__(self, settings_path: Path | None = None) -> None:
        self.settings_path = settings_path or _default_prompt_optimizer_settings_path()

    def load(self) -> dict[str, Any]:
        """读取本地配置文件，文件不存在时返回空配置。"""

        if not self.settings_path.is_file():
            return {}
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"提示词优化配置读取失败: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("提示词优化配置格式错误")
        return data

    def public_payload(self) -> dict[str, Any]:
        """返回可给前端展示的配置摘要，API Key 只暴露是否已保存。"""

        settings = self.load()
        return {
            "model": str(settings.get("prompt_optimizer_model", "") or ""),
            "base_url": str(settings.get("prompt_optimizer_base_url", "") or ""),
            "has_api_key": bool(
                str(settings.get("prompt_optimizer_api_key", "") or "").strip()
            ),
        }

    def save_public_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存前端提交的模型配置，空 API Key 表示保留旧密钥。"""

        current_settings = self.load()
        next_settings = {
            "prompt_optimizer_model": str(payload.get("model", "") or "").strip(),
            "prompt_optimizer_base_url": str(payload.get("base_url", "") or "").strip(),
            "prompt_optimizer_api_key": str(
                current_settings.get("prompt_optimizer_api_key", "") or ""
            ).strip(),
        }

        new_api_key = str(payload.get("api_key", "") or "").strip()
        if new_api_key:
            next_settings["prompt_optimizer_api_key"] = new_api_key

        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(
            json.dumps(next_settings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return self.public_payload()


def _default_prompt_optimizer_settings_path() -> Path:
    """返回提示词优化设置文件路径，统一放在插件数据目录内。"""

    return (
        Path(get_astrbot_plugin_data_path())
        / PLUGIN_DATA_DIR_NAME
        / PROMPT_OPTIMIZER_SETTINGS_FILE_NAME
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

    def delete_image_by_name(self, file_name: str) -> str:
        """删除缓存图片及同名元数据文件，供网页后台管理历史图片。"""

        image_path = self.resolve_image_path(file_name)
        removed_name = image_path.name
        image_path.unlink()
        metadata_path = self._metadata_path_for(image_path)
        if metadata_path.is_file():
            metadata_path.unlink()
        return removed_name

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

    @classmethod
    def _build_image_metadata(cls, image_path: Path) -> dict[str, Any]:
        """将缓存图片路径转换为前端需要的稳定字段。"""

        stat_result = image_path.stat()
        metadata = cls._read_image_sidecar_metadata(image_path)
        return {
            "name": image_path.name,
            "url": f"/api/images/{image_path.name}",
            "mime_type": _guess_mime_type(image_path),
            "size_bytes": stat_result.st_size,
            "modified_at": int(stat_result.st_mtime),
            "prompt": str(metadata.get("prompt", "") or ""),
            "generation_size": str(metadata.get("size", "") or ""),
            "mode": str(metadata.get("mode", "") or ""),
        }

    @staticmethod
    def _metadata_path_for(image_path: Path) -> Path:
        """返回图片对应的 sidecar 元数据路径。"""

        return image_path.with_name(f"{image_path.name}.json")

    @classmethod
    def _read_image_sidecar_metadata(cls, image_path: Path) -> dict[str, Any]:
        """读取图片同名元数据；旧图片没有记录时返回空字段。"""

        metadata_path = cls._metadata_path_for(image_path)
        if not metadata_path.is_file():
            return {}
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # 元数据缺失或损坏不应影响图库浏览，前端会展示“未记录”。
            return {}
        return data if isinstance(data, dict) else {}


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
    data_url: str = "",
    data_urls: list[str] | None = None,
    size: str | None,
    quality: str,
    moderation: str,
) -> Callable[[], Awaitable[Path]]:
    """创建图片编辑任务闭包，统一复用插件既有编辑服务。"""

    async def _job() -> Path:
        # 网页端允许多张参考图；保留单图参数分支，避免其它调用方仍使用旧接口时失效。
        image_kwargs = (
            {"data_urls": data_urls}
            if data_urls is not None
            else {"data_url": data_url}
        )
        return await plugin._edit_service.edit(
            model=plugin._get_configured_model(),
            prompt=prompt,
            **image_kwargs,
            endpoint_type=plugin._get_endpoint_type(),
            size=plugin._resolve_output_size(size),
            quality=quality,
            moderation=moderation,
        )

    return _job


async def optimize_prompt_text(
    *,
    settings_store: PromptOptimizerSettingsStore,
    prompt: str,
    mode: str,
) -> str:
    """调用用户配置的文本模型扩写提示词。

    网页后台只负责把当前输入发送给优化模型，并把纯文本结果回填到输入框；
    配置缺失、网络错误或响应结构异常都会抛出明确异常，避免用户误以为已经优化成功。
    """

    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        raise ValueError("提示词不能为空")

    settings = PromptOptimizerSettings.from_config(settings_store.load())
    endpoint = _resolve_chat_completions_endpoint(settings.base_url)
    payload = _build_prompt_optimizer_payload(
        model=settings.model,
        prompt=clean_prompt,
        mode=mode,
    )
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.api_key}",
    }
    timeout = aiohttp.ClientTimeout(total=settings.timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
        try:
            async with session.post(
                endpoint, json=payload, headers=headers
            ) as response:
                response.raise_for_status()
                response_data = await response.json()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"提示词优化接口请求失败: {exc}") from exc

    optimized_prompt = _extract_optimized_prompt(response_data)
    if not optimized_prompt:
        raise RuntimeError("提示词优化接口未返回可用文本")
    return optimized_prompt


def _resolve_chat_completions_endpoint(base_url: str) -> str:
    """根据 Base URL 推导 OpenAI 兼容 Chat Completions 接口地址。"""

    normalized = str(base_url or "").strip().rstrip("/")
    if not normalized:
        raise ValueError("提示词优化 Base URL 不能为空")

    chat_suffix = "/chat/completions"
    if normalized.endswith(chat_suffix):
        return normalized

    parsed = urlsplit(normalized)
    base_path = parsed.path.rstrip("/")
    merged_path = f"{base_path}{chat_suffix}" if base_path else chat_suffix
    return urlunsplit((parsed.scheme, parsed.netloc, merged_path, "", ""))


def _build_prompt_optimizer_payload(
    *,
    model: str,
    prompt: str,
    mode: str,
) -> dict[str, Any]:
    """构造提示词优化请求体，集中约束模型只返回最终提示词。"""

    mode_label = PROMPT_OPTIMIZER_MODE_LABELS.get(str(mode or "").strip(), "图片生成")
    user_prompt = (
        f"当前任务类型：{mode_label}\n"
        "请优化以下提示词，使其更具体、更适合图像模型执行：\n"
        f"{prompt}"
    )
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": PROMPT_OPTIMIZER_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.7,
    }


def _extract_optimized_prompt(response_data: Any) -> str:
    """从 Chat Completions 响应中提取助手文本。"""

    if not isinstance(response_data, dict):
        raise RuntimeError(
            f"提示词优化接口响应结构异常: {type(response_data).__name__}"
        )

    choices = response_data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("提示词优化接口响应缺少 choices")

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise RuntimeError("提示词优化接口响应 choices[0] 不是对象")

    message = first_choice.get("message")
    if not isinstance(message, dict):
        raise RuntimeError("提示词优化接口响应缺少 message")

    content = message.get("content")
    if isinstance(content, str):
        return content.strip()

    # 部分兼容服务会把 content 返回为多段结构，这里只拼接文本段，不把未知段伪装为成功内容。
    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                text_parts.append(text.strip())
        return "\n".join(text_parts).strip()

    raise RuntimeError("提示词优化接口响应 message.content 不是文本")


class WebAdminServer:
    """插件内置的 aiohttp 网页后台。"""

    def __init__(
        self, plugin: Any, settings: WebAdminSettings, cache_dir: Path
    ) -> None:
        self.plugin = plugin
        self.settings = settings
        self.library = ImageLibrary(cache_dir)
        self.prompt_optimizer_settings = PromptOptimizerSettingsStore()
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
        app.router.add_delete("/api/images/{file_name}", self._handle_delete_image)
        app.router.add_post("/api/generate", self._handle_generate)
        app.router.add_post("/api/edit", self._handle_edit)
        app.router.add_get(
            "/api/prompt-optimizer-settings",
            self._handle_get_prompt_optimizer_settings,
        )
        app.router.add_post(
            "/api/prompt-optimizer-settings",
            self._handle_save_prompt_optimizer_settings,
        )
        app.router.add_post("/api/optimize-prompt", self._handle_optimize_prompt)
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

    async def _handle_delete_image(self, request: web.Request) -> web.Response:
        """删除服务端缓存图片及其元数据。"""

        auth_response = self._require_auth(request)
        if auth_response is not None:
            return auth_response

        try:
            removed_name = self.library.delete_image_by_name(
                request.match_info.get("file_name", "")
            )
        except FileNotFoundError:
            return web.json_response({"error": "图片不存在"}, status=404)

        logger.info("[OpenAIImage][web] 删除历史图片 file=%s", removed_name)
        return web.json_response({"deleted": removed_name})

    async def _handle_get_prompt_optimizer_settings(
        self, request: web.Request
    ) -> web.Response:
        """读取网页端提示词优化设置，密钥只返回是否存在。"""

        auth_response = self._require_auth(request)
        if auth_response is not None:
            return auth_response

        try:
            return web.json_response(self.prompt_optimizer_settings.public_payload())
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_save_prompt_optimizer_settings(
        self, request: web.Request
    ) -> web.Response:
        """保存网页端提示词优化设置。"""

        auth_response = self._require_auth(request)
        if auth_response is not None:
            return auth_response

        payload = await request.json()
        try:
            saved_payload = self.prompt_optimizer_settings.save_public_payload(payload)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=500)

        logger.info(
            "[OpenAIImage][web][prompt_optimizer] 设置已保存 model=%s base_url=%s has_api_key=%s",
            saved_payload["model"] or "-",
            saved_payload["base_url"] or "-",
            saved_payload["has_api_key"],
        )
        return web.json_response(saved_payload)

    async def _handle_generate(self, request: web.Request) -> web.Response:
        """处理网页文生图请求。"""

        auth_response = self._require_auth(request)
        if auth_response is not None:
            return auth_response

        payload = await request.json()
        prompt = str(payload.get("prompt", "") or "").strip()
        if not prompt:
            return web.json_response({"error": "提示词不能为空"}, status=400)
        size = _optional_text(payload.get("size"))
        quality = _option_text(payload.get("quality"), "auto")
        moderation = _option_text(payload.get("moderation"), "low")

        self.plugin._ensure_ready()
        self._log_web_request(
            mode="generate",
            prompt=prompt,
            size=size or "auto",
            quality=quality,
            moderation=moderation,
        )
        task_result = await self.plugin._task_service.run_task(
            mode="web_generate",
            job_coro=create_generation_job(
                plugin=self.plugin,
                prompt=prompt,
                size=size,
                quality=quality,
                moderation=moderation,
            ),
            stage_name="web_generate",
        )
        return self._task_response(task_result)

    async def _handle_optimize_prompt(self, request: web.Request) -> web.Response:
        """处理网页端提示词优化请求。"""

        auth_response = self._require_auth(request)
        if auth_response is not None:
            return auth_response

        payload = await request.json()
        prompt = str(payload.get("prompt", "") or "").strip()
        mode = _option_text(payload.get("mode"), "generate")
        if not prompt:
            return web.json_response({"error": "提示词不能为空"}, status=400)

        try:
            optimized_prompt = await optimize_prompt_text(
                settings_store=self.prompt_optimizer_settings,
                prompt=prompt,
                mode=mode,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[OpenAIImage][web][prompt_optimizer] 优化失败 mode=%s prompt=%s error=%s",
                mode,
                _truncate_log_text(prompt),
                _truncate_log_text(str(exc)),
            )
            return web.json_response({"error": str(exc)}, status=500)

        logger.info(
            "[OpenAIImage][web][prompt_optimizer] 优化完成 mode=%s before=%s after=%s",
            mode,
            _truncate_log_text(prompt),
            _truncate_log_text(optimized_prompt),
        )
        return web.json_response({"prompt": optimized_prompt})

    async def _handle_edit(self, request: web.Request) -> web.Response:
        """处理网页图片编辑请求。"""

        auth_response = self._require_auth(request)
        if auth_response is not None:
            return auth_response

        reader = await request.multipart()
        fields: dict[str, str] = {}
        image_data_urls: list[str] = []
        async for part in reader:
            if part.name == "image":
                # 前端会为每张参考图追加一个同名 image 字段，这里按顺序收集后交给编辑服务。
                image_data_urls.append(await _multipart_image_to_data_url(part))
            else:
                fields[str(part.name)] = (await part.text()).strip()

        prompt = fields.get("prompt", "").strip()
        if not prompt:
            return web.json_response({"error": "提示词不能为空"}, status=400)
        if not image_data_urls:
            return web.json_response({"error": "请上传待编辑图片"}, status=400)
        size = _optional_text(fields.get("size"))
        quality = _option_text(fields.get("quality"), "auto")
        moderation = _option_text(fields.get("moderation"), "low")

        self.plugin._ensure_ready()
        self._log_web_request(
            mode="edit",
            prompt=prompt,
            size=size or "auto",
            quality=quality,
            moderation=moderation,
        )
        task_result = await self.plugin._task_service.run_task(
            mode="web_edit",
            job_coro=create_edit_job(
                plugin=self.plugin,
                prompt=prompt,
                data_urls=image_data_urls,
                size=size,
                quality=quality,
                moderation=moderation,
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

        self._log_task_result(task_result)
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

    def _log_web_request(
        self,
        *,
        mode: str,
        prompt: str,
        size: str,
        quality: str,
        moderation: str,
    ) -> None:
        """记录网页端发起的图片任务，便于在 AstrBot 后台定位用户操作。"""

        logger.info(
            "[OpenAIImage][web][%s] 收到请求 prompt=%s size=%s quality=%s moderation=%s endpoint_type=%s model=%s",
            mode,
            _truncate_log_text(prompt),
            size,
            quality,
            moderation,
            self._safe_plugin_value("_get_endpoint_type"),
            self._safe_plugin_value("_get_configured_model"),
        )

    def _log_task_result(self, task_result: dict[str, Any]) -> None:
        """记录网页任务执行结果，失败日志包含阶段和错误摘要。"""

        timings = task_result.get("timings", {})
        elapsed_ms = timings.get("elapsed_ms")
        queue_wait_ms = timings.get("queue_wait_ms")
        mode = str(task_result.get("mode") or "web_unknown")
        if task_result.get("success"):
            payload = Path(str(task_result.get("payload", ""))).name or "-"
            logger.info(
                "[OpenAIImage][web][%s] 任务完成 output=%s elapsed_ms=%s queue_wait_ms=%s",
                mode,
                payload,
                elapsed_ms,
                queue_wait_ms,
            )
            return

        logger.warning(
            "[OpenAIImage][web][%s] 任务失败 stage=%s error=%s elapsed_ms=%s queue_wait_ms=%s",
            mode,
            str(task_result.get("error_stage") or "-"),
            _truncate_log_text(str(task_result.get("error_message") or "图片任务失败")),
            elapsed_ms,
            queue_wait_ms,
        )

    def _safe_plugin_value(self, getter_name: str) -> str:
        """读取插件运行时字段；测试桩或异常状态下使用 unknown 兜底，避免日志反向打断请求。"""

        getter = getattr(self.plugin, getter_name, None)
        if not callable(getter):
            return "unknown"
        try:
            return str(getter())
        except Exception as exc:  # noqa: BLE001
            # 日志字段不是业务主流程，异常只作为摘要输出，避免掩盖真实生图/编辑结果。
            return f"unknown({exc.__class__.__name__})"


def _normalize_port(value: Any) -> int:
    """归一化端口号，越界或非法时回退到默认端口。"""

    try:
        port = int(value)
    except (TypeError, ValueError):
        return DEFAULT_ADMIN_PORT
    if 1 <= port <= 65535:
        return port
    return DEFAULT_ADMIN_PORT


def _truncate_log_text(value: str, max_length: int = LOG_PROMPT_MAX_LENGTH) -> str:
    """压缩日志文本，去掉换行并截断超长提示词，避免后台日志被大段内容刷屏。"""

    normalized = " ".join(str(value or "").split())
    if len(normalized) <= max_length:
        return normalized
    return f"{normalized[:max_length]}..."


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
      align-items: start;
      min-height: 100vh;
    }
    body.preview-hidden .app-shell {
      grid-template-columns: 240px minmax(420px, 1fr);
    }
    body.preview-hidden .preview {
      display: none;
    }
    body.preview-hidden .topbar {
      display: none;
    }
    .sidebar,
    .preview {
      position: sticky;
      top: 0;
      height: 100vh;
      overflow-y: auto;
    }
    .sidebar {
      padding: 24px 18px;
      background: rgba(248, 251, 255, 0.88);
      border-right: 1px solid var(--line);
      backdrop-filter: blur(16px);
      display: flex;
      flex-direction: column;
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
    .btn.danger {
      background: #fdecec;
      color: var(--danger);
    }
    .btn:disabled {
      opacity: 0.56;
      cursor: not-allowed;
      transform: none;
    }
    .btn.is-loading .loading-icon {
      animation: result-spin 800ms linear infinite;
    }
    .gallery {
      display: grid;
      grid-template-columns: repeat(var(--gallery-column-count, 1), minmax(0, 1fr));
      align-items: start;
      gap: 14px;
      min-height: 300px;
    }
    .gallery-column {
      display: flex;
      flex-direction: column;
      gap: 14px;
      min-width: 0;
    }
    .gallery-empty {
      width: 100%;
      box-sizing: border-box;
      grid-column: 1 / -1;
    }
    .pagination-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-top: 16px;
      min-height: 40px;
      color: var(--muted);
      font-size: 13px;
    }
    .pagination-actions {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .image-card {
      position: relative;
      display: block;
      width: 100%;
      margin: 0;
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
    .image-card-actions {
      position: absolute;
      top: 8px;
      right: 8px;
      display: flex;
      gap: 6px;
    }
    .image-card-action {
      display: grid;
      place-items: center;
      width: 32px;
      height: 32px;
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.9);
      color: var(--danger);
      box-shadow: 0 8px 20px rgba(24, 32, 51, 0.12);
    }
    .thumb {
      width: 100%;
      height: auto;
      object-fit: contain;
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
    .mode-switch {
      display: flex;
      width: fit-content;
      gap: 6px;
      padding: 5px;
      margin-bottom: 16px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #f3f7fd;
    }
    .mode-option {
      min-height: 34px;
      padding: 0 14px;
      border-radius: 8px;
      color: var(--muted);
      background: transparent;
      font-weight: 700;
    }
    .mode-option.active {
      background: #fff;
      color: var(--primary);
      box-shadow: 0 8px 18px rgba(48, 76, 126, 0.09);
    }
    .workflow-layout {
      display: grid;
      grid-template-rows: minmax(360px, 1fr) auto;
      min-height: calc(100vh - 190px);
      gap: 14px;
    }
    .workspace-result-panel {
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      gap: 8px;
      min-height: 0;
    }
    .result-label {
      width: fit-content;
      margin: 0;
      padding: 3px 8px;
      border-radius: 999px;
      background: var(--primary-soft);
      color: #47628d;
      font-size: 12px;
      font-weight: 700;
    }
    .result-box {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      align-items: stretch;
      gap: 12px;
      min-height: clamp(360px, 64vh, 680px);
      padding: 14px;
      border: 1px dashed var(--line-strong);
      border-radius: 12px;
      background: rgba(248, 251, 255, 0.68);
    }
    .result-box.result-state-empty,
    .result-box.result-state-loading,
    .result-box.result-state-error {
      display: grid;
      grid-template-columns: 1fr;
      place-items: center;
      text-align: center;
    }
    .result-box.result-state-empty {
      border-style: dashed;
      color: var(--muted);
      background: #f7f9fc;
    }
    .result-box.result-state-loading {
      border-color: rgba(61, 115, 246, 0.38);
      background: #eef5ff;
      color: var(--primary);
    }
    .result-box.result-state-success {
      border-style: solid;
      background: #fff;
    }
    .result-box.result-state-error {
      border-color: rgba(194, 65, 65, 0.38);
      background: #fff5f5;
      color: var(--danger);
    }
    .result-status {
      display: grid;
      place-items: center;
      gap: 10px;
      max-width: 360px;
      line-height: 1.6;
    }
    .result-status strong {
      color: inherit;
      font-size: 15px;
    }
    .result-spinner {
      width: 34px;
      height: 34px;
      border: 3px solid rgba(61, 115, 246, 0.18);
      border-top-color: var(--primary);
      border-radius: 50%;
      animation: result-spin 800ms linear infinite;
    }
    @keyframes result-spin {
      to { transform: rotate(360deg); }
    }
    .result-box img {
      width: 100%;
      height: 100%;
      min-height: 248px;
      max-height: 640px;
      object-fit: contain;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #edf3fb;
      cursor: zoom-in;
    }
    .action-panel {
      display: grid;
      grid-template-columns: minmax(280px, 1fr) 300px;
      align-self: end;
      align-items: start;
      gap: 14px;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.68);
    }
    .action-panel-single {
      grid-template-columns: 1fr;
    }
    .control-stack {
      display: grid;
      align-content: start;
      gap: 12px;
    }
    .prompt-action-row {
      display: grid;
      grid-template-columns: 136px minmax(0, 1fr);
      align-items: end;
      gap: 12px;
    }
    .prompt-action-row > .btn {
      width: 100%;
      min-height: 86px;
      flex-direction: column;
      align-self: end;
    }
    .prompt-field {
      min-width: 0;
    }
    .prompt-label-row {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 8px;
      min-height: 30px;
      white-space: nowrap;
    }
    .prompt-label-row label {
      margin-bottom: 0;
      line-height: 30px;
    }
    .prompt-optimize-btn {
      min-height: 24px;
      padding: 0 8px;
      border: 1px solid rgba(65, 116, 201, 0.26);
      border-radius: 999px;
      background: #f4f8ff;
      color: #1f4f9f;
      font-size: 12px;
      font-weight: 700;
      line-height: 1;
    }
    .prompt-optimize-btn svg {
      width: 13px;
      height: 13px;
      stroke-width: 2.2;
    }
    .prompt-optimize-btn:hover {
      background: #eaf1ff;
      box-shadow: 0 8px 18px rgba(53, 101, 181, 0.12);
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
      min-height: 86px;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 10px;
      outline: none;
      background: #fff;
      color: var(--text);
      line-height: 1.55;
    }
    .auto-resize-textarea {
      overflow: hidden;
      resize: none;
    }
    textarea:disabled {
      background: #f3f7fd;
      color: #6f7d94;
      cursor: wait;
    }
    .field-row {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin-top: 12px;
    }
    .field-row .control { width: 100%; height: 40px; }
    .edit-options {
      grid-template-columns: repeat(4, minmax(0, 1fr));
    }
    .custom-size { margin-top: 10px; }
    .reference-panel {
      display: grid;
      gap: 12px;
      align-content: start;
    }
    .reference-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      min-width: 0;
    }
    .reference-head span {
      font-weight: 700;
      color: var(--text);
    }
    .reference-file-input {
      position: absolute;
      inline-size: 1px;
      block-size: 1px;
      overflow: hidden;
      clip: rect(0 0 0 0);
      clip-path: inset(50%);
      white-space: nowrap;
    }
    .reference-upload-btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      min-height: 40px;
      padding: 0 14px;
      border: 1px solid rgba(65, 116, 201, 0.26);
      border-radius: 10px;
      background: linear-gradient(180deg, #ffffff 0%, #f4f8ff 100%);
      color: #1f4f9f;
      font-size: 13px;
      font-weight: 700;
      line-height: 1;
      cursor: pointer;
      box-shadow: 0 10px 22px rgba(53, 101, 181, 0.12);
      transition: background 160ms ease, border-color 160ms ease, box-shadow 160ms ease, color 160ms ease;
      user-select: none;
      white-space: nowrap;
    }
    .reference-upload-btn:hover {
      border-color: rgba(40, 96, 190, 0.45);
      background: #eef5ff;
      color: #153f83;
      box-shadow: 0 12px 26px rgba(53, 101, 181, 0.18);
    }
    .reference-file-input:focus-visible + .reference-upload-btn {
      outline: 3px solid rgba(43, 108, 246, 0.22);
      outline-offset: 2px;
    }
    .reference-upload-btn svg {
      width: 18px;
      height: 18px;
      stroke-width: 2.1;
    }
    .reference-thumbs {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }
    .reference-thumb {
      position: relative;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #edf3fb;
      aspect-ratio: 4 / 3;
    }
    .reference-thumb img {
      width: 100%;
      height: 100%;
      object-fit: contain;
      display: block;
    }
    .reference-thumb-remove {
      position: absolute;
      top: 6px;
      right: 6px;
      display: grid;
      place-items: center;
      width: 28px;
      height: 28px;
      border-radius: 8px;
      background: rgba(23, 32, 51, 0.72);
      color: #fff;
      box-shadow: 0 8px 18px rgba(24, 32, 51, 0.18);
    }
    .reference-thumb-remove:hover {
      background: var(--danger);
    }
    .preview {
      padding: 22px;
      background: rgba(255, 255, 255, 0.88);
      overflow-x: hidden;
    }
    .preview-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 18px;
      gap: 10px;
      min-width: 0;
    }
    .preview .settings-actions {
      flex: 0 1 auto;
      min-width: 0;
      justify-content: flex-end;
    }
    .preview-box {
      display: grid;
      place-items: center;
      overflow: hidden;
      border-radius: 8px;
      border: 1px solid var(--line);
      background: #edf3fb;
      aspect-ratio: 1 / 1;
      min-height: 420px;
      cursor: zoom-in;
    }
    .preview-box img {
      max-width: 100%;
      max-height: 100%;
      width: auto;
      height: auto;
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
      min-width: 0;
    }
    .detail-row {
      display: flex;
      justify-content: space-between;
      gap: 14px;
      padding: 10px 0;
      border-bottom: 1px solid var(--line);
      color: var(--muted);
      font-size: 13px;
      min-width: 0;
    }
    .detail-row strong {
      color: #263247;
      text-align: right;
      word-break: break-word;
      overflow-wrap: anywhere;
      min-width: 0;
    }
    .detail-row.detail-row-block {
      display: block;
    }
    .detail-row.detail-row-block strong {
      display: block;
      margin-top: 8px;
      text-align: left;
      line-height: 1.5;
    }
    .actions {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 10px;
      margin-top: 18px;
    }
    .settings-grid {
      display: grid;
      gap: 14px;
      max-width: 760px;
    }
    .settings-card {
      display: grid;
      gap: 12px;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: rgba(255, 255, 255, 0.72);
    }
    .settings-card label {
      font-weight: 700;
      color: var(--text);
    }
    .settings-form-grid {
      display: grid;
      gap: 12px;
    }
    .settings-form-grid .control {
      width: 100%;
      height: 40px;
      margin-top: 8px;
    }
    .settings-inline {
      display: flex;
      flex-wrap: wrap;
      align-items: end;
      gap: 10px;
    }
    .settings-inline .control {
      width: 160px;
    }
    .settings-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }
    .cache-info {
      display: grid;
      gap: 10px;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: rgba(255, 255, 255, 0.72);
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
      .preview { position: static; height: auto; overflow-x: hidden; overflow-y: visible; }
      .form-grid { grid-template-columns: 1fr; }
      .field-row,
      .edit-options { grid-template-columns: repeat(2, 1fr); }
      .settings-form-grid { grid-template-columns: 1fr; }
      .workflow-layout { min-height: 0; }
      .action-panel { grid-template-columns: 1fr; }
      .prompt-action-row { grid-template-columns: 1fr; }
      .prompt-action-row > .btn { min-height: 46px; flex-direction: row; }
      .result-box { min-height: 360px; }
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
        <button class="nav-item active" type="button" data-panel="galleryPanel" title="历史图库">
          <svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M8 13l2.5-2.5L14 14l2-2 3 3"/><circle cx="8" cy="9" r="1"/></svg>
          <span>历史图库</span>
        </button>
        <button class="nav-item" type="button" data-panel="generatePanel" title="生图">
          <svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v4"/><path d="M12 17v4"/><path d="M3 12h4"/><path d="M17 12h4"/><path d="M6 6l2.8 2.8"/><path d="M15.2 15.2L18 18"/><path d="M18 6l-2.8 2.8"/><path d="M8.8 15.2L6 18"/></svg>
          <span>生图</span>
        </button>
        <button class="nav-item" type="button" data-panel="editPanel" title="编辑">
          <svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z"/></svg>
          <span>编辑</span>
        </button>
      </div>
      <div class="nav-group">
        <div class="nav-label">系统管理</div>
        <button class="nav-item" type="button" data-panel="settingsPanel" title="设置">
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
        <div id="galleryPagination" class="pagination-bar hidden">
          <span id="pageSummary">第 1 / 1 页</span>
          <div class="pagination-actions">
            <button id="prevPageBtn" class="btn" type="button">上一页</button>
            <button id="nextPageBtn" class="btn" type="button">下一页</button>
          </div>
        </div>
      </section>

      <section id="generatePanel" class="workspace page-panel hidden">
        <div class="workflow-layout">
          <div class="workspace-result-panel">
            <div class="result-label">结果预览</div>
            <div id="generateResultBox" class="result-box result-state-empty">暂无结果</div>
          </div>
          <form id="generateForm" class="action-panel action-panel-single compact-control-panel">
            <div class="control-stack">
              <div class="prompt-action-row">
                <button id="generateSubmit" class="btn primary" type="submit">
                  <svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v4"/><path d="M12 17v4"/><path d="M3 12h4"/><path d="M17 12h4"/><path d="M6 6l2.8 2.8"/><path d="M15.2 15.2L18 18"/><path d="M18 6l-2.8 2.8"/><path d="M8.8 15.2L6 18"/></svg>
                  生成图片
                </button>
                <div class="prompt-field">
                  <div class="prompt-label-row">
                    <label for="generatePrompt">提示词</label>
                    <button id="generateOptimizePrompt" class="btn prompt-optimize-btn" type="button" data-prompt-target="generatePrompt" data-prompt-mode="generate">
                      <svg class="nav-icon loading-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v3"/><path d="M12 18v3"/><path d="M3 12h3"/><path d="M18 12h3"/><path d="M5.6 5.6l2.1 2.1"/><path d="M16.3 16.3l2.1 2.1"/><path d="M18.4 5.6l-2.1 2.1"/><path d="M7.7 16.3l-2.1 2.1"/></svg>
                      <span>优化</span>
                    </button>
                  </div>
                  <textarea id="generatePrompt" class="auto-resize-textarea" placeholder="例如：浅色自然光下的现代别墅，干净构图，高细节。"></textarea>
                </div>
              </div>
              <div class="field-row">
                <div><label for="generateCount">数量</label><input id="generateCount" class="control" type="number" min="1" max="4" value="1"></div>
                <div><label for="generateSizePreset">生图尺寸</label><select id="generateSizePreset" class="control"><option value="auto">自动</option><option value="1024x1024">方图 1024x1024</option><option value="1024x1536">竖图 1024x1536</option><option value="1536x1024">横图 1536x1024</option><option value="2048x2048">2K 方图</option><option value="2560x1440">2K 横图</option><option value="1440x2560">2K 竖图</option><option value="custom">自定义</option></select><input id="generateCustomSize" class="control custom-size hidden" type="text" placeholder="例如 1280x1280"></div>
                <div><label for="generateQuality">质量</label><select id="generateQuality" class="control"><option>auto</option><option>low</option><option>medium</option><option>high</option></select></div>
                <div><label for="generateModeration">审核</label><select id="generateModeration" class="control"><option>low</option><option>auto</option></select></div>
              </div>
            </div>
          </form>
        </div>
      </section>

      <section id="editPanel" class="workspace page-panel hidden">
        <div class="workflow-layout">
          <div class="workspace-result-panel">
            <div class="result-label">结果预览</div>
            <div id="editResultBox" class="result-box result-state-empty">暂无结果</div>
          </div>
          <form id="editForm" class="action-panel">
            <div class="control-stack">
              <div class="prompt-action-row">
                <button id="editSubmit" class="btn primary" type="submit">
                  <svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z"/></svg>
                  编辑图片
                </button>
                <div class="prompt-field">
                  <div class="prompt-label-row">
                    <label for="editPrompt">提示词</label>
                    <button id="editOptimizePrompt" class="btn prompt-optimize-btn" type="button" data-prompt-target="editPrompt" data-prompt-mode="edit">
                      <svg class="nav-icon loading-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v3"/><path d="M12 18v3"/><path d="M3 12h3"/><path d="M18 12h3"/><path d="M5.6 5.6l2.1 2.1"/><path d="M16.3 16.3l2.1 2.1"/><path d="M18.4 5.6l-2.1 2.1"/><path d="M7.7 16.3l-2.1 2.1"/></svg>
                      <span>优化</span>
                    </button>
                  </div>
                  <textarea id="editPrompt" class="auto-resize-textarea" placeholder="例如：保留主体构图，改成柔和水彩风格，背景更明亮。"></textarea>
                </div>
              </div>
              <div class="field-row edit-options">
                <div><label for="editCount">数量</label><input id="editCount" class="control" type="number" min="1" max="4" value="1"></div>
                <div><label for="editSizePreset">编辑尺寸</label><select id="editSizePreset" class="control"><option value="auto">自动</option><option value="1024x1024">方图 1024x1024</option><option value="1024x1536">竖图 1024x1536</option><option value="1536x1024">横图 1536x1024</option><option value="2048x2048">2K 方图</option><option value="2560x1440">2K 横图</option><option value="1440x2560">2K 竖图</option><option value="custom">自定义</option></select><input id="editCustomSize" class="control custom-size hidden" type="text" placeholder="例如 1280x1280"></div>
                <div><label for="editQuality">质量</label><select id="editQuality" class="control"><option>auto</option><option>low</option><option>medium</option><option>high</option></select></div>
                <div><label for="editModeration">审核</label><select id="editModeration" class="control"><option>low</option><option>auto</option></select></div>
              </div>
            </div>
            <div id="referencePanel" class="reference-panel">
              <div class="reference-head">
                <span>参考图片</span>
                <input id="editImage" class="reference-file-input" type="file" accept="image/png,image/jpeg,image/webp" multiple>
                <label class="reference-upload-btn" for="editImage" title="上传参考图片">
                  <svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 16V4"/><path d="M7 9l5-5 5 5"/><path d="M20 16.5V19a2 2 0 01-2 2H6a2 2 0 01-2-2v-2.5"/></svg>
                  上传图片
                </label>
              </div>
              <div id="referenceThumbs" class="reference-thumbs"></div>
            </div>
          </form>
        </div>
      </section>

      <section id="settingsPanel" class="workspace page-panel hidden">
        <div class="section-head">
          <div>
            <h1>设置</h1>
            <p class="muted">管理提示词优化模型和当前浏览器的图库缓存展示方式。</p>
          </div>
        </div>
        <div class="settings-grid">
          <div class="settings-card">
            <label>提示词优化模型</label>
            <div class="settings-form-grid">
              <div>
                <label for="promptOptimizerModel">模型名称</label>
                <input id="promptOptimizerModel" class="control" type="text" placeholder="例如 gpt-5.4-mini">
              </div>
              <div>
                <label for="promptOptimizerBaseUrl">Base URL</label>
                <input id="promptOptimizerBaseUrl" class="control" type="text" placeholder="例如 https://api.openai.com/v1">
              </div>
              <div>
                <label for="promptOptimizerApiKey">API Key</label>
                <input id="promptOptimizerApiKey" class="control" type="password" placeholder="留空则保留已保存 Key" autocomplete="off">
              </div>
            </div>
            <div class="settings-actions">
              <button id="savePromptOptimizerSettingsBtn" class="btn primary" type="button">保存优化设置</button>
              <span id="promptOptimizerKeyState" class="muted">API Key 未保存</span>
            </div>
            <p class="muted">点击生图或编辑页面的“优化”按钮时会调用这里配置的 OpenAI 兼容 Chat Completions 模型。</p>
          </div>
          <div class="settings-card">
            <label for="galleryPageSizeInput">历史图库每页显示数量</label>
            <div class="settings-inline">
              <input id="galleryPageSizeInput" class="control" type="number" min="5" max="200" step="1">
              <button id="saveGalleryPageSizeBtn" class="btn" type="button">保存数量</button>
            </div>
            <p class="muted">用于控制当前浏览器历史图库分页数量，范围 5-200。</p>
          </div>
          <div class="settings-card">
            <label for="galleryColumnCountInput">历史图库显示列数</label>
            <div class="settings-inline">
              <select id="galleryColumnMode" class="control" aria-label="历史图库列数模式">
                <option value="auto">自动列数</option>
                <option value="fixed">固定列数</option>
              </select>
              <input id="galleryColumnCountInput" class="control" type="number" min="2" max="8" step="1">
              <button id="saveGalleryColumnCountBtn" class="btn" type="button">保存列数</button>
            </div>
            <p class="muted">自动列数会按窗口宽度自适应；固定列数范围 2-8。</p>
          </div>
          <div id="cacheInfo" class="cache-info">
            <div class="detail-row"><span>缩略图缓存</span><strong id="thumbnailCacheInfo">0 张 / 0 B</strong></div>
            <div class="detail-row"><span>原图缓存</span><strong id="originalCacheInfo">0 张 / 0 B</strong></div>
            <div class="detail-row"><span>总占用空间</span><strong id="cacheBytes">0 B</strong></div>
          </div>
          <div class="settings-actions">
            <button id="clearLocalCacheBtn" class="btn" type="button">清空本地图片缓存</button>
          </div>
        </div>
      </section>
    </main>

    <aside class="preview">
      <div class="preview-head">
        <h2>预览</h2>
        <div class="settings-actions">
          <button id="viewOriginalBtn" class="btn" type="button" disabled>查看原图</button>
          <button id="deleteImageBtn" class="btn danger" type="button" disabled>删除</button>
        </div>
      </div>
      <div id="previewBox" class="preview-box empty-preview" title="双击查看原图">请选择一张图片</div>
      <h3 id="detailTitle" class="detail-title">暂无选中图片</h3>
      <div class="detail-row detail-row-block"><span>提示词</span><strong id="detailPrompt">-</strong></div>
      <div class="detail-row"><span>图片尺寸</span><strong id="detailGenerationSize">-</strong></div>
      <div class="detail-row"><span>格式</span><strong id="detailType">-</strong></div>
      <div class="detail-row"><span>大小</span><strong id="detailSize">-</strong></div>
      <div class="detail-row"><span>更新时间</span><strong id="detailTime">-</strong></div>
    </aside>
  </div>

  <div id="toast" class="toast hidden"></div>

  <script>
    const GALLERY_PAGE_SIZE_KEY = "openaiImageAdminGalleryPageSize";
    const GALLERY_COLUMN_MODE_KEY = "openaiImageAdminGalleryColumnMode";
    const GALLERY_COLUMN_COUNT_KEY = "openaiImageAdminGalleryColumnCount";
    const DEFAULT_GALLERY_PAGE_SIZE = 30;
    const MIN_GALLERY_PAGE_SIZE = 5;
    const MAX_GALLERY_PAGE_SIZE = 200;
    const DEFAULT_GALLERY_COLUMN_COUNT = 4;
    const MIN_GALLERY_COLUMN_COUNT = 2;
    const MAX_GALLERY_COLUMN_COUNT = 8;
    const PROMPT_OPTIMIZER_API_KEY_MASK = "********";
    const state = {
      token: localStorage.getItem("openaiImageAdminToken") || "",
      images: [],
      selected: null,
      activePanel: "galleryPanel",
      referenceImageFiles: [],
      imageCacheDb: null,
      galleryPage: 1,
      galleryPageSize: DEFAULT_GALLERY_PAGE_SIZE,
      galleryColumnMode: "auto",
      galleryColumnCount: DEFAULT_GALLERY_COLUMN_COUNT,
    };
    const $ = (id) => document.getElementById(id);
    const IMAGE_CACHE_DB_NAME = "openai-image-admin-cache";
    const IMAGE_CACHE_STORE = "images";
    const REFERENCE_IMAGE_PREVIEW_URLS = new WeakMap();
    let resizeRenderTimer = null;

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

    function showPanel(panelId) {
      state.activePanel = panelId;
      document.querySelectorAll(".page-panel, #galleryPanel").forEach((panel) => {
        panel.classList.toggle("hidden", panel.id !== panelId);
      });
      document.querySelectorAll(".nav-item").forEach((item) => {
        item.classList.toggle("active", item.dataset.panel === panelId);
      });
      updatePreviewVisibility(panelId);
      resizePromptTextareas();
    }

    function updatePreviewVisibility(panelId) {
      document.body.classList.toggle("preview-hidden", panelId !== "galleryPanel");
    }

    function resizePromptTextarea(textarea) {
      if (!textarea) return;
      textarea.style.height = "auto";
      textarea.style.height = `${textarea.scrollHeight}px`;
    }

    function resizePromptTextareas() {
      ["generatePrompt", "editPrompt"].forEach((id) => resizePromptTextarea($(id)));
    }

    function escapeAttribute(value) {
      return escapeHtml(value).replace(/`/g, "&#96;");
    }

    function imageUrl(image) {
      return `${image.url}?v=${image.modified_at}`;
    }

    async function optimizePrompt(button) {
      const textarea = $(button.dataset.promptTarget);
      const prompt = textarea.value.trim();
      if (!prompt) {
        showToast("请先输入提示词");
        textarea.focus();
        return;
      }

      const label = button.querySelector("span");
      const originalLabel = label.textContent;
      button.disabled = true;
      textarea.disabled = true;
      button.classList.add("is-loading");
      label.textContent = "优化中";
      try {
        const data = await apiFetch("/api/optimize-prompt", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            prompt,
            mode: button.dataset.promptMode || "generate",
          }),
        });
        textarea.value = data.prompt || "";
        resizePromptTextarea(textarea);
        showToast("提示词已优化");
      } catch (error) {
        showToast(error.message);
      } finally {
        label.textContent = originalLabel;
        button.classList.remove("is-loading");
        textarea.disabled = false;
        button.disabled = false;
        textarea.focus();
      }
    }

    // 缩略图和原图必须拆成两个缓存键：图库滚动只保存小图，用户点开预览后才写入原图。
    function thumbnailCacheKey(image) {
      return `thumb:v2:${image.name}:${image.modified_at}:${image.size_bytes}`;
    }

    function originalCacheKey(image) {
      return `original:${image.name}:${image.modified_at}:${image.size_bytes}`;
    }

    function imageCacheKeys(image) {
      return [thumbnailCacheKey(image), originalCacheKey(image)];
    }

    function openImageCache() {
      if (state.imageCacheDb) return Promise.resolve(state.imageCacheDb);
      if (!("indexedDB" in window)) return Promise.resolve(null);
      return new Promise((resolve) => {
        const request = indexedDB.open(IMAGE_CACHE_DB_NAME, 1);
        request.onupgradeneeded = () => {
          const db = request.result;
          if (!db.objectStoreNames.contains(IMAGE_CACHE_STORE)) {
            db.createObjectStore(IMAGE_CACHE_STORE, { keyPath: "key" });
          }
        };
        request.onsuccess = () => {
          state.imageCacheDb = request.result;
          resolve(state.imageCacheDb);
        };
        request.onerror = () => resolve(null);
      });
    }

    async function readCachedImage(key) {
      const db = await openImageCache();
      if (!db) return null;
      return new Promise((resolve) => {
        const request = db.transaction(IMAGE_CACHE_STORE, "readonly").objectStore(IMAGE_CACHE_STORE).get(key);
        request.onsuccess = () => {
          const record = request.result || null;
          resolve(record);
        };
        request.onerror = () => resolve(null);
      });
    }

    // 浏览器本地缓存固定使用 IndexedDB，不再暴露自定义目录选择，避免不同浏览器权限模型导致路径不可控。
    async function writeCachedImage(record) {
      const db = await openImageCache();
      if (db) {
        await new Promise((resolve) => {
          const request = db.transaction(IMAGE_CACHE_STORE, "readwrite").objectStore(IMAGE_CACHE_STORE).put(record);
          request.onsuccess = () => resolve();
          request.onerror = () => resolve();
        });
      }
    }

    async function createThumbnailBlob(sourceBlob) {
      // 历史图库会直接承担浏览和挑选图片的任务，因此缩略图需要保留足够清晰度。
      const bitmap = await createImageBitmap(sourceBlob);
      const maxSide = 960;
      const scale = Math.min(1, maxSide / Math.max(bitmap.width, bitmap.height));
      const width = Math.max(1, Math.round(bitmap.width * scale));
      const height = Math.max(1, Math.round(bitmap.height * scale));
      const canvas = document.createElement("canvas");
      canvas.width = width;
      canvas.height = height;
      const context = canvas.getContext("2d");
      context.imageSmoothingQuality = "high";
      context.drawImage(bitmap, 0, 0, width, height);
      bitmap.close();
      return new Promise((resolve) => {
        canvas.toBlob((blob) => resolve(blob || sourceBlob), "image/webp", 0.92);
      });
    }

    async function getCachedThumbnailUrl(image) {
      const key = thumbnailCacheKey(image);
      const cached = await readCachedImage(key);
      if (cached && cached.blob) return URL.createObjectURL(cached.blob);
      const response = await fetch(imageUrl(image));
      const originalBlob = await response.blob();
      const blob = await createThumbnailBlob(originalBlob);
      await writeCachedImage({
        key,
        name: image.name,
        size: blob.size,
        blob,
        kind: "thumbnail",
        cached_at: Date.now(),
      });
      refreshCacheInfo().catch(() => {});
      return URL.createObjectURL(blob);
    }

    async function getCachedOriginalUrl(image) {
      // 原图体积通常明显大于缩略图，因此只在用户双击查看原图时缓存。
      const key = originalCacheKey(image);
      const cached = await readCachedImage(key);
      if (cached && cached.blob) return URL.createObjectURL(cached.blob);
      const response = await fetch(imageUrl(image));
      const blob = await response.blob();
      await writeCachedImage({
        key,
        name: image.name,
        size: blob.size,
        blob,
        kind: "original",
        cached_at: Date.now(),
      });
      refreshCacheInfo().catch(() => {});
      return URL.createObjectURL(blob);
    }

    async function listCachedImages() {
      const db = await openImageCache();
      if (!db) return [];
      return new Promise((resolve) => {
        const request = db.transaction(IMAGE_CACHE_STORE, "readonly").objectStore(IMAGE_CACHE_STORE).getAll();
        request.onsuccess = () => resolve(request.result || []);
        request.onerror = () => resolve([]);
      });
    }

    async function clearLocalImageCache() {
      const db = await openImageCache();
      if (!db) return;
      await new Promise((resolve) => {
        const request = db.transaction(IMAGE_CACHE_STORE, "readwrite").objectStore(IMAGE_CACHE_STORE).openCursor();
        request.onsuccess = () => {
          const cursor = request.result;
          if (!cursor) {
            resolve();
            return;
          }
          cursor.delete();
          cursor.continue();
        };
        request.onerror = () => resolve();
      });
      await refreshCacheInfo();
    }

    async function clearImageCacheRecords(image) {
      const db = await openImageCache();
      if (!db || !image) return;
      const keys = imageCacheKeys(image);
      await new Promise((resolve) => {
        const store = db.transaction(IMAGE_CACHE_STORE, "readwrite").objectStore(IMAGE_CACHE_STORE);
        let pending = keys.length;
        const finish = () => {
          pending -= 1;
          if (pending <= 0) resolve();
        };
        keys.forEach((key) => {
          const request = store.delete(key);
          request.onsuccess = finish;
          request.onerror = finish;
        });
      });
      await refreshCacheInfo();
    }

    async function refreshCacheInfo() {
      const records = await listCachedImages();
      const thumbnailRecords = records.filter((item) => item.kind === "thumbnail");
      const originalRecords = records.filter((item) => item.kind === "original");
      const thumbnailBytes = thumbnailRecords.reduce((sum, item) => sum + (item.size || 0), 0);
      const originalBytes = originalRecords.reduce((sum, item) => sum + (item.size || 0), 0);
      const totalBytes = records.reduce((sum, item) => sum + (item.size || 0), 0);
      $("thumbnailCacheInfo").textContent = `${thumbnailRecords.length} 张 / ${formatBytes(thumbnailBytes)}`;
      $("originalCacheInfo").textContent = `${originalRecords.length} 张 / ${formatBytes(originalBytes)}`;
      $("cacheBytes").textContent = formatBytes(totalBytes);
      $("cacheSummary").textContent = `${thumbnailRecords.length} 张缩略图、${originalRecords.length} 张原图，占用 ${formatBytes(totalBytes)}`;
    }

    function renderPromptOptimizerSettings(settings) {
      $("promptOptimizerModel").value = settings.model || "";
      $("promptOptimizerBaseUrl").value = settings.base_url || "";
      $("promptOptimizerApiKey").value = settings.has_api_key ? PROMPT_OPTIMIZER_API_KEY_MASK : "";
      $("promptOptimizerKeyState").textContent = settings.has_api_key ? "API Key 已保存" : "API Key 未保存";
    }

    async function loadPromptOptimizerSettings() {
      const settings = await apiFetch("/api/prompt-optimizer-settings");
      renderPromptOptimizerSettings(settings);
    }

    async function savePromptOptimizerSettings() {
      const button = $("savePromptOptimizerSettingsBtn");
      button.disabled = true;
      try {
        const settings = await apiFetch("/api/prompt-optimizer-settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            model: $("promptOptimizerModel").value,
            base_url: $("promptOptimizerBaseUrl").value,
            api_key: $("promptOptimizerApiKey").value === PROMPT_OPTIMIZER_API_KEY_MASK ? "" : $("promptOptimizerApiKey").value,
          }),
        });
        renderPromptOptimizerSettings(settings);
        showToast("提示词优化设置已保存");
      } catch (error) {
        showToast(error.message);
      } finally {
        button.disabled = false;
      }
    }

    function resolveSizeValue(presetId, customId) {
      const preset = $(presetId).value;
      if (preset === "custom") return $(customId).value.trim();
      return preset;
    }

    function syncCustomSize(presetId, customId) {
      $(customId).classList.toggle("hidden", $(presetId).value !== "custom");
    }

    function formatPrompt(value) {
      return String(value || "").trim() || "未记录";
    }

    function formatImageDimensions(width, height) {
      if (!width || !height) return "未记录";
      return `${width}×${height}`;
    }

    function clampGalleryPageSize(value) {
      const numericValue = Number(value);
      if (!Number.isFinite(numericValue)) return DEFAULT_GALLERY_PAGE_SIZE;
      return Math.max(MIN_GALLERY_PAGE_SIZE, Math.min(MAX_GALLERY_PAGE_SIZE, Math.floor(numericValue)));
    }

    function clampGalleryColumnCount(value) {
      const numericValue = Number(value);
      if (!Number.isFinite(numericValue)) return DEFAULT_GALLERY_COLUMN_COUNT;
      return Math.max(MIN_GALLERY_COLUMN_COUNT, Math.min(MAX_GALLERY_COLUMN_COUNT, Math.floor(numericValue)));
    }

    function applyGalleryColumnSetting() {
      const gallery = $("gallery");
      gallery.style.setProperty("--gallery-column-count", String(resolveGalleryColumnCount()));
    }

    function resolveGalleryColumnCount() {
      if (state.galleryColumnMode === "fixed") return state.galleryColumnCount;
      const galleryWidth = $("gallery").clientWidth || 0;
      if (!galleryWidth) return 1;
      const targetColumnWidth = 220;
      const columnGap = 14;
      const estimatedColumns = Math.floor((galleryWidth + columnGap) / (targetColumnWidth + columnGap));
      return Math.max(1, estimatedColumns);
    }

    function distributeImagesByRows(images) {
      const columnCount = resolveGalleryColumnCount();
      const columns = Array.from({ length: columnCount }, () => []);
      images.forEach((image, index) => {
        // 这里按行优先分发：排序后的第 1-N 张先铺满第一行，再进入下一行。
        // 每列内部仍是纵向自然流，保留图片自适应高度和上下紧密衔接的瀑布流观感。
        columns[index % columnCount].push(image);
      });
      return columns;
    }

    function loadGalleryPageSizeSetting() {
      state.galleryPageSize = clampGalleryPageSize(localStorage.getItem(GALLERY_PAGE_SIZE_KEY));
      $("galleryPageSizeInput").value = String(state.galleryPageSize);
    }

    function loadGalleryColumnSetting() {
      const storedMode = localStorage.getItem(GALLERY_COLUMN_MODE_KEY);
      state.galleryColumnMode = storedMode === "fixed" ? "fixed" : "auto";
      state.galleryColumnCount = clampGalleryColumnCount(localStorage.getItem(GALLERY_COLUMN_COUNT_KEY));
      $("galleryColumnMode").value = state.galleryColumnMode;
      $("galleryColumnCountInput").value = String(state.galleryColumnCount);
      syncGalleryColumnCountInput();
      applyGalleryColumnSetting();
    }

    function saveGalleryPageSizeSetting() {
      // 分页数量只影响当前浏览器的后台展示，不写入插件配置，避免后台重启或配置迁移。
      state.galleryPageSize = clampGalleryPageSize($("galleryPageSizeInput").value);
      localStorage.setItem(GALLERY_PAGE_SIZE_KEY, String(state.galleryPageSize));
      $("galleryPageSizeInput").value = String(state.galleryPageSize);
      state.galleryPage = 1;
      renderGallery();
      showToast("每页显示数量已保存");
    }

    function saveGalleryColumnSetting() {
      state.galleryColumnMode = $("galleryColumnMode").value === "fixed" ? "fixed" : "auto";
      state.galleryColumnCount = clampGalleryColumnCount($("galleryColumnCountInput").value);
      localStorage.setItem(GALLERY_COLUMN_MODE_KEY, state.galleryColumnMode);
      localStorage.setItem(GALLERY_COLUMN_COUNT_KEY, String(state.galleryColumnCount));
      $("galleryColumnMode").value = state.galleryColumnMode;
      $("galleryColumnCountInput").value = String(state.galleryColumnCount);
      syncGalleryColumnCountInput();
      applyGalleryColumnSetting();
      renderGallery();
      showToast("历史图库列数已保存");
    }

    function syncGalleryColumnCountInput() {
      $("galleryColumnCountInput").disabled = $("galleryColumnMode").value !== "fixed";
    }

    function updatePreviewImageDimensions(image, previewImage) {
      // 预览区需要展示图片文件真实像素尺寸，而不是接口请求里的 auto/square 等生成参数。
      if (!image || state.selected?.name !== image.name) return;
      $("detailGenerationSize").textContent = formatImageDimensions(previewImage.naturalWidth, previewImage.naturalHeight);
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

    function paginatedImages(images) {
      const totalPages = Math.max(1, Math.ceil(images.length / state.galleryPageSize));
      state.galleryPage = Math.max(1, Math.min(totalPages, state.galleryPage));
      const startIndex = (state.galleryPage - 1) * state.galleryPageSize;
      return {
        pageImages: images.slice(startIndex, startIndex + state.galleryPageSize),
        startIndex,
        totalPages,
      };
    }

    function updateGalleryPagination(totalImages, startIndex, pageImages, totalPages) {
      const pagination = $("galleryPagination");
      if (!totalImages) {
        pagination.classList.add("hidden");
        return;
      }
      pagination.classList.toggle("hidden", totalPages <= 1);
      const endIndex = startIndex + pageImages.length;
      $("pageSummary").textContent = `第 ${state.galleryPage} / ${totalPages} 页，显示 ${startIndex + 1}-${endIndex} 张，共 ${totalImages} 张`;
      $("prevPageBtn").disabled = state.galleryPage <= 1;
      $("nextPageBtn").disabled = state.galleryPage >= totalPages;
    }

    function renderGallery() {
      const gallery = $("gallery");
      const images = filteredImages();
      $("imageCount").textContent = state.images.length;
      refreshCacheInfo().catch(() => {});
      if (!images.length) {
        gallery.innerHTML = '<div class="empty-gallery gallery-empty">暂无图片，可切换到生图页面创建新图片。</div>';
        updateGalleryPagination(0, 0, [], 1);
        return;
      }
      const { pageImages, startIndex, totalPages } = paginatedImages(images);
      updateGalleryPagination(images.length, startIndex, pageImages, totalPages);
      const columns = distributeImagesByRows(pageImages);
      gallery.style.setProperty("--gallery-column-count", String(columns.length || 1));
      gallery.innerHTML = columns.map((columnImages) => `
        <div class="gallery-column">
          ${columnImages.map((image) => {
        const safeName = escapeHtml(image.name);
        const safeAttrName = escapeAttribute(image.name);
        return `
        <button class="image-card ${state.selected && state.selected.name === image.name ? "selected" : ""}" type="button" data-name="${safeAttrName}">
          <span class="image-card-actions">
            <span class="image-card-action delete-image-action" data-name="${safeAttrName}" title="删除图片" aria-label="删除图片">
              <svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v5"/><path d="M14 11v5"/></svg>
            </span>
          </span>
          <img class="thumb" data-cache-key="${escapeAttribute(thumbnailCacheKey(image))}" alt="${safeAttrName}" loading="lazy">
          <div class="card-meta">
            <div class="card-name" title="${safeAttrName}">${safeName}</div>
            <div class="card-sub"><span>${formatBytes(image.size_bytes)}</span><span>${new Date(image.modified_at * 1000).toLocaleString()}</span></div>
          </div>
        </button>
      `;
      }).join("")}
        </div>
      `).join("");
      gallery.querySelectorAll(".image-card").forEach((card) => {
        card.addEventListener("click", () => selectImage(card.dataset.name).catch((error) => showToast(error.message)));
      });
      gallery.querySelectorAll(".delete-image-action").forEach((button) => {
        button.addEventListener("click", (event) => {
          event.stopPropagation();
          deleteImageByName(button.dataset.name).catch((error) => showToast(error.message));
        });
      });
      hydrateGalleryImages(pageImages);
    }

    function updateGallerySelection() {
      // 点击缩略图只需要切换选中态；避免重建流式图库导致浏览器重新计算滚动位置。
      $("gallery").querySelectorAll(".image-card").forEach((card) => {
        card.classList.toggle("selected", state.selected && card.dataset.name === state.selected.name);
      });
    }

    async function hydrateGalleryImages(images) {
      for (const image of images) {
        const thumb = Array.from($("gallery").querySelectorAll(".thumb")).find((item) => item.dataset.cacheKey === thumbnailCacheKey(image));
        if (!thumb) continue;
        try {
          thumb.src = await getCachedThumbnailUrl(image);
        } catch (error) {
          thumb.src = imageUrl(image);
        }
      }
    }

    function clearPreview() {
      state.selected = null;
      $("previewBox").classList.add("empty-preview");
      $("previewBox").textContent = "请选择一张图片";
      $("detailTitle").textContent = "暂无选中图片";
      $("detailPrompt").textContent = "-";
      $("detailGenerationSize").textContent = "-";
      $("detailType").textContent = "-";
      $("detailSize").textContent = "-";
      $("detailTime").textContent = "-";
      $("viewOriginalBtn").disabled = true;
      $("deleteImageBtn").disabled = true;
    }

    async function selectImage(name) {
      const image = state.images.find((item) => item.name === name);
      if (!image) return;
      state.selected = image;
      const previewBox = $("previewBox");
      previewBox.classList.remove("empty-preview");
      previewBox.textContent = "图片加载中...";
      $("detailTitle").textContent = image.name;
      $("detailPrompt").textContent = formatPrompt(image.prompt);
      $("detailGenerationSize").textContent = "读取中...";
      $("detailType").textContent = image.mime_type;
      $("detailSize").textContent = formatBytes(image.size_bytes);
      $("detailTime").textContent = new Date(image.modified_at * 1000).toLocaleString();
      $("viewOriginalBtn").disabled = false;
      $("deleteImageBtn").disabled = false;
      updateGallerySelection();
      const previewImage = document.createElement("img");
      previewImage.alt = image.name;
      previewImage.addEventListener("load", () => updatePreviewImageDimensions(image, previewImage), { once: true });
      previewImage.addEventListener("error", () => {
        if (state.selected?.name === image.name) $("detailGenerationSize").textContent = "未记录";
      }, { once: true });
      previewImage.src = imageUrl(image);
      previewBox.replaceChildren(previewImage);
    }

    function setResultState(targetId, stateName, message = "") {
      const box = $(targetId);
      box.classList.remove("result-state-empty", "result-state-loading", "result-state-success", "result-state-error", "empty-gallery");
      box.classList.add(`result-state-${stateName}`);
      if (stateName === "empty") {
        box.textContent = message || "暂无结果";
        return;
      }
      if (stateName === "loading") {
        box.innerHTML = `
          <div class="result-status">
            <span class="result-spinner" aria-hidden="true"></span>
            <strong>${escapeHtml(message || "正在生成图片，请稍候...")}</strong>
          </div>
        `;
        return;
      }
      if (stateName === "error") {
        box.innerHTML = `
          <div class="result-status" role="alert">
            <strong>生成失败</strong>
            <span>${escapeHtml(message || "图片任务失败，请查看错误信息后重试。")}</span>
          </div>
        `;
      }
    }

    function renderResultImages(targetId, imageNames) {
      // 生成/编辑完成后，把本次结果留在大预览区，减少用户在图库和表单之间来回找图。
      const box = $(targetId);
      const names = Array.isArray(imageNames) ? imageNames.filter(Boolean) : [];
      const images = names
        .map((name) => state.images.find((item) => item.name === name))
        .filter(Boolean);

      if (!images.length) {
        setResultState(targetId, "empty");
        return;
      }

      setResultState(targetId, "success");
      box.innerHTML = images.map((image) => `
        <img src="${escapeAttribute(imageUrl(image))}" alt="${escapeAttribute(image.name)}" title="双击查看原图">
      `).join("");
      box.querySelectorAll("img").forEach((imageNode, index) => {
        imageNode.addEventListener("dblclick", () => window.open(imageUrl(images[index]), "_blank", "noopener"));
      });
    }

    async function loadImages(preferredName = "") {
      const data = await apiFetch("/api/images");
      state.images = data.images || [];
      if (preferredName) {
        await selectImage(preferredName);
      } else {
        clearPreview();
        renderGallery();
      }
    }

    function resetGalleryPageAndRender() {
      state.galleryPage = 1;
      renderGallery();
    }

    function renderReferenceThumbnails() {
      // 多参考图只在编辑页右侧显示缩略图，真正提交时仍按文件对象逐张追加到 FormData。
      const files = state.referenceImageFiles;
      $("referenceThumbs").innerHTML = files.map((file, index) => {
        if (!REFERENCE_IMAGE_PREVIEW_URLS.has(file)) {
          REFERENCE_IMAGE_PREVIEW_URLS.set(file, URL.createObjectURL(file));
        }
        const previewUrl = REFERENCE_IMAGE_PREVIEW_URLS.get(file);
        return `
          <div class="reference-thumb">
            <img src="${escapeAttribute(previewUrl)}" alt="参考图片 ${index + 1}">
            <button class="reference-thumb-remove" type="button" data-index="${index}" title="移除参考图片 ${index + 1}" aria-label="移除参考图片 ${index + 1}">
              <svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6L6 18"/><path d="M6 6l12 12"/></svg>
            </button>
          </div>
        `;
      }).join("");
      $("referenceThumbs").querySelectorAll(".reference-thumb-remove").forEach((button) => {
        button.addEventListener("click", () => removeReferenceImageFile(Number(button.dataset.index)));
      });
    }

    function removeReferenceImageFile(index) {
      const file = state.referenceImageFiles[index];
      if (!file) return;
      const previewUrl = REFERENCE_IMAGE_PREVIEW_URLS.get(file);
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      REFERENCE_IMAGE_PREVIEW_URLS.delete(file);
      state.referenceImageFiles.splice(index, 1);
      renderReferenceThumbnails();
    }

    function addReferenceImageFile(file) {
      if (!file || !file.type.startsWith("image/")) {
        showToast("请提供图片文件");
        return;
      }
      state.referenceImageFiles.push(file);
      renderReferenceThumbnails();
    }

    function handlePasteImage(event) {
      const items = event.clipboardData && event.clipboardData.items;
      if (!items) return;
      let addedCount = 0;
      for (const item of items) {
        if (item.type && item.type.startsWith("image/")) {
          const file = item.getAsFile();
          if (file) {
            event.preventDefault();
            addReferenceImageFile(file);
            addedCount += 1;
          }
        }
      }
      if (addedCount > 1) showToast(`已添加 ${addedCount} 张参考图片`);
    }

    async function openSelectedOriginalImage() {
      if (!state.selected) return;
      try {
        const originalUrl = await getCachedOriginalUrl(state.selected);
        window.open(originalUrl, "_blank", "noopener");
      } catch (error) {
        window.open(imageUrl(state.selected), "_blank", "noopener");
      }
    }

    async function deleteImageByName(name) {
      const image = state.images.find((item) => item.name === name);
      if (!image) return;
      if (!window.confirm(`确定删除图片 ${image.name} 吗？服务器缓存文件也会被删除。`)) return;
      await apiFetch(`/api/images/${encodeURIComponent(image.name)}`, { method: "DELETE" });
      await clearImageCacheRecords(image);
      state.images = state.images.filter((item) => item.name !== image.name);
      if (state.selected && state.selected.name === image.name) clearPreview();
      renderGallery();
      showToast("图片已删除");
    }

    async function deleteSelectedImage() {
      if (!state.selected) return;
      await deleteImageByName(state.selected.name);
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
        loadGalleryPageSizeSetting();
        loadGalleryColumnSetting();
        await loadPromptOptimizerSettings();
        await loadImages();
      } catch (error) {
        showToast(error.message);
      }
    });

    $("refreshBtn").addEventListener("click", () => loadImages().catch((error) => showToast(error.message)));
    $("searchInput").addEventListener("input", resetGalleryPageAndRender);
    $("typeFilter").addEventListener("change", resetGalleryPageAndRender);
    $("sortFilter").addEventListener("change", resetGalleryPageAndRender);
    $("prevPageBtn").addEventListener("click", () => {
      state.galleryPage = Math.max(1, state.galleryPage - 1);
      renderGallery();
    });
    $("nextPageBtn").addEventListener("click", () => {
      state.galleryPage += 1;
      renderGallery();
    });
    $("saveGalleryPageSizeBtn").addEventListener("click", saveGalleryPageSizeSetting);
    $("galleryColumnMode").addEventListener("change", syncGalleryColumnCountInput);
    $("saveGalleryColumnCountBtn").addEventListener("click", saveGalleryColumnSetting);
    $("savePromptOptimizerSettingsBtn").addEventListener("click", savePromptOptimizerSettings);
    $("generateSizePreset").addEventListener("change", () => syncCustomSize("generateSizePreset", "generateCustomSize"));
    $("editSizePreset").addEventListener("change", () => syncCustomSize("editSizePreset", "editCustomSize"));
    $("generateOptimizePrompt").addEventListener("click", () => optimizePrompt($("generateOptimizePrompt")));
    $("editOptimizePrompt").addEventListener("click", () => optimizePrompt($("editOptimizePrompt")));
    ["generatePrompt", "editPrompt"].forEach((id) => {
      $(id).addEventListener("input", () => resizePromptTextarea($(id)));
    });
    $("editImage").addEventListener("change", () => {
      Array.from($("editImage").files || []).forEach(addReferenceImageFile);
      $("editImage").value = "";
    });
    $("clearLocalCacheBtn").addEventListener("click", async () => {
      await clearLocalImageCache();
      showToast("本地图片缓存已清空");
    });
    document.addEventListener("paste", (event) => {
      if (state.activePanel === "editPanel") handlePasteImage(event);
    });
    document.querySelectorAll("[data-panel]").forEach((button) => {
      button.addEventListener("click", () => {
        showPanel(button.dataset.panel);
      });
    });
    $("previewBox").addEventListener("dblclick", openSelectedOriginalImage);
    $("viewOriginalBtn").addEventListener("click", openSelectedOriginalImage);
    $("deleteImageBtn").addEventListener("click", () => deleteSelectedImage().catch((error) => showToast(error.message)));
    window.addEventListener("resize", () => {
      window.clearTimeout(resizeRenderTimer);
      resizeRenderTimer = window.setTimeout(() => {
        if (state.activePanel === "galleryPanel") renderGallery();
        resizePromptTextareas();
      }, 120);
    });

    $("generateForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const submit = $("generateSubmit");
      submit.disabled = true;
      setResultState("generateResultBox", "loading", "正在生成图片，请稍候...");
      try {
        const resultImages = [];
        const count = Math.max(1, Math.min(4, Number($("generateCount").value || 1)));
        for (let index = 0; index < count; index += 1) {
          const data = await apiFetch("/api/generate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              prompt: $("generatePrompt").value,
              size: resolveSizeValue("generateSizePreset", "generateCustomSize"),
              quality: $("generateQuality").value,
              moderation: $("generateModeration").value,
            }),
          });
          if (data.image && data.image.name) resultImages.push(data.image.name);
        }
        showToast("图片生成完成");
        const lastImage = resultImages[resultImages.length - 1] || "";
        await loadImages(lastImage);
        renderResultImages("generateResultBox", resultImages);
      } catch (error) {
        setResultState("generateResultBox", "error", error.message);
        showToast(error.message);
      } finally {
        submit.disabled = false;
      }
    });

    $("editForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const submit = $("editSubmit");
      submit.disabled = true;
      try {
        const files = state.referenceImageFiles;
        if (!files.length) {
          setResultState("editResultBox", "error", "请先上传或粘贴参考图片");
          showToast("请先上传或粘贴参考图片");
          return;
        }
        setResultState("editResultBox", "loading", "正在编辑图片，请稍候...");
        const resultImages = [];
        const count = Math.max(1, Math.min(4, Number($("editCount").value || 1)));
        for (let index = 0; index < count; index += 1) {
          const body = new FormData();
          body.append("prompt", $("editPrompt").value);
          body.append("size", resolveSizeValue("editSizePreset", "editCustomSize"));
          body.append("quality", $("editQuality").value);
          body.append("moderation", $("editModeration").value);
          files.forEach((file) => {
            body.append("image", file);
          });
          const data = await apiFetch("/api/edit", { method: "POST", body });
          if (data.image && data.image.name) resultImages.push(data.image.name);
        }
        const lastImage = resultImages[resultImages.length - 1] || "";
        showToast("图片编辑完成");
        await loadImages(lastImage);
        renderResultImages("editResultBox", resultImages);
      } catch (error) {
        setResultState("editResultBox", "error", error.message);
        showToast(error.message);
      } finally {
        submit.disabled = false;
      }
    });

    if (state.token) {
      $("loginMask").classList.add("hidden");
      loadGalleryPageSizeSetting();
      loadGalleryColumnSetting();
      loadPromptOptimizerSettings().catch((error) => showToast(error.message));
      loadImages().catch(() => $("loginMask").classList.remove("hidden"));
    } else {
      loadGalleryPageSizeSetting();
      loadGalleryColumnSetting();
    }
    resizePromptTextareas();
  </script>
</body>
</html>
"""
