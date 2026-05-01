"""命令文本解析。"""

from __future__ import annotations

import re
import shlex

from .models import ParsedCommand

SIZE_ALIASES = {
    "auto": "auto",
    "square": "1024x1024",
    "portrait": "1024x1536",
    "vertical": "1024x1536",
    "landscape": "1536x1024",
    "wide": "1536x1024",
    "2k-square": "2048x2048",
    "2k-landscape": "2560x1440",
    "2k-portrait": "1440x2560",
    "4k-landscape": "3840x2160",
    "4k-portrait": "2160x3840",
}

MIN_CUSTOM_SIZE_PIXELS = 655_360
MAX_CUSTOM_SIZE_PIXELS = 8_294_400
SIZE_PATTERN = re.compile(r"^(?P<width>\d{2,5})[xX*×](?P<height>\d{2,5})$")
QUALITY_VALUES = {"low", "medium", "high", "auto"}
MODERATION_VALUES = {"low", "auto"}
DEFAULT_IMAGE_QUALITY = "auto"
DEFAULT_IMAGE_MODERATION = "low"


def parse_command_payload(text: str) -> ParsedCommand:
    """解析图片命令中的数量、尺寸与提示词。"""

    raw_text = str(text or "").strip()
    if not raw_text:
        raise ValueError("提示词不能为空")

    tokens = _split_command_tokens(raw_text)
    count = 1
    size: str | None = None
    quality = DEFAULT_IMAGE_QUALITY
    moderation = DEFAULT_IMAGE_MODERATION

    if tokens and tokens[0].lstrip("-").isdigit():
        count = int(tokens.pop(0))
        if count <= 0:
            raise ValueError("数量必须为正整数")

    prompt_tokens: list[str] = []
    token_index = 0
    while token_index < len(tokens):
        token = tokens[token_index]
        if token in {"--size", "-s"}:
            if token_index + 1 >= len(tokens):
                raise ValueError("尺寸参数不能为空")
            size = normalize_output_size(tokens[token_index + 1])
            token_index += 2
            continue
        if token.startswith("--size="):
            size = normalize_output_size(token.partition("=")[2])
            token_index += 1
            continue
        if token in {"--quality", "-q"}:
            if token_index + 1 >= len(tokens):
                raise ValueError("质量参数不能为空")
            quality = normalize_image_quality(tokens[token_index + 1])
            token_index += 2
            continue
        if token.startswith("--quality="):
            quality = normalize_image_quality(token.partition("=")[2])
            token_index += 1
            continue
        if token in {"--moderation", "-m"}:
            if token_index + 1 >= len(tokens):
                raise ValueError("审核参数不能为空")
            moderation = normalize_image_moderation(tokens[token_index + 1])
            token_index += 2
            continue
        if token.startswith("--moderation="):
            moderation = normalize_image_moderation(token.partition("=")[2])
            token_index += 1
            continue
        prompt_tokens.append(token)
        token_index += 1

    prompt = " ".join(prompt_tokens).strip()
    if not prompt:
        raise ValueError("提示词不能为空")

    return ParsedCommand(
        count=count,
        prompt=prompt,
        size=size,
        quality=quality,
        moderation=moderation,
    )


def normalize_image_quality(quality: str | None) -> str:
    """规范化输出质量参数，避免把上游不支持的枚举值传给图片接口。"""

    clean_quality = str(quality or "").strip().lower()
    if clean_quality in QUALITY_VALUES:
        return clean_quality
    raise ValueError("质量参数必须为 low、medium、high 或 auto")


def normalize_image_moderation(moderation: str | None) -> str:
    """规范化图片审核参数，默认 low 用于降低误拦截概率。"""

    clean_moderation = str(moderation or "").strip().lower()
    if clean_moderation in MODERATION_VALUES:
        return clean_moderation
    raise ValueError("审核参数必须为 low 或 auto")


def normalize_output_size(size: str | None) -> str | None:
    """规范化输出图片尺寸，确保命令侧传入接口前已经完成基础校验。"""

    clean_size = str(size or "").strip().lower()
    if not clean_size:
        return None

    if clean_size in SIZE_ALIASES:
        return SIZE_ALIASES[clean_size]

    matched_size = SIZE_PATTERN.match(clean_size)
    if not matched_size:
        raise ValueError("尺寸必须为 auto、portrait、landscape 或 1024x1024 这类格式")

    width = int(matched_size.group("width"))
    height = int(matched_size.group("height"))
    if width % 16 != 0 or height % 16 != 0:
        raise ValueError("尺寸宽高必须是 16 的倍数")
    if width > 3840 or height > 3840:
        raise ValueError("尺寸宽高不能超过 3840")
    if max(width, height) / min(width, height) > 3:
        raise ValueError("尺寸长短边比例不能超过 3:1")
    total_pixels = width * height
    if not MIN_CUSTOM_SIZE_PIXELS <= total_pixels <= MAX_CUSTOM_SIZE_PIXELS:
        raise ValueError("尺寸总像素必须在 655360 到 8294400 之间")

    return f"{width}x{height}"


def _split_command_tokens(raw_text: str) -> list[str]:
    """按 shell 风格拆分命令参数，让带空格的提示词可继续用引号包裹。"""

    try:
        return shlex.split(raw_text)
    except ValueError as exc:
        raise ValueError("命令参数引号不完整") from exc
