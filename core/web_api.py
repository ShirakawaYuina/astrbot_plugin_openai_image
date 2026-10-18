"""内置 WebUI 页面的后端接口。"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .provider_config import (
    ImageProviderConfig,
    is_same_model,
    read_raw_providers_json,
    write_raw_providers_json,
)
from .services.image_edit_service import ImageEditService
from .services.image_generate_service import ImageGenerateService
from .storage.cache_cleaner import IMAGE_SUFFIXES
from .storage.thumbnail_store import ThumbnailStore

PLUGIN_NAME = "astrbot_plugin_openai_image"
PROMPT_OPTIMIZER_SETTINGS_FILE_NAME = "prompt_optimizer_settings.json"
WEBUI_SETTINGS_FILE_NAME = "webui_settings.json"
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

    def get_provider_id(self) -> str:
        """读取当前选中的提供商 ID。"""

        return str(self.load().get("provider_id", "") or "").strip()

    def save_provider_id(self, provider_id: str) -> dict[str, Any]:
        """保存前端提交的提供商 ID。"""

        next_settings = {
            "provider_id": str(provider_id or "").strip(),
        }
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(
            json.dumps(next_settings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return next_settings

    def public_payload(self) -> dict[str, Any]:
        """返回公开配置摘要。"""

        return {"provider_id": self.get_provider_id()}

    def save_public_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存前端提交的配置。"""

        return self.save_provider_id(
            str(payload.get("provider_id", "") or "").strip()
        )


def _default_prompt_optimizer_settings_path() -> Path:
    """返回提示词优化设置文件路径，统一放在插件数据目录内。"""

    return (
        Path(get_astrbot_plugin_data_path())
        / PLUGIN_NAME
        / PROMPT_OPTIMIZER_SETTINGS_FILE_NAME
    )


class WebUISettingsStore:
    """读写 WebUI 独立配置（如当前选中的图片供应商/模型）。"""

    def __init__(self, settings_path: Path | None = None) -> None:
        self.settings_path = settings_path or _default_webui_settings_path()

    def load(self) -> dict[str, Any]:
        """读取本地配置文件，文件不存在时返回空配置。"""

        if not self.settings_path.is_file():
            return {}
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"WebUI 配置读取失败: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("WebUI 配置格式错误")
        return data

    def get_active_provider_id(self) -> str:
        """读取 WebUI 选中的提供商 ID。"""

        return str(self.load().get("active_provider_id", "") or "").strip()

    def save_active_provider_id(self, provider_id: str) -> dict[str, Any]:
        """保存 WebUI 选中的提供商 ID。"""

        try:
            current = self.load()
        except Exception:
            current = {}
        current["active_provider_id"] = str(provider_id or "").strip()
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(
            json.dumps(current, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return current


def _default_webui_settings_path() -> Path:
    """返回 WebUI 独立设置文件路径，统一放在插件数据目录内。"""

    return (
        Path(get_astrbot_plugin_data_path())
        / PLUGIN_NAME
        / WEBUI_SETTINGS_FILE_NAME
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
    generate_service: Any = None,
    provider: Any = None,
) -> Callable[[], Awaitable[Path]]:
    """创建文生图任务闭包，供页面接口与任务调度服务组合。"""

    service = generate_service or getattr(plugin, "_generate_service", None)
    endpoint_type = (
        provider.endpoint_type
        if provider and hasattr(provider, "endpoint_type")
        else (
            plugin._get_endpoint_type()
            if hasattr(plugin, "_get_endpoint_type")
            else "responses"
        )
    )
    model = (
        provider.model
        if provider and hasattr(provider, "model")
        else (
            plugin._get_configured_model()
            if hasattr(plugin, "_get_configured_model")
            else ""
        )
    )

    async def _job() -> Path:
        resolve_size = getattr(plugin, "_resolve_output_size", None)
        out_size = resolve_size(size) if callable(resolve_size) else size
        try:
            return await service.generate(
                model=model,
                prompt=prompt,
                endpoint_type=endpoint_type,
                size=out_size,
                quality=quality,
                moderation=moderation,
            )
        except Exception as exc:
            get_fallback = getattr(plugin, "_get_fallback_image_provider", None)
            fallback = get_fallback() if callable(get_fallback) else None
            create_gw = getattr(plugin, "_create_image_gateway", None)
            if fallback is None or is_same_model(provider, fallback) or not callable(create_gw):
                raise
            logger.warning(
                "[OpenAIImage][webui_generate] 主模型 %s 调用失败: %s，正在尝试回退模型 %s（%s）",
                model,
                exc,
                fallback.model,
                fallback.name,
            )
            fallback_gateway = create_gw(fallback)
            try:
                fallback_service = ImageGenerateService(
                    gateway=fallback_gateway,
                    cache_store=getattr(plugin, "_cache_store", None),
                )
                return await fallback_service.generate(
                    model=fallback.model,
                    prompt=prompt,
                    endpoint_type=fallback.endpoint_type,
                    size=out_size,
                    quality=quality,
                    moderation=moderation,
                )
            except Exception as fallback_exc:
                raise RuntimeError(
                    f"主模型 [{model}] 调用失败: {exc}；回退模型 [{fallback.model}] 调用失败: {fallback_exc}"
                ) from fallback_exc
            finally:
                if hasattr(fallback_gateway, "close"):
                    await fallback_gateway.close()

    return _job


def create_edit_job(
    *,
    plugin: Any,
    prompt: str,
    data_urls: list[str],
    size: str | None,
    quality: str,
    moderation: str,
    edit_service: Any = None,
    provider: Any = None,
) -> Callable[[], Awaitable[Path]]:
    """创建图片编辑任务闭包，统一复用插件既有编辑服务。"""

    service = edit_service or getattr(plugin, "_edit_service", None)
    endpoint_type = (
        provider.endpoint_type
        if provider and hasattr(provider, "endpoint_type")
        else (
            plugin._get_endpoint_type()
            if hasattr(plugin, "_get_endpoint_type")
            else "responses"
        )
    )
    model = (
        provider.model
        if provider and hasattr(provider, "model")
        else (
            plugin._get_configured_model()
            if hasattr(plugin, "_get_configured_model")
            else ""
        )
    )

    async def _job() -> Path:
        resolve_size = getattr(plugin, "_resolve_output_size", None)
        out_size = resolve_size(size) if callable(resolve_size) else size
        try:
            return await service.edit(
                model=model,
                prompt=prompt,
                data_urls=data_urls,
                endpoint_type=endpoint_type,
                size=out_size,
                quality=quality,
                moderation=moderation,
            )
        except Exception as exc:
            get_fallback = getattr(plugin, "_get_fallback_image_provider", None)
            fallback = get_fallback() if callable(get_fallback) else None
            create_gw = getattr(plugin, "_create_image_gateway", None)
            if fallback is None or is_same_model(provider, fallback) or not callable(create_gw):
                raise
            logger.warning(
                "[OpenAIImage][webui_edit] 主模型 %s 调用失败: %s，正在尝试回退模型 %s（%s）",
                model,
                exc,
                fallback.model,
                fallback.name,
            )
            fallback_gateway = create_gw(fallback)
            try:
                fallback_service = ImageEditService(
                    gateway=fallback_gateway,
                    cache_store=getattr(plugin, "_cache_store", None),
                )
                return await fallback_service.edit(
                    model=fallback.model,
                    prompt=prompt,
                    data_urls=data_urls,
                    endpoint_type=fallback.endpoint_type,
                    size=out_size,
                    quality=quality,
                    moderation=moderation,
                )
            except Exception as fallback_exc:
                raise RuntimeError(
                    f"主模型 [{model}] 调用失败: {exc}；回退模型 [{fallback.model}] 调用失败: {fallback_exc}"
                ) from fallback_exc
            finally:
                if hasattr(fallback_gateway, "close"):
                    await fallback_gateway.close()

    return _job


def _resolve_optimizer_provider(context: Any, provider_id: str) -> Any:
    """根据 provider_id 或默认配置解析用于提示词优化的 Provider 实例。

    Args:
        context: AstrBot 上下文对象。
        provider_id: 用户配置的提供商 ID（可为空）。

    Returns:
        匹配的 Provider 实例，未找到时返回 None。
    """

    if not context:
        return None

    if provider_id and hasattr(context, "get_provider_by_id"):
        try:
            prov = context.get_provider_by_id(provider_id)
            if prov:
                return prov
        except Exception:
            pass

    if hasattr(context, "get_using_provider"):
        try:
            prov = context.get_using_provider()
            if prov:
                return prov
        except Exception:
            pass

    if hasattr(context, "get_all_providers"):
        try:
            provs = context.get_all_providers()
            if provs:
                return provs[0]
        except Exception:
            pass

    return None


async def optimize_prompt_text(
    *,
    context: Any = None,
    settings_store: PromptOptimizerSettingsStore,
    prompt: str,
    mode: str,
) -> str:
    """调用 AstrBot 的模型提供商扩写提示词。

    页面只负责把当前输入发送给优化模型，并把纯文本结果回填到输入框；
    未找到可用模型、接口请求失败或响应结构不可用时都会抛出明确异常。

    Args:
        context: AstrBot 上下文对象。
        settings_store: 页面设置 Tab 维护的提示词优化配置存储。
        prompt: 用户当前输入的提示词。
        mode: 任务类型，取值 generate 或 edit。

    Returns:
        优化后的提示词正文。

    Raises:
        ValueError: 提示词为空。
        RuntimeError: 未找到可用提供商或模型生成失败。
    """

    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        raise ValueError("提示词不能为空")

    provider = _resolve_optimizer_provider(context, settings_store.get_provider_id())
    if not provider:
        raise RuntimeError("未找到可用的 AstrBot 对话模型提供商，请先在 AstrBot 设置中配置模型")

    mode_label = PROMPT_OPTIMIZER_MODE_LABELS.get(str(mode or "").strip(), "图片生成")
    user_prompt = (
        f"当前任务类型：{mode_label}\n"
        "请优化以下提示词，使其更具体、更适合图像模型执行：\n"
        f"{clean_prompt}"
    )

    try:
        response = await provider.text_chat(
            prompt=user_prompt,
            system_prompt=PROMPT_OPTIMIZER_SYSTEM_PROMPT,
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"提示词优化接口请求失败: {exc}") from exc

    if getattr(response, "role", None) == "err":
        error_msg = getattr(response, "completion_text", "") or "模型生成失败"
        raise RuntimeError(f"提示词优化失败: {error_msg}")

    optimized_prompt = str(getattr(response, "completion_text", "") or "").strip()
    if optimized_prompt.startswith("```") and optimized_prompt.endswith("```"):
        lines = optimized_prompt.splitlines()
        if len(lines) >= 2:
            optimized_prompt = "\n".join(lines[1:-1]).strip()

    if not optimized_prompt:
        raise RuntimeError("提示词优化接口未返回可用文本")
    return optimized_prompt


class ImageStudioApi:
    """内置 WebUI 页面对应的后端 Web API。

    页面运行在 Dashboard 的受限 iframe 中，鉴权、主题和路由都由 Dashboard 负责，
    这里只提供图库浏览、删除、生成、编辑和提示词优化等插件自身能力。
    """

    def __init__(
        self,
        plugin: Any,
        cache_dir: Path,
        webui_settings_path: Path | None = None,
    ) -> None:
        self.plugin = plugin
        cache_path = Path(cache_dir)
        self.library = ImageLibrary(cache_path)
        # 受限 iframe 只能接收内联 base64，图库列表必须由服务端缩图后随响应一起返回。
        self.thumbnails = ThumbnailStore(cache_path.parent / "webui_thumbnails")
        self.prompt_optimizer_settings = PromptOptimizerSettingsStore()
        self.webui_settings = WebUISettingsStore(webui_settings_path)
        self._webui_gateway: Any | None = None
        self._webui_gateway_provider: Any | None = None
        self._webui_generate_service: Any | None = None
        self._webui_edit_service: Any | None = None

    async def close(self) -> None:
        """释放 WebUI 独占的网关等网络资源。"""

        if self._webui_gateway is not None:
            await self._webui_gateway.close()
            self._webui_gateway = None
            self._webui_generate_service = None
            self._webui_edit_service = None
            self._webui_gateway_provider = None

    def get_webui_active_image_provider(self) -> Any:
        """获取 WebUI 独立选用的图片供应商。

        若 WebUI 未单独配置，或配置的 provider_id 在可用列表中不存在，
        回退至插件主配置所启用的供应商。
        """

        webui_provider_id = self.webui_settings.get_active_provider_id()
        load_fn = getattr(self.plugin, "_load_configured_providers", None)
        providers = load_fn() if callable(load_fn) else []
        if webui_provider_id and providers:
            matching = next(
                (p for p in providers if p.provider_id == webui_provider_id), None
            )
            if matching is not None:
                return matching

        getter = getattr(self.plugin, "_get_active_image_provider", None)
        if callable(getter):
            return getter()
        if providers:
            return providers[0]
        return None

    async def _resolve_webui_services(
        self, provider: Any
    ) -> tuple[Any, Any]:
        """为 WebUI 解析生图和编辑服务实例。

        若 WebUI 选择的供应商与插件全局供应商相同，直接复用插件主服务；
        若不同，则按该供应商独立创建网关并缓存服务实例。
        """

        plugin_active = getattr(self.plugin, "_active_image_provider", None)
        if (
            provider is None
            or (
                plugin_active is not None
                and plugin_active.provider_id == getattr(provider, "provider_id", None)
            )
        ):
            if self._webui_gateway is not None:
                await self._webui_gateway.close()
                self._webui_gateway = None
                self._webui_generate_service = None
                self._webui_edit_service = None
                self._webui_gateway_provider = None
            return getattr(self.plugin, "_generate_service", None), getattr(
                self.plugin, "_edit_service", None
            )

        if (
            self._webui_gateway is not None
            and self._webui_gateway_provider == provider
            and self._webui_generate_service is not None
            and self._webui_edit_service is not None
        ):
            return self._webui_generate_service, self._webui_edit_service

        create_gw = getattr(self.plugin, "_create_image_gateway", None)
        if not callable(create_gw):
            return getattr(self.plugin, "_generate_service", None), getattr(
                self.plugin, "_edit_service", None
            )

        if self._webui_gateway is not None:
            await self._webui_gateway.close()
            self._webui_gateway = None

        new_gateway = self.plugin._create_image_gateway(provider)
        cache_store = getattr(self.plugin, "_cache_store", None)
        self._webui_gateway = new_gateway
        self._webui_gateway_provider = provider
        self._webui_generate_service = ImageGenerateService(
            gateway=new_gateway,
            cache_store=cache_store,
        )
        self._webui_edit_service = ImageEditService(
            gateway=new_gateway,
            cache_store=cache_store,
        )
        return self._webui_generate_service, self._webui_edit_service

    async def _get_webui_generate_service(self) -> tuple[Any, Any]:
        """获取 WebUI 专用的生图服务及对应供应商配置。"""

        provider = self.get_webui_active_image_provider()
        gen_service, _ = await self._resolve_webui_services(provider)
        return gen_service, provider

    async def _get_webui_edit_service(self) -> tuple[Any, Any]:
        """获取 WebUI 专用的图片编辑服务及对应供应商配置。"""

        provider = self.get_webui_active_image_provider()
        _, edit_service = await self._resolve_webui_services(provider)
        return edit_service, provider

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
        context.register_web_api(
            f"/{PLUGIN_NAME}/image-provider-settings",
            self.get_image_provider_settings,
            ["GET"],
            "读取图片生成供应商与模型配置",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/image-provider-settings",
            self.save_image_provider_settings,
            ["POST"],
            "保存当前启用的图片生成供应商与模型设置",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/providers-json",
            self.get_providers_json,
            ["GET"],
            "读取 providers.json 原始文本配置",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/providers-json",
            self.save_providers_json,
            ["POST"],
            "保存并更新 providers.json 原始文本配置",
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
        gen_service, provider = await self._get_webui_generate_service()
        self._log_web_request(
            mode="generate",
            prompt=prompt,
            size=size or "auto",
            quality=quality,
            moderation=moderation,
            provider=provider,
        )
        task_result = await self.plugin._task_service.run_task(
            mode="web_generate",
            job_coro=create_generation_job(
                plugin=self.plugin,
                prompt=prompt,
                size=size,
                quality=quality,
                moderation=moderation,
                generate_service=gen_service,
                provider=provider,
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
        edit_service, provider = await self._get_webui_edit_service()
        self._log_web_request(
            mode="edit",
            prompt=prompt,
            size=size or "auto",
            quality=quality,
            moderation=moderation,
            provider=provider,
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
                edit_service=edit_service,
                provider=provider,
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
                context=getattr(self.plugin, "context", None),
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
        """读取页面提示词优化设置及 AstrBot 可选的模型提供商列表。"""

        context = getattr(self.plugin, "context", None)
        providers_info: list[dict[str, str]] = []
        default_model = ""

        if context:
            if hasattr(context, "get_using_provider"):
                try:
                    default_prov = context.get_using_provider()
                    if default_prov:
                        default_model = str(
                            default_prov.get_model()
                            if hasattr(default_prov, "get_model")
                            else getattr(default_prov, "model_name", "")
                        )
                except Exception:
                    pass

            if hasattr(context, "get_all_providers"):
                try:
                    for p in context.get_all_providers():
                        meta = p.meta() if hasattr(p, "meta") else None
                        pid = str(
                            getattr(meta, "id", "") or getattr(p, "id", "") or ""
                        )
                        pmodel = str(
                            p.get_model()
                            if hasattr(p, "get_model")
                            else getattr(meta, "model", "")
                        )
                        ptype = str(getattr(meta, "type", "") or "")
                        if pid:
                            providers_info.append(
                                {
                                    "id": pid,
                                    "model": pmodel,
                                    "type": ptype,
                                }
                            )
                except Exception:
                    pass

        selected_id = self.prompt_optimizer_settings.get_provider_id()
        return json_response(
            {
                "provider_id": selected_id,
                "providers": providers_info,
                "default_model": default_model,
            }
        )

    async def save_prompt_optimizer_settings(self) -> Any:
        """保存页面提示词优化设置。"""

        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")

        provider_id = str(payload.get("provider_id", "") or "").strip()
        self.prompt_optimizer_settings.save_provider_id(provider_id)
        logger.info(
            "[OpenAIImage][webui][prompt_optimizer] 设置已保存 provider_id=%s",
            provider_id or "(default)",
        )
        return await self.get_prompt_optimizer_settings()

    async def get_image_provider_settings(self) -> Any:
        """返回图片生成供应商与模型配置，供 WebUI 设置 Tab 展示。"""
        load_fn = getattr(self.plugin, "_load_configured_providers", None)
        providers = load_fn() if callable(load_fn) else []
        active_provider = self.get_webui_active_image_provider()
        active_id = getattr(active_provider, "provider_id", "") if active_provider else ""
        active_name = getattr(active_provider, "name", "") if active_provider else ""
        active_model = getattr(active_provider, "model", "") if active_provider else ""
        return json_response(
            {
                "active_provider_id": active_id,
                "active_provider_name": active_name,
                "active_model": active_model,
                "providers": [
                    {
                        "provider_id": p.provider_id,
                        "name": p.name,
                        "model": p.model,
                        "base_url": p.base_url,
                        "endpoint_type": p.endpoint_type,
                    }
                    for p in providers
                ],
            }
        )

    async def save_image_provider_settings(self) -> Any:
        """保存 WebUI 设置 Tab 提交的当前启用图片供应商/模型（仅在 WebUI 生效）。"""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")

        provider_id = str(payload.get("active_provider_id", "") or "").strip()
        if not provider_id:
            return error_response("未提供供应商 ID")

        load_fn = getattr(self.plugin, "_load_configured_providers", None)
        providers = load_fn() if callable(load_fn) else []
        matching = next((p for p in providers if p.provider_id == provider_id), None)
        if matching is None:
            return error_response(f"未找到指定的图片供应商: {provider_id}")

        self.webui_settings.save_active_provider_id(provider_id)
        await self._resolve_webui_services(matching)

        logger.info(
            "[OpenAIImage][webui][image_provider] 已切换 WebUI 图片供应商 provider_id=%s name=%s model=%s",
            matching.provider_id,
            matching.name,
            matching.model,
        )
        return await self.get_image_provider_settings()

    async def get_providers_json(self) -> Any:
        """返回 providers.json 的原始文本内容。"""
        content = read_raw_providers_json()
        return json_response(
            {
                "path": "data/plugin_data/astrbot_plugin_openai_image/providers.json",
                "content": content,
            }
        )

    async def save_providers_json(self) -> Any:
        """接收前端提交的 providers.json 文本并校验保存。"""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")

        content = str(payload.get("content", "") or "").strip()
        if not content:
            return error_response("配置文件内容不能为空")

        try:
            normalized_providers = write_raw_providers_json(content)
        except ValueError as exc:
            return error_response(str(exc))

        current_active_id = str(
            self.plugin.config.get("active_provider_id", "") or ""
        ).strip()
        if not any(p.provider_id == current_active_id for p in normalized_providers):
            fallback_id = normalized_providers[0].provider_id
            self.plugin.config["active_provider_id"] = fallback_id
            if hasattr(self.plugin.config, "save_config"):
                try:
                    self.plugin.config.save_config()
                except Exception as exc:
                    logger.warning("[OpenAIImage] 保存配置失败: %s", exc)

        self.plugin._rebuild_runtime_dependencies()
        self.plugin._sync_schema_provider_options()

        # 检查 WebUI 独立选中的 provider 是否仍然有效；若已被删除，回退到有效供应商
        webui_active_id = self.webui_settings.get_active_provider_id()
        if webui_active_id and not any(
            p.provider_id == webui_active_id for p in normalized_providers
        ):
            self.webui_settings.save_active_provider_id(
                normalized_providers[0].provider_id
            )

        logger.info(
            "[OpenAIImage][webui][providers_json] 已更新 providers.json，供应商数=%s",
            len(normalized_providers),
        )
        return await self.get_image_provider_settings()

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
        image_metadata = self.library.get_image_by_name(payload_path.name)
        image_metadata["data_url"] = _build_image_data_url(payload_path)
        return json_response(
            {
                "image": image_metadata,
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
        provider: Any = None,
    ) -> None:
        """记录页面发起的图片任务，便于在 AstrBot 后台定位用户操作。"""

        endpoint_type = (
            getattr(provider, "endpoint_type", None)
            if provider is not None
            else self._safe_plugin_value("_get_endpoint_type")
        )
        model = (
            getattr(provider, "model", None)
            if provider is not None
            else self._safe_plugin_value("_get_configured_model")
        )

        logger.info(
            "[OpenAIImage][webui][%s] 收到请求 prompt=%s size=%s quality=%s moderation=%s endpoint_type=%s model=%s",
            mode,
            _truncate_log_text(prompt),
            size,
            quality,
            moderation,
            endpoint_type,
            model,
        )

    def _log_task_result(self, task_result: dict[str, Any]) -> None:
        """记录页面任务执行结果，失败日志包含阶段和错误摘要。"""

        timings = task_result.get("timings", {})
        elapsed_ms = timings.get("elapsed_ms") or timings.get("total_elapsed_ms")
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
