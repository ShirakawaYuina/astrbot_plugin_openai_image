"""英文扩写相关的纯函数工具。"""

from __future__ import annotations

from typing import Any


def validate_enhancement_config(config: dict[str, Any]) -> None:
    """校验英文扩写功能所需配置是否完整。"""

    provider_id = str(config.get("english_prompt_enhancer_model", "") or "").strip()
    if provider_id:
        return
    raise ValueError("请先在插件配置中选择英文扩写模型")


def build_enhancement_messages(prompt: str) -> list[dict[str, str]]:
    """构造用于英文扩写的消息。"""

    clean_prompt = str(prompt or "").strip()
    return [
        {
            "role": "system",
            "content": (
                "你是一个英文生图提示词扩写器。"
                "请将用户输入改写为适合图片生成模型使用的英文提示词，"
                "可以补充必要的主体细节、风格、镜头、光线、构图和质感描述，"
                "但必须严格围绕原意，不要偏题。"
                "只能输出英文提示词本身，不要输出解释、标题、引号、列表或额外说明。"
            ),
        },
        {
            "role": "user",
            "content": clean_prompt,
        },
    ]


def extract_enhancement_text(response_data: dict[str, Any]) -> str:
    """从英文扩写接口响应中提取文本结果。"""

    choices = response_data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("英文扩写结果为空")

    first_choice = choices[0]
    message = first_choice.get("message") if isinstance(first_choice, dict) else None
    content = message.get("content") if isinstance(message, dict) else ""
    enhanced_text = str(content or "").strip()
    if not enhanced_text:
        raise ValueError("英文扩写结果为空")
    return enhanced_text
