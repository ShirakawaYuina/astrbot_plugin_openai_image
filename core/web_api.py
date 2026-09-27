"""内置 WebUI 页面的后端接口。"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .storage.cache_cleaner import IMAGE_SUFFIXES
from .storage.thumbnail_store import ThumbnailStore

PLUGIN_NAME = "astrbot_plugin_openai_image"
PROMPT_OPTIMIZER_SETTINGS_FILE_NAME = "prompt_optimizer_settings.json"
LOG_PROMPT_MAX_LENGTH = 120
GALLERY_SORT_FIELDS = ("newest", "oldest", "name")
GALLERY_MAX_PAGE_SIZE = 60
GALLERY_DEFAULT_PAGE_SIZE = 24
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


@dataclass(frozen=True, slots=True)
class PromptOptimizerSettings:
    """提示词优化模型配置。

    该功能走独立的 OpenAI 兼容 Chat Completions 接口，避免和图片接口供应商混用；
    任一关键字段缺失时显式报错，提醒用户到页面设置页补齐配置。
    """

    model: str
    base_url: str
    api_key: str
    timeout_seconds: int

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> PromptOptimizerSettings:
        """从页面保存的配置读取提示词优化模型参数，并校验必填字段。"""

        model = str(config.get("prompt_optimizer_model", "") or "").strip()
        base_url = (
            str(config.get("prompt_optimizer_base_url", "") or "").strip().rstrip("/")
        )
        api_key = str(config.get("prompt_optimizer_api_key", "") or "").strip()
        if not model:
            raise ValueError("请先在页面设置中配置提示词优化模型名称")
        if not base_url:
            raise ValueError("请先在页面设置中配置提示词优化 Base URL")
        if not api_key:
            raise ValueError("请先在页面设置中配置提示词优化 API Key")

        return cls(
            model=model,
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=max(
                5, int(config.get("request_timeout_seconds", 180) or 180)
            ),
        )


class PromptOptimizerSettingsStore:
    """读写页面设置 Tab 专用的提示词优化配置。"""

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
        """保存前端提交的模型配置，空 API Key 表示保留旧密钥。

        Args:
            payload: 页面提交的 model、base_url、api_key 字段。

        Returns:
            保存后的公开配置摘要。
        """

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
        / PLUGIN_NAME
        / PROMPT_OPTIMIZER_SETTINGS_FILE_NAME
    )


class ImageLibrary:
    """读取插件缓存图库，并限制文件访问只发生在缓存目录内。"""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def list_images(self) -> list[dict[str, Any]]:
        """按更新时间倒序返回可在页面展示的图片元数据。

        Returns:
            图片元数据列表，不含缩略图数据，避免首屏响应体过大。
        """

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
        """删除缓存图片及同名元数据文件，供页面管理历史图片。"""

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
        """判断路径是否为页面允许展示的图片文件。"""

        return path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES

    @classmethod
    def _build_image_metadata(cls, image_path: Path) -> dict[str, Any]:
        """将缓存图片路径转换为前端需要的稳定字段。"""

        stat_result = image_path.stat()
        metadata = cls._read_image_sidecar_metadata(image_path)
        return {
            "name": image_path.name,
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
    """创建文生图任务闭包，供页面接口与任务调度服务组合。"""

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
    data_urls: list[str],
    size: str | None,
    quality: str,
    moderation: str,
) -> Callable[[], Awaitable[Path]]:
    """创建图片编辑任务闭包，统一复用插件既有编辑服务。"""

    async def _job() -> Path:
        return await plugin._edit_service.edit(
            model=plugin._get_configured_model(),
            prompt=prompt,
            data_urls=data_urls,
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

    页面只负责把当前输入发送给优化模型，并把纯文本结果回填到输入框；
    配置缺失、网络错误或响应结构异常都会抛出明确异常，避免用户误以为已经优化成功。

    Args:
        settings_store: 页面设置 Tab 维护的提示词优化配置存储。
        prompt: 用户当前输入的提示词。
        mode: 任务类型，取值 generate 或 edit。

    Returns:
        优化后的提示词正文。

    Raises:
        ValueError: 提示词为空。
        RuntimeError: 优化接口请求失败或响应结构不可用。
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


class ImageStudioApi:
    """内置 WebUI 页面对应的后端 Web API。

    页面运行在 Dashboard 的受限 iframe 中，鉴权、主题和路由都由 Dashboard 负责，
    这里只提供图库浏览、删除、生成、编辑和提示词优化等插件自身能力。
    """

    def __init__(self, plugin: Any, cache_dir: Path) -> None:
        self.plugin = plugin
        cache_path = Path(cache_dir)
        self.library = ImageLibrary(cache_path)
        # 受限 iframe 只能接收内联 base64，图库列表必须由服务端缩图后随响应一起返回。
        self.thumbnails = ThumbnailStore(cache_path.parent / "webui_thumbnails")
        self.prompt_optimizer_settings = PromptOptimizerSettingsStore()

    def register(self) -> None:
        """向 Dashboard 注册内置页面使用的后端接口。"""

        context = self.plugin.context
        context.register_web_api(
            f"/{PLUGIN_NAME}/images",
            self.list_images,
            ["GET"],
            "获取历史图库分页数据与缩略图",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/image",
            self.get_image,
            ["GET"],
            "获取单张图片的完整内容",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/image/delete",
            self.delete_image,
            ["POST"],
            "删除历史图库图片",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/generate",
            self.generate_image,
            ["POST"],
            "WebUI 发起文生图任务",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/edit",
            self.edit_image,
            ["POST"],
            "WebUI 发起图片编辑任务",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/optimize-prompt",
            self.optimize_prompt,
            ["POST"],
            "使用配置的文本模型优化提示词",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/prompt-optimizer-settings",
            self.get_prompt_optimizer_settings,
            ["GET"],
            "读取提示词优化模型配置",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/prompt-optimizer-settings",
            self.save_prompt_optimizer_settings,
            ["POST"],
            "保存提示词优化模型配置",
        )

    async def list_images(self) -> Any:
        """返回筛选、排序、分页后的图库数据和对应缩略图。

        Returns:
            包含 images、total、page、page_size 和 total_pages 的 JSON 响应。
        """

        keyword = str(request.query.get("keyword", "") or "").strip().lower()
        image_suffix = str(request.query.get("type", "") or "").strip().lower()
        sort_by = str(request.query.get("sort", "newest") or "newest").lower()
        page = max(1, int(request.query.get("page", 1, type=int) or 1))
        page_size = min(
            GALLERY_MAX_PAGE_SIZE,
            max(
                1,
                int(
                    request.query.get("page_size", GALLERY_DEFAULT_PAGE_SIZE, type=int)
                    or GALLERY_DEFAULT_PAGE_SIZE
                ),
            ),
        )
        if sort_by not in GALLERY_SORT_FIELDS:
            sort_by = "newest"

        all_images = self.library.list_images()
        self.thumbnails.prune({str(item["name"]) for item in all_images})
        if keyword:
            all_images = [
                image
                for image in all_images
                if keyword in str(image["name"]).lower()
                or keyword in str(image["prompt"]).lower()
            ]
        if image_suffix:
            all_images = [
                image
                for image in all_images
                if str(image["name"]).endswith(image_suffix)
            ]
        if sort_by == "oldest":
            all_images.sort(key=lambda item: (int(item["modified_at"]), item["name"]))
        elif sort_by == "name":
            all_images.sort(key=lambda item: str(item["name"]))

        total = len(all_images)
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        start_index = (page - 1) * page_size
        page_images = all_images[start_index : start_index + page_size]
        for image in page_images:
            # Pillow 解码 2K/4K 原图是 CPU 密集操作，放到线程池避免卡住事件循环。
            try:
                image["thumbnail"] = await asyncio.to_thread(
                    self.thumbnails.data_url,
                    self.library.resolve_image_path(str(image["name"])),
                )
            except OSError:
                # 图片可能在生成新图时被并发清理，跳过缩略图即可，不让整页图库失败。
                image["thumbnail"] = ""

        return json_response(
            {
                "images": page_images,
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
            }
        )

    async def get_image(self) -> Any:
        """返回单张图片的完整 base64 内容，用于大图预览。

        Returns:
            图片元数据加 data_url 字段；图片不存在时返回 404。

        Note:
            文件名必须走 query 参数。bridge 的 apiGet 只能把 params 拼到 query string，
            endpoint 本身决定 path，注册 `image/<name>` 这类路径参数路由永远匹配不上。
        """

        name = str(request.query.get("name", "") or "").strip()
        try:
            image_path = self.library.resolve_image_path(name)
        except FileNotFoundError:
            return error_response("图片不存在", status_code=404)

        image = self.library.get_image_by_name(name)
        image["data_url"] = await asyncio.to_thread(_build_image_data_url, image_path)
        return json_response({"image": image})

    async def delete_image(self) -> Any:
        """删除服务端缓存图片及其元数据。"""

        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")

        file_name = str(payload.get("name", "") or "").strip()
        try:
            removed_name = self.library.delete_image_by_name(file_name)
        except FileNotFoundError:
            return error_response("图片不存在", status_code=404)
        except OSError as exc:
            return error_response(f"图片删除失败：{exc}", status_code=500)

        self.thumbnails.discard(removed_name)
        logger.info("[OpenAIImage][webui] 删除历史图片 file=%s", removed_name)
        return json_response({"deleted": removed_name})

    async def generate_image(self) -> Any:
        """处理页面发起的文生图请求。"""

        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")

        prompt = str(payload.get("prompt", "") or "").strip()
        if not prompt:
            return error_response("提示词不能为空")

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

    async def edit_image(self) -> Any:
        """处理页面发起的图片编辑请求。

        受限 iframe 无法直接提交多文件表单，页面会把参考图转成 data URL
        随 JSON 一起提交，因此这里直接复用编辑服务既有的 data_urls 接口。
        """

        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")

        prompt = str(payload.get("prompt", "") or "").strip()
        if not prompt:
            return error_response("提示词不能为空")

        raw_data_urls = payload.get("data_urls")
        if not isinstance(raw_data_urls, list):
            return error_response("参考图片格式错误")
        image_data_urls = [
            str(item) for item in raw_data_urls if str(item).startswith("data:image/")
        ]
        if not image_data_urls:
            return error_response("请上传待编辑图片")

        size = _optional_text(payload.get("size"))
        quality = _option_text(payload.get("quality"), "auto")
        moderation = _option_text(payload.get("moderation"), "low")

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

    async def optimize_prompt(self) -> Any:
        """处理页面提示词优化请求。"""

        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")

        prompt = str(payload.get("prompt", "") or "").strip()
        mode = _option_text(payload.get("mode"), "generate")
        if not prompt:
            return error_response("提示词不能为空")

        try:
            optimized_prompt = await optimize_prompt_text(
                settings_store=self.prompt_optimizer_settings,
                prompt=prompt,
                mode=mode,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[OpenAIImage][webui][prompt_optimizer] 优化失败 mode=%s prompt=%s error=%s",
                mode,
                _truncate_log_text(prompt),
                _truncate_log_text(str(exc)),
            )
            return error_response(str(exc), status_code=500)

        logger.info(
            "[OpenAIImage][webui][prompt_optimizer] 优化完成 mode=%s before=%s after=%s",
            mode,
            _truncate_log_text(prompt),
            _truncate_log_text(optimized_prompt),
        )
        return json_response({"prompt": optimized_prompt})

    async def get_prompt_optimizer_settings(self) -> Any:
        """读取页面提示词优化设置，密钥只返回是否存在。"""

        try:
            return json_response(self.prompt_optimizer_settings.public_payload())
        except ValueError as exc:
            return error_response(str(exc), status_code=500)

    async def save_prompt_optimizer_settings(self) -> Any:
        """保存页面提示词优化设置。"""

        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")

        try:
            saved_payload = self.prompt_optimizer_settings.save_public_payload(payload)
        except ValueError as exc:
            return error_response(str(exc), status_code=500)

        logger.info(
            "[OpenAIImage][webui][prompt_optimizer] 设置已保存 model=%s base_url=%s has_api_key=%s",
            saved_payload["model"] or "-",
            saved_payload["base_url"] or "-",
            saved_payload["has_api_key"],
        )
        return json_response(saved_payload)

    def _task_response(self, task_result: dict[str, Any]) -> Any:
        """将任务服务的统一结果转换为页面 JSON 响应。"""

        self._log_task_result(task_result)
        if not task_result.get("success"):
            return error_response(
                str(task_result.get("error_message") or "图片任务失败"),
                status_code=500,
                data={"timings": task_result.get("timings", {})},
            )

        payload_path = Path(task_result["payload"])
        return json_response(
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
        """记录页面发起的图片任务，便于在 AstrBot 后台定位用户操作。"""

        logger.info(
            "[OpenAIImage][webui][%s] 收到请求 prompt=%s size=%s quality=%s moderation=%s endpoint_type=%s model=%s",
            mode,
            _truncate_log_text(prompt),
            size,
            quality,
            moderation,
            self._safe_plugin_value("_get_endpoint_type"),
            self._safe_plugin_value("_get_configured_model"),
        )

    def _log_task_result(self, task_result: dict[str, Any]) -> None:
        """记录页面任务执行结果，失败日志包含阶段和错误摘要。"""

        timings = task_result.get("timings", {})
        elapsed_ms = timings.get("elapsed_ms")
        queue_wait_ms = timings.get("queue_wait_ms")
        mode = str(task_result.get("mode") or "web_unknown")
        if task_result.get("success"):
            payload = Path(str(task_result.get("payload", ""))).name or "-"
            logger.info(
                "[OpenAIImage][webui][%s] 任务完成 output=%s elapsed_ms=%s queue_wait_ms=%s",
                mode,
                payload,
                elapsed_ms,
                queue_wait_ms,
            )
            return

        logger.warning(
            "[OpenAIImage][webui][%s] 任务失败 stage=%s error=%s elapsed_ms=%s queue_wait_ms=%s",
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


def _build_image_data_url(image_path: Path) -> str:
    """把缓存图片编码为 data URL，供受限 iframe 直接渲染。"""

    encoded = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
    return f"data:{_guess_mime_type(image_path)};base64,{encoded}"


def _optional_text(value: Any) -> str | None:
    """读取可选字符串字段，空白值统一视为未配置。"""

    clean_value = str(value or "").strip()
    return clean_value or None


def _option_text(value: Any, default: str) -> str:
    """读取下拉选项字段，空白时回退默认值。"""

    return str(value or default).strip().lower() or default
