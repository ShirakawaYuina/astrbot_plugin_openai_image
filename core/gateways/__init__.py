"""外部接口网关。"""

from .local_image_gateway import LocalImageGateway, resolve_health_endpoint
from .openai_image_gateway import OpenAIImageGateway

__all__ = [
    "LocalImageGateway",
    "OpenAIImageGateway",
    "resolve_health_endpoint",
]
