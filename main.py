"""OpenAI 图片插件主入口。"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import time
import uuid
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .core.commands import normalize_output_size, parse_command_payload
from .core.gateways.openai_image_gateway import OpenAIImageGateway
from .core.models import ParsedCommand
from .core.presenters.result_presenter import ResultPresenter
from .core.provider_config import (
    ImageProviderConfig,
    resolve_active_image_provider,
)
from .core.services.image_edit_service import ImageEditService
from .core.services.image_generate_service import ImageGenerateService
from .core.services.image_task_service import ImageTaskService
from .core.storage.image_cache_store import ImageCacheStore
from .core.utils.image_extract import (
    extract_first_at_target,
    extract_first_image_component,
    extract_image_components,
)
from .core.web_admin import WebAdminServer, WebAdminSettings

PLUGIN_NAME = "astrbot_plugin_openai_image"
DEFAULT_RESPONSES_MODEL = "gpt-5.4-mini"
DEFAULT_IMAGES_MODEL = "gpt-image-2"
ENDPOINT_TYPE_IMAGES = "images"
ENDPOINT_TYPE_RESPONSES = "responses"
FIGURE_IMAGE_DIR_NAME = "figure"
FIGURE_IMAGE_FILE_NAME = "robot_figure.png"


@register(
    PLUGIN_NAME,
    "Codex",
    "基于 OpenAI 兼容 chat/completions 接口的图片生成与图片编辑插件。",
    "0.6.40",
)
class OpenAIImagePlugin(Star):
    """OpenAI 图片插件。"""

    def __init__(self, context: Context, config: dict[str, Any] | None = None) -> None:
        super().__init__(context)
        self.config = dict(config or {})
        self.presenter = ResultPresenter()
        self._image_gateway: OpenAIImageGateway | None = None
        self._cache_store: ImageCacheStore | None = None
        self._generate_service: ImageGenerateService | None = None
        self._edit_service: ImageEditService | None = None
        self._task_service: ImageTaskService | None = None
        self._active_image_provider: ImageProviderConfig | None = None
        self._web_admin_server: WebAdminServer | None = None

    async def initialize(self) -> None:
        """初始化插件运行时依赖。"""

        self._rebuild_runtime_dependencies()
        active_provider = self._get_active_image_provider()
        logger.info(
            "[OpenAIImage][startup] 插件已初始化 provider=%s base_url=%s endpoint_type=%s model=%s max_concurrency=%s max_cache_images=%s api_key=%s",
            active_provider.name,
            active_provider.base_url,
            self._get_endpoint_type(),
            self._get_configured_model(),
            self.config.get("max_concurrency", 2),
            self.config.get("max_cache_images", 50),
            self._mask_secret(active_provider.api_key),
        )
        if self._web_admin_server is not None:
            await self._web_admin_server.start()

    async def terminate(self) -> None:
        """关闭插件内部创建的网络资源。"""

        if self._web_admin_server is not None:
            await self._web_admin_server.stop()
        if self._image_gateway is not None:
            await self._image_gateway.close()

    @filter.command("oaiimg")
    async def generate_image_command(
        self,
        event: AstrMessageEvent,
        prompt: str = "",
    ) -> None:
        """文本生成图片。

        用法：
        - /oaiimg <提示词>
        - /oaiimg 2 <提示词>
        """

        event.should_call_llm(True)
        await self._handle_generate_command(
            event,
            raw_prompt=self._resolve_command_raw_prompt(event, "oaiimg", prompt),
        )

    @filter.command("oaiedit")
    async def edit_image_command(
        self,
        event: AstrMessageEvent,
        prompt: str = "",
    ) -> Any:
        """图片编辑。

        用法：
        - /oaiedit <提示词>
        - /oaiedit 2 <提示词>
        - 需要在同条消息带图，或回复一张图片后执行
        """

        event.should_call_llm(True)
        async for result in self._handle_edit_command(
            event,
            raw_prompt=self._resolve_command_raw_prompt(event, "oaiedit", prompt),
        ):
            yield result

    @filter.command("oaiqlogo")
    async def qlogo_edit_command(
        self,
        event: AstrMessageEvent,
        prompt: str = "",
    ) -> None:
        """使用被 @ 用户的 QQ 头像作为输入图执行图片编辑。"""

        event.should_call_llm(True)
        await self._handle_qlogo_command(event, raw_prompt=prompt)

    @filter.command("oaifigure")
    async def figure_image_command(
        self,
        event: AstrMessageEvent,
        _prompt: str = "",
    ) -> None:
        """设置或更新机器人形象参考图。"""

        event.should_call_llm(True)
        await self._handle_figure_command(event)

    @filter.llm_tool(name="openai_generate_image")
    async def openai_generate_image_tool(
        self,
        event: AstrMessageEvent,
        prompt: str,
        count: int = 1,
    ) -> str:
        """供 LLM 调用的文生图工具。

        Args:
            prompt(string): 图片生成提示词。
            count(number): 生成图片数量，默认 1。
        """
        # LLM 调用时可能传递 float 类型（如 1.0），需转为 int
        count = int(count)

        result = await self._execute_generate_flow(
            event=event,
            prompt=str(prompt or "").strip(),
            count=count,
            size=None,
            send_user_message=False,
        )
        return str(result["summary"])

    @filter.llm_tool(name="openai_edit_image")
    async def openai_edit_image_tool(
        self,
        event: AstrMessageEvent,
        prompt: str,
        count: int = 1,
    ) -> str:
        """供 LLM 调用的图片编辑工具。

        Args:
            prompt(string): 图片编辑提示词。
            count(number): 生成图片数量，默认 1。
        """
        # LLM 调用时可能传递 float 类型（如 1.0），需转为 int
        count = int(count)

        if extract_first_image_component(event) is None:
            return "未检测到图片，请在当前消息中携带图片或回复一张图片后再调用图片编辑工具。"

        result = await self._execute_edit_flow(
            event=event,
            prompt=str(prompt or "").strip(),
            count=count,
            size=None,
            send_user_message=False,
        )
        return str(result["summary"])

    @filter.llm_tool(name="openai_edit_robot_figure_image")
    async def openai_edit_robot_figure_image_tool(
        self,
        event: AstrMessageEvent,
        prompt: str,
        count: int = 1,
        size: str | None = None,
        quality: str = "auto",
        moderation: str = "low",
    ) -> str:
        """供 LLM 调用的机器人形象图编辑工具。

        当用户要求生成、编辑或查看机器人自己、Bot 形象、助手形象、看板娘、
        机器人头像、机器人立绘、机器人表情包等与机器人有关的图像时调用。
        生成时应鼓励图像模型根据提示词调整表情、动作、姿态和服装，避免机器人形象单一化。
        工具成功后应以机器人本人看到新形象的自然反应回复，不要描述工具执行过程。

        Args:
            prompt(string): 基于机器人形象参考图进行编辑的提示词。
            count(number): 生成图片数量，默认 1。
            size(string): 可选输出尺寸，例如 auto、1024x1024、portrait。
            quality(string): 可选质量，auto、low、medium、high。
            moderation(string): 可选审核级别，low 或 auto。
        """

        figure_path = self._get_figure_image_path()
        if not figure_path.is_file():
            return (
                "尚未设置机器人形象图，请先回复或发送一张图片并使用 /oaifigure 设置。"
            )

        result = await self._execute_figure_edit_flow(
            event=event,
            prompt=self._build_robot_figure_edit_prompt(prompt),
            count=int(count),
            size=size,
            quality=quality,
            moderation=moderation,
            send_user_message=False,
        )
        return self._build_robot_figure_tool_reply(result)

    @filter.regex(r"(?:[/!])oaiedit(?:\s+.+)?", priority=-10)
    async def edit_image_regex_fallback(self, event: AstrMessageEvent) -> Any:
        """兼容图片在前、命令在后的场景。"""

        message_text = str(event.message_str or "").strip()
        if not message_text:
            return
        if self._is_direct_command_text(message_text, "oaiedit"):
            return
        extracted_prompt = self._extract_command_arg_anywhere(message_text, "oaiedit")
        if extracted_prompt is None:
            return
        if extract_first_image_component(event) is None:
            return

        event.should_call_llm(True)
        async for result in self._handle_edit_command(
            event,
            raw_prompt=extracted_prompt,
        ):
            yield result
        event.stop_event()

    async def _handle_generate_command(
        self,
        event: AstrMessageEvent,
        raw_prompt: str,
    ) -> None:
        """处理 `/oaiimg` 命令的完整流程。"""

        try:
            parsed_command = self._parse_command_payload_for_event(event, raw_prompt)
        except ValueError as exc:
            await event.send(event.plain_result(f"参数错误：{exc}"))
            return

        await self._send_pending_message(
            event,
            prompt=parsed_command.prompt,
        )
        await self._execute_generate_flow(
            event=event,
            prompt=parsed_command.prompt,
            count=parsed_command.count,
            size=parsed_command.size,
            quality=parsed_command.quality,
            moderation=parsed_command.moderation,
            send_user_message=True,
        )

    async def _handle_edit_command(
        self,
        event: AstrMessageEvent,
        raw_prompt: str,
    ) -> Any:
        """处理 `/oaiedit` 命令的完整流程。"""

        try:
            parsed_command = self._parse_command_payload_for_event(event, raw_prompt)
        except ValueError as exc:
            await event.send(event.plain_result(f"参数错误：{exc}"))
            return

        if extract_first_image_component(event) is None:
            await self._execute_edit_flow(
                event=event,
                prompt=parsed_command.prompt,
                count=parsed_command.count,
                size=parsed_command.size,
                quality=parsed_command.quality,
                moderation=parsed_command.moderation,
                send_user_message=True,
            )
            return

        if (
            event.get_platform_name() == "aiocqhttp"
            and extract_first_image_component(event) is not None
        ):
            yield self._build_edit_pending_result(parsed_command.prompt)
        else:
            await self._send_pending_message(
                event,
                prompt=parsed_command.prompt,
            )

        await self._execute_edit_flow(
            event=event,
            prompt=parsed_command.prompt,
            count=parsed_command.count,
            size=parsed_command.size,
            quality=parsed_command.quality,
            moderation=parsed_command.moderation,
            send_user_message=True,
        )

    async def _handle_qlogo_command(
        self,
        event: AstrMessageEvent,
        raw_prompt: str,
    ) -> None:
        """处理 `/oaiqlogo` 命令。"""

        try:
            parsed_command = self._parse_command_payload_for_event(event, raw_prompt)
        except ValueError as exc:
            await event.send(event.plain_result(f"参数错误：{exc}"))
            return

        qq_id = extract_first_at_target(event)
        if not qq_id:
            await event.send(
                event.plain_result("请在命令中 @ 一位 QQ 用户后再执行 /oaiqlogo。")
            )
            return

        await self._send_pending_message(
            event,
            prompt=parsed_command.prompt,
        )
        await self._execute_avatar_edit_flow(
            event=event,
            qq_id=qq_id,
            prompt=parsed_command.prompt,
            count=parsed_command.count,
            size=parsed_command.size,
            quality=parsed_command.quality,
            moderation=parsed_command.moderation,
            send_user_message=True,
        )

    async def _handle_figure_command(self, event: AstrMessageEvent) -> None:
        """处理 `/oaifigure` 命令，将消息中的图片保存为机器人形象图。"""

        image_component = extract_first_image_component(event)
        if image_component is None:
            await event.send(
                event.plain_result(
                    "请在同条消息带图，或回复一张图片后再执行 /oaifigure 设置机器人形象图。"
                )
            )
            return

        try:
            figure_path = await self._save_figure_image(image_component)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[OpenAIImage][figure] 机器人形象图保存失败 error=%s",
                exc,
                exc_info=True,
            )
            await event.send(event.plain_result(f"机器人形象图保存失败：{exc}"))
            return

        logger.info("[OpenAIImage][figure] 机器人形象图已更新 path=%s", figure_path)
        await event.send(event.plain_result("机器人形象图已更新。"))

    async def _execute_generate_flow(
        self,
        event: AstrMessageEvent,
        prompt: str,
        count: int,
        size: str | None = None,
        quality: str = "auto",
        moderation: str = "low",
        *,
        send_user_message: bool,
    ) -> dict[str, Any]:
        """执行文生图公共流程，供命令与函数工具复用。"""

        task_id = self._new_task_id()
        command_start = time.perf_counter()
        self._ensure_ready()

        logger.info(
            "[OpenAIImage][generate][task_id=%s] 收到请求 count=%s prompt=%s source=%s",
            task_id,
            count,
            self._truncate_text(prompt),
            "command" if send_user_message else "llm_tool",
        )

        task_results = await self._run_generate_jobs(
            task_id=task_id,
            count=count,
            prompt=prompt,
            size=self._resolve_output_size(size),
            quality=quality,
            moderation=moderation,
            event=event,
            send_each_result=send_user_message,
        )
        return await self._finalize_results(
            event=event,
            task_id=task_id,
            mode="generate",
            task_results=task_results,
            command_start=command_start,
            send_user_message=send_user_message,
            results_already_sent=send_user_message,
        )

    async def _execute_edit_flow(
        self,
        event: AstrMessageEvent,
        prompt: str,
        count: int,
        size: str | None = None,
        quality: str = "auto",
        moderation: str = "low",
        *,
        send_user_message: bool,
    ) -> dict[str, Any]:
        """执行图片编辑公共流程，供命令与函数工具复用。"""

        task_id = self._new_task_id()
        command_start = time.perf_counter()
        self._ensure_ready()

        image_components = extract_image_components(event)
        if not image_components:
            logger.warning(
                "[OpenAIImage][edit][task_id=%s] 未检测到待编辑图片",
                task_id,
            )
            summary = (
                "未检测到图片，请在同条消息带图，或回复一张图片后再执行 /oaiedit。"
            )
            if send_user_message:
                await event.send(event.plain_result(summary))
            return {"status": "failed", "summary": summary}

        image_source = self._detect_image_source(event)
        logger.info(
            "[OpenAIImage][edit][task_id=%s] 收到请求 count=%s input_image_count=%s image_source=%s prompt=%s source=%s",
            task_id,
            count,
            len(image_components),
            image_source,
            self._truncate_text(prompt),
            "command" if send_user_message else "llm_tool",
        )
        try:
            data_url_start = time.perf_counter()
            image_data_urls = [
                await self._build_image_data_url(image_component)
                for image_component in image_components
            ]
            data_url_elapsed_ms = int((time.perf_counter() - data_url_start) * 1000)
            logger.info(
                "[OpenAIImage][edit][task_id=%s] 输入图片转换完成 image_count=%s elapsed_ms=%s",
                task_id,
                len(image_data_urls),
                data_url_elapsed_ms,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[OpenAIImage][edit][task_id=%s] 输入图片处理失败 error=%s",
                task_id,
                exc,
                exc_info=True,
            )
            summary = f"图片读取失败：{exc}"
            if send_user_message:
                await event.send(event.plain_result(summary))
            return {"status": "failed", "summary": summary}

        task_results = await self._run_edit_jobs(
            task_id=task_id,
            count=count,
            prompt=prompt,
            size=self._resolve_output_size(size),
            quality=quality,
            moderation=moderation,
            data_urls=image_data_urls,
            event=event,
            send_each_result=send_user_message,
        )
        return await self._finalize_results(
            event=event,
            task_id=task_id,
            mode="edit",
            task_results=task_results,
            command_start=command_start,
            send_user_message=send_user_message,
            results_already_sent=send_user_message,
        )

    async def _execute_avatar_edit_flow(
        self,
        event: AstrMessageEvent,
        qq_id: str,
        prompt: str,
        count: int,
        size: str | None = None,
        quality: str = "auto",
        moderation: str = "low",
        *,
        send_user_message: bool,
    ) -> dict[str, Any]:
        """使用 QQ 头像作为输入图执行图片编辑流程。"""

        task_id = self._new_task_id()
        command_start = time.perf_counter()
        self._ensure_ready()

        logger.info(
            "[OpenAIImage][qlogo][task_id=%s] 收到请求 qq_id=%s count=%s prompt=%s source=%s",
            task_id,
            qq_id,
            count,
            self._truncate_text(prompt),
            "command" if send_user_message else "llm_tool",
        )

        try:
            avatar_data_url = await self._build_qlogo_data_url(qq_id)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[OpenAIImage][qlogo][task_id=%s] QQ 头像获取失败 qq_id=%s error=%s",
                task_id,
                qq_id,
                exc,
                exc_info=True,
            )
            summary = f"QQ 头像获取失败：{exc}"
            if send_user_message:
                await event.send(event.plain_result(summary))
            return {"status": "failed", "summary": summary}

        task_results = await self._run_edit_jobs(
            task_id=task_id,
            count=count,
            prompt=prompt,
            size=self._resolve_output_size(size),
            quality=quality,
            moderation=moderation,
            data_urls=[avatar_data_url],
            event=event,
            send_each_result=send_user_message,
        )
        return await self._finalize_results(
            event=event,
            task_id=task_id,
            mode="edit",
            task_results=task_results,
            command_start=command_start,
            send_user_message=send_user_message,
            results_already_sent=send_user_message,
        )

    async def _execute_figure_edit_flow(
        self,
        event: AstrMessageEvent,
        prompt: str,
        count: int,
        size: str | None = None,
        quality: str = "auto",
        moderation: str = "low",
        *,
        send_user_message: bool,
    ) -> dict[str, Any]:
        """使用已保存的机器人形象图作为输入图执行编辑流程。"""

        task_id = self._new_task_id()
        command_start = time.perf_counter()
        self._ensure_ready()

        figure_path = self._get_figure_image_path()
        if not figure_path.is_file():
            summary = (
                "尚未设置机器人形象图，请先回复或发送一张图片并使用 /oaifigure 设置。"
            )
            if send_user_message:
                await event.send(event.plain_result(summary))
            return {"status": "failed", "summary": summary}

        logger.info(
            "[OpenAIImage][figure][task_id=%s] 收到请求 count=%s prompt=%s source=%s",
            task_id,
            count,
            self._truncate_text(prompt),
            "command" if send_user_message else "llm_tool",
        )

        try:
            figure_data_url = self._build_local_image_data_url(figure_path)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[OpenAIImage][figure][task_id=%s] 机器人形象图读取失败 error=%s",
                task_id,
                exc,
                exc_info=True,
            )
            summary = f"机器人形象图读取失败：{exc}"
            if send_user_message:
                await event.send(event.plain_result(summary))
            return {"status": "failed", "summary": summary}

        task_results = await self._run_edit_jobs(
            task_id=task_id,
            count=count,
            prompt=prompt,
            size=self._resolve_output_size(size),
            quality=quality,
            moderation=moderation,
            data_urls=[figure_data_url],
            event=event,
            send_each_result=send_user_message,
        )
        return await self._finalize_results(
            event=event,
            task_id=task_id,
            mode="edit",
            task_results=task_results,
            command_start=command_start,
            send_user_message=send_user_message,
            results_already_sent=send_user_message,
        )

    async def _run_generate_jobs(
        self,
        task_id: str,
        count: int,
        prompt: str,
        size: str | None = None,
        quality: str = "auto",
        moderation: str = "low",
        event: AstrMessageEvent | None = None,
        send_each_result: bool = False,
    ) -> list[dict[str, Any]]:
        """执行批量文生图任务。"""

        assert self._task_service is not None
        assert self._generate_service is not None

        async def _run_one(index: int) -> dict[str, Any]:
            result = await self._task_service.run_task(
                mode="generate",
                stage_name="request",
                job_coro=lambda: self._generate_service.generate(
                    model=self._get_configured_model(),
                    prompt=prompt,
                    negative_prompt=self._get_negative_prompt(),
                    endpoint_type=self._get_endpoint_type(),
                    size=size,
                    quality=quality,
                    moderation=moderation,
                ),
            )
            self._log_task_result(
                task_id=task_id,
                job_index=index,
                result=result,
            )
            await self._send_single_success_result(
                event=event,
                task_id=task_id,
                job_index=index,
                result=result,
                enabled=send_each_result,
            )
            return result

        tasks = [_run_one(index) for index in range(1, count + 1)]
        return await asyncio.gather(*tasks)

    async def _run_edit_jobs(
        self,
        task_id: str,
        count: int,
        prompt: str,
        data_urls: list[str],
        size: str | None = None,
        quality: str = "auto",
        moderation: str = "low",
        event: AstrMessageEvent | None = None,
        send_each_result: bool = False,
    ) -> list[dict[str, Any]]:
        """执行批量图生图任务。"""

        assert self._task_service is not None
        assert self._edit_service is not None

        async def _run_one(index: int) -> dict[str, Any]:
            result = await self._task_service.run_task(
                mode="edit",
                stage_name="request",
                job_coro=lambda: self._edit_service.edit(
                    model=self._get_configured_model(),
                    prompt=prompt,
                    data_urls=data_urls,
                    negative_prompt=self._get_negative_prompt(),
                    endpoint_type=self._get_endpoint_type(),
                    size=size,
                    quality=quality,
                    moderation=moderation,
                ),
            )
            self._log_task_result(
                task_id=task_id,
                job_index=index,
                result=result,
            )
            await self._send_single_success_result(
                event=event,
                task_id=task_id,
                job_index=index,
                result=result,
                enabled=send_each_result,
            )
            return result

        tasks = [_run_one(index) for index in range(1, count + 1)]
        return await asyncio.gather(*tasks)

    async def _send_single_success_result(
        self,
        *,
        event: AstrMessageEvent | None,
        task_id: str,
        job_index: int,
        result: dict[str, Any],
        enabled: bool,
    ) -> None:
        """在单个任务成功后立即发送图片，失败时改写任务结果用于最终统计。"""

        if not enabled or event is None:
            return
        if not result.get("success") or not result.get("payload"):
            return

        image_path = Path(str(result["payload"]))
        send_start = time.perf_counter()
        try:
            await self.presenter.send_images(event, [image_path])
        except Exception as exc:  # noqa: BLE001
            send_elapsed_ms = int((time.perf_counter() - send_start) * 1000)
            logger.error(
                "[OpenAIImage][send][task_id=%s][job=%s] 单图发送失败 elapsed_ms=%s error=%s",
                task_id,
                job_index,
                send_elapsed_ms,
                exc,
                exc_info=True,
            )
            result.update(
                {
                    "success": False,
                    "error_stage": "send",
                    "error_message": f"图片已生成，但发送到 QQ 失败：{exc}",
                }
            )
            return

        send_elapsed_ms = int((time.perf_counter() - send_start) * 1000)
        logger.info(
            "[OpenAIImage][send][task_id=%s][job=%s] 单图发送完成 elapsed_ms=%s",
            task_id,
            job_index,
            send_elapsed_ms,
        )

    async def _finalize_results(
        self,
        event: AstrMessageEvent,
        task_id: str,
        mode: str,
        task_results: list[dict[str, Any]],
        command_start: float,
        *,
        send_user_message: bool,
        results_already_sent: bool = False,
    ) -> dict[str, Any]:
        """整理结果并按需要向用户发送图片。"""

        success_paths = [
            Path(str(result["payload"]))
            for result in task_results
            if result.get("success") and result.get("payload")
        ]
        failed_results = [
            result for result in task_results if not result.get("success")
        ]

        if not success_paths:
            summary = (
                "；".join(
                    f"{item.get('error_stage', 'request')}={item.get('error_message', '未知错误')}"
                    for item in failed_results[:3]
                )
                or "接口未返回可用图片"
            )
            logger.error(
                "[OpenAIImage][%s][task_id=%s] 全部任务失败 reason=%s",
                mode,
                task_id,
                summary,
            )
            message = f"图片处理失败：{summary}"
            if send_user_message:
                await event.send(event.plain_result(message))
            return {"status": "failed", "summary": message}

        if not results_already_sent:
            send_start = time.perf_counter()
            try:
                await self.presenter.send_images(event, success_paths)
            except Exception as exc:  # noqa: BLE001
                send_elapsed_ms = int((time.perf_counter() - send_start) * 1000)
                logger.error(
                    "[OpenAIImage][send][task_id=%s] 平台发送失败 elapsed_ms=%s error=%s",
                    task_id,
                    send_elapsed_ms,
                    exc,
                    exc_info=True,
                )
                message = f"图片已生成，但发送到 QQ 失败：{exc}"
                if send_user_message:
                    await event.send(event.plain_result(message))
                return {"status": "failed", "summary": message}

            send_elapsed_ms = int((time.perf_counter() - send_start) * 1000)
            logger.info(
                "[OpenAIImage][send][task_id=%s] 发送完成 elapsed_ms=%s image_count=%s",
                task_id,
                send_elapsed_ms,
                len(success_paths),
            )

        total_elapsed_ms = int((time.perf_counter() - command_start) * 1000)
        logger.info(
            "[OpenAIImage][%s][task_id=%s] 命令完成 total_elapsed_ms=%s success_count=%s failed_count=%s",
            mode,
            task_id,
            total_elapsed_ms,
            len(success_paths),
            len(failed_results),
        )

        summary = f"已生成 {len(success_paths)} 张图片"
        if mode == "edit":
            summary = f"已编辑 {len(success_paths)} 张图片"
        if failed_results:
            summary = f"{summary}，失败 {len(failed_results)} 张"
            if send_user_message:
                await event.send(
                    event.plain_result(
                        f"本次共成功 {len(success_paths)} 张，失败 {len(failed_results)} 张。"
                    )
                )
        return {"status": "success", "summary": summary}

    async def _build_image_data_url(self, image_component: Any) -> str:
        """将 AstrBot 图片组件转换为 data URL。"""

        if hasattr(image_component, "convert_to_base64"):
            base64_data = await image_component.convert_to_base64()
        else:
            image_path = Path(str(getattr(image_component, "path", ""))).expanduser()
            if not image_path.exists():
                raise ValueError("未找到待编辑图片文件")
            base64_data = base64.b64encode(image_path.read_bytes()).decode("utf-8")

        mime_type = self._guess_image_mime_type(image_component)
        return f"data:{mime_type};base64,{base64_data}"

    async def _build_qlogo_data_url(self, qq_id: str) -> str:
        """下载 QQ 头像并转换为 data URL。"""

        qlogo_url = f"https://q1.qlogo.cn/g?b=qq&nk={qq_id}&s=100"
        timeout = aiohttp.ClientTimeout(
            total=max(5, int(self.config.get("request_timeout_seconds", 180) or 180))
        )
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(qlogo_url) as response:
                response.raise_for_status()
                image_bytes = await response.read()
                mime_type = (
                    str(response.headers.get("Content-Type", "") or "")
                    .split(";")[0]
                    .strip()
                    or "image/jpeg"
                )

        if not image_bytes:
            raise ValueError("头像响应内容为空")

        base64_data = base64.b64encode(image_bytes).decode("utf-8")
        return f"data:{mime_type};base64,{base64_data}"

    async def _save_figure_image(self, image_component: Any) -> Path:
        """将用户提供的图片保存为机器人形象图。"""

        if hasattr(image_component, "convert_to_base64"):
            base64_data = await image_component.convert_to_base64()
            image_bytes = base64.b64decode(base64_data)
        else:
            image_path = Path(str(getattr(image_component, "path", ""))).expanduser()
            if not image_path.exists():
                raise ValueError("未找到形象图片文件")
            image_bytes = image_path.read_bytes()

        if not image_bytes:
            raise ValueError("形象图片内容为空")

        figure_path = self._get_figure_image_path()
        figure_path.parent.mkdir(parents=True, exist_ok=True)
        # 固定文件名便于 LLM 工具稳定读取最新形象图，更新时直接覆盖旧图。
        figure_path.write_bytes(image_bytes)
        return figure_path

    def _get_figure_image_path(self) -> Path:
        """返回机器人形象图在 data/plugin_data 下的固定保存路径。"""

        return (
            Path(get_astrbot_plugin_data_path())
            / PLUGIN_NAME
            / FIGURE_IMAGE_DIR_NAME
            / FIGURE_IMAGE_FILE_NAME
        )

    def _build_local_image_data_url(self, image_path: Path) -> str:
        """将本地图片文件转换为 data URL，供图片编辑接口使用。"""

        image_bytes = image_path.read_bytes()
        if not image_bytes:
            raise ValueError("图片内容为空")
        mime_type = mimetypes.guess_type(str(image_path))[0] or "image/png"
        base64_data = base64.b64encode(image_bytes).decode("utf-8")
        return f"data:{mime_type};base64,{base64_data}"

    @staticmethod
    def _build_robot_figure_edit_prompt(prompt: str) -> str:
        """构建机器人形象图工具专用提示词，强化表情、动作和服装的多样化。"""

        clean_prompt = str(prompt or "").strip()
        return (
            "请以提供的机器人形象参考图为基础生成新图，保持角色核心身份、脸部识别特征、"
            "发型/轮廓/主要配色等能代表机器人的关键设定一致。"
            "同时必须根据用户提示词主动设计贴合场景的表情、动作、姿态、视线、肢体语言和服装，"
            "服装可以随主题、职业、季节、活动或情绪变化而变化。"
            "避免总是生成正脸站立、微笑、静态半身、相同手势或相同服装；"
            "画面应体现提示词中的情绪和事件，让机器人看起来正在自然参与该场景。"
            f"\n\n用户提示词：{clean_prompt}"
        )

    @staticmethod
    def _build_robot_figure_tool_reply(result: dict[str, Any]) -> str:
        """为机器人形象图工具返回面向 LLM 的自然回复约束。"""

        if result.get("status") != "success":
            return str(result.get("summary", "机器人形象处理失败"))

        # 成功时直接给出可作为最终回复的自然反应，避免 LLM 复述工具执行摘要。
        return "哇，这个感觉还挺像我的，稍微有点害羞，但我挺喜欢。"

    def _guess_image_mime_type(self, image_component: Any) -> str:
        """推断输入图片的 MIME 类型。"""

        component_mime = str(getattr(image_component, "mime_type", "") or "").strip()
        if component_mime:
            return component_mime

        raw_file = str(getattr(image_component, "file", "") or "").strip()
        if raw_file.startswith("file:///"):
            guessed = mimetypes.guess_type(raw_file[8:])[0]
            if guessed:
                return guessed

        image_path = str(getattr(image_component, "path", "") or "").strip()
        guessed = mimetypes.guess_type(image_path)[0]
        return guessed or "image/png"

    def _rebuild_runtime_dependencies(self) -> None:
        """根据当前配置重建运行时依赖。"""

        active_provider = resolve_active_image_provider(self.config)
        self._active_image_provider = active_provider

        self.presenter = ResultPresenter(
            send_mode=str(self.config.get("image_send_mode", "base64") or "base64"),
            url_base=str(self.config.get("image_send_url_base", "") or ""),
        )

        cache_root = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME / "images"
        self._cache_store = ImageCacheStore(
            cache_dir=cache_root,
            max_cache_images=int(self.config.get("max_cache_images", 50) or 50),
        )

        self._image_gateway = OpenAIImageGateway(
            base_url=active_provider.base_url,
            api_key=active_provider.api_key,
            timeout_seconds=int(self.config.get("request_timeout_seconds", 180) or 180),
            proxy_url=active_provider.proxy_url,
        )
        self._generate_service = ImageGenerateService(
            gateway=self._image_gateway,
            cache_store=self._cache_store,
        )
        self._edit_service = ImageEditService(
            gateway=self._image_gateway,
            cache_store=self._cache_store,
        )
        self._task_service = ImageTaskService(
            max_concurrency=int(self.config.get("max_concurrency", 2) or 2),
        )
        self._web_admin_server = WebAdminServer(
            plugin=self,
            settings=WebAdminSettings.from_config(self.config),
            cache_dir=cache_root,
        )

    def _ensure_ready(self) -> None:
        """确保运行时依赖已初始化。"""

        if self._image_gateway is None or self._generate_service is None:
            self._rebuild_runtime_dependencies()

    def _get_active_image_provider(self) -> ImageProviderConfig:
        """读取当前启用供应商，必要时重新解析配置作为兜底。"""

        if self._active_image_provider is None:
            # initialize 日志可能在依赖重建后读取该对象；这里保留兜底，防止测试或热重载流程绕过重建。
            self._active_image_provider = resolve_active_image_provider(self.config)
        return self._active_image_provider

    def _get_negative_prompt(self) -> str:
        """读取全局负面提示词配置，统一去除首尾空白避免污染请求体。"""

        return str(self.config.get("negative_prompt", "") or "").strip()

    def _resolve_output_size(self, command_size: str | None = None) -> str | None:
        """解析本次请求尺寸，命令参数优先，配置默认值作为兜底。"""

        clean_command_size = str(command_size or "").strip()
        if clean_command_size:
            return normalize_output_size(clean_command_size)

        # image_size 为空时沿用接口默认尺寸；auto 则显式交给上游模型自动决定。
        return normalize_output_size(self.config.get("image_size"))

    def _get_endpoint_type(self) -> str:
        """读取图片生成端点类型，未知值回退到 Responses 以保持兼容。"""

        endpoint_type = str(
            self.config.get("endpoint_type", ENDPOINT_TYPE_RESPONSES) or ""
        ).strip()
        if endpoint_type == ENDPOINT_TYPE_IMAGES:
            return ENDPOINT_TYPE_IMAGES
        return ENDPOINT_TYPE_RESPONSES

    def _get_configured_model(self) -> str:
        """读取模型配置，未显式配置时按端点类型选择默认模型。"""

        configured_model = str(self.config.get("model", "") or "").strip()
        if configured_model:
            return configured_model
        if self._get_endpoint_type() == ENDPOINT_TYPE_IMAGES:
            return DEFAULT_IMAGES_MODEL
        return DEFAULT_RESPONSES_MODEL

    def _parse_command_payload_for_event(
        self,
        event: AstrMessageEvent,
        raw_prompt: str,
    ) -> ParsedCommand:
        """按调用者权限解析命令参数。

        Bot 管理员保持完整参数解析，普通成员在开启限制时则不拆解任何参数。
        这样普通成员输入的 `2`、`--size`、`-q`、`-m` 会完整保留到提示词里，
        只是不再影响生成数量、尺寸、质量和审核设置。
        """

        clean_raw_prompt = str(raw_prompt or "").strip()
        if not clean_raw_prompt:
            raise ValueError("提示词不能为空")

        if self._should_parse_command_options(event):
            return parse_command_payload(clean_raw_prompt)

        return ParsedCommand(count=1, prompt=clean_raw_prompt)

    def _should_parse_command_options(self, event: AstrMessageEvent) -> bool:
        """判断当前调用者是否允许拆解命令参数。

        这里沿用原有配置键，保证旧配置升级后继续可用。
        """

        admin_only = bool(self.config.get("multi_image_command_admin_only", True))
        if not admin_only:
            return True

        # AstrBot 的 is_admin 表示 Bot 管理员，不是 QQ 群管理员或群主。
        return bool(event.is_admin())

    def _log_task_result(
        self,
        task_id: str,
        job_index: int,
        result: dict[str, Any],
    ) -> None:
        """记录单个任务的耗时与结果。"""

        timings = result.get("timings", {})
        if result.get("success"):
            logger.info(
                "[OpenAIImage][task][task_id=%s][job=%s] success queue_wait_ms=%s total_elapsed_ms=%s",
                task_id,
                job_index,
                timings.get("queue_wait_ms", 0),
                timings.get("total_elapsed_ms", 0),
            )
            return

        logger.error(
            "[OpenAIImage][task][task_id=%s][job=%s] failed error_stage=%s error_message=%s queue_wait_ms=%s total_elapsed_ms=%s",
            task_id,
            job_index,
            result.get("error_stage", "request"),
            result.get("error_message", "未知错误"),
            timings.get("queue_wait_ms", 0),
            timings.get("total_elapsed_ms", 0),
        )

    @staticmethod
    def _is_direct_command_text(message_text: str, command_name: str) -> bool:
        """判断是否是标准的首段命令文本。"""

        normalized = str(message_text or "").strip().lower()
        prefixes = (f"/{command_name}", f"!{command_name}", command_name)
        return any(normalized.startswith(prefix) for prefix in prefixes)

    @staticmethod
    def _extract_command_arg_anywhere(
        message_text: str,
        command_name: str,
    ) -> str | None:
        """从任意位置提取命令后的参数文本。"""

        normalized = str(message_text or "")
        search_tokens = (f"/{command_name}", f"!{command_name}", command_name)
        lower_text = normalized.lower()
        found_index = -1
        token_text = ""
        for token in search_tokens:
            found_index = lower_text.find(token.lower())
            if found_index >= 0:
                token_text = token
                break
        if found_index < 0:
            return None

        return normalized[found_index + len(token_text) :].strip()

    def _resolve_command_raw_prompt(
        self,
        event: AstrMessageEvent,
        command_name: str,
        parsed_prompt: str,
    ) -> str:
        """从完整消息恢复命令参数，兼容 AstrBot 普通 str 参数只取首段的问题。"""

        clean_parsed_prompt = str(parsed_prompt or "").strip()
        full_prompt = self._extract_command_arg_anywhere(
            str(getattr(event, "message_str", "") or ""),
            command_name,
        )
        if not full_prompt:
            return clean_parsed_prompt

        if not clean_parsed_prompt:
            return full_prompt

        first_token = full_prompt.split(maxsplit=1)[0] if full_prompt else ""
        if clean_parsed_prompt == first_token and clean_parsed_prompt != full_prompt:
            return full_prompt
        return clean_parsed_prompt

    @staticmethod
    def _detect_image_source(event: AstrMessageEvent) -> str:
        """识别编辑图来源，便于记录日志。"""

        message_components = list(getattr(event.message_obj, "message", []) or [])
        for component in message_components:
            component_type = str(getattr(component, "type", "") or "")
            normalized_type = component_type.split(".")[-1].lower()
            if normalized_type == "reply":
                reply_chain = list(getattr(component, "chain", []) or [])
                for item in reply_chain:
                    item_type = str(getattr(item, "type", "") or "")
                    normalized_item_type = item_type.split(".")[-1].lower()
                    if normalized_item_type == "image":
                        return "reply_image"
        return "current_message_image"

    @staticmethod
    def _new_task_id() -> str:
        """生成日志追踪用任务 ID。"""

        return uuid.uuid4().hex[:12]

    @staticmethod
    def _truncate_text(value: Any, limit: int = 200) -> str:
        """截断日志中的长文本。"""

        text = " ".join(str(value or "").split())
        if len(text) <= limit:
            return text
        return f"{text[: limit - 3].rstrip()}..."

    def _build_edit_pending_result(self, prompt: str):
        """构造编辑命令的处理中提示，优先走命令结果链以兼容 OneBot11。"""

        return MessageEventResult().message(self._build_pending_message(prompt))

    async def _send_pending_message(
        self,
        event: AstrMessageEvent,
        *,
        prompt: str,
    ) -> None:
        """在耗时图片任务开始前发送用户可见提示，避免命令长时间静默。"""

        await event.send(event.plain_result(self._build_pending_message(prompt)))

    def _build_pending_message(self, prompt: str) -> str:
        """生成统一的处理中提示文本。"""

        return (
            f'收到请求，prompt="{self._truncate_text(prompt, limit=60)}"，正在生成中...'
        )

    @staticmethod
    def _mask_secret(secret: Any) -> str:
        """对密钥做日志脱敏。"""

        text = str(secret or "").strip()
        if len(text) <= 10:
            return "*" * len(text)
        return f"{text[:6]}...{text[-4:]}"
