from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT.parent
for candidate in (str(PARENT), str(ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)


def _load_module():
    return importlib.import_module("core.gateways.local_image_gateway")


def test_resolve_health_endpoint_standard():
    module = _load_module()

    # 标准 v1 后缀推导
    assert (
        module.resolve_health_endpoint("http://100.75.141.72:8000/v1")
        == "http://100.75.141.72:8000/health"
    )
    assert (
        module.resolve_health_endpoint("http://100.75.141.72:8000/v1/")
        == "http://100.75.141.72:8000/health"
    )

    # 根路径推导
    assert (
        module.resolve_health_endpoint("http://100.75.141.72:8000")
        == "http://100.75.141.72:8000/health"
    )
    assert (
        module.resolve_health_endpoint("http://100.75.141.72:8000/")
        == "http://100.75.141.72:8000/health"
    )

    # 已包含 health
    assert (
        module.resolve_health_endpoint("http://100.75.141.72:8000/health")
        == "http://100.75.141.72:8000/health"
    )


def test_resolve_health_endpoint_rejects_empty():
    module = _load_module()
    with pytest.raises(ValueError, match="图片接口地址不能为空"):
        module.resolve_health_endpoint("")


class _FakeResponse:
    def __init__(self, status: int = 200, text: str = "ok", json_data: dict | None = None):
        self.status = status
        self._text = text
        self._json_data = json_data or {}

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=None,  # type: ignore
                history=(),
                status=self.status,
                message=self._text,
            )

    async def text(self) -> str:
        return self._text

    async def read(self) -> bytes:
        return self._text.encode("utf-8")

    async def json(self) -> dict:
        return self._json_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _MockLocalSession:
    def __init__(
        self,
        health_status: int = 200,
        health_error: Exception | None = None,
        post_response: dict | None = None,
    ):
        self.health_status = health_status
        self.health_error = health_error
        self.post_response = post_response or {"output": [{"result": "base64"}]}
        self.get_called = False
        self.post_called = False
        self.closed = False

    def get(self, endpoint: str, **kwargs):
        self.get_called = True
        if self.health_error:
            raise self.health_error
        return _FakeResponse(status=self.health_status, text="healthy")

    def post(self, endpoint: str, **kwargs):
        self.post_called = True
        return _FakeResponse(status=200, json_data=self.post_response)


@pytest.mark.asyncio
async def test_local_gateway_success_flow():
    module = _load_module()
    session = _MockLocalSession(health_status=200)

    gateway = module.LocalImageGateway(
        base_url="http://100.75.141.72:8000/v1",
        api_key="test-key",
        timeout_seconds=120,
        session=session,
    )

    assert gateway.health_endpoint == "http://100.75.141.72:8000/health"

    # 执行生图请求，预期先通过健康检查然后完成生图
    res = await gateway.request_response({"prompt": "cat"})
    assert session.get_called is True
    assert session.post_called is True
    assert "output" in res


@pytest.mark.asyncio
async def test_local_gateway_aborts_immediately_on_timeout():
    module = _load_module()
    session = _MockLocalSession(health_error=asyncio.TimeoutError())

    gateway = module.LocalImageGateway(
        base_url="http://100.75.141.72:8000/v1",
        api_key="",
        timeout_seconds=180,  # 即使设置了 180s 的长超时
        health_timeout_seconds=2,
        session=session,
    )

    with pytest.raises(RuntimeError) as exc_info:
        await gateway.request_response({"prompt": "cat"})

    assert "本地图片服务健康检查超时" in str(exc_info.value)
    assert session.get_called is True
    assert session.post_called is False  # 立即中断，绝不发起生图调用


@pytest.mark.asyncio
async def test_local_gateway_aborts_on_non_2xx_status():
    module = _load_module()
    session = _MockLocalSession(health_status=503)

    gateway = module.LocalImageGateway(
        base_url="http://100.75.141.72:8000/v1",
        api_key="",
        session=session,
    )

    with pytest.raises(RuntimeError) as exc_info:
        await gateway.request_image_generation({"prompt": "cat"})

    assert "本地图片服务健康检查未通过: status=503" in str(exc_info.value)
    assert session.get_called is True
    assert session.post_called is False


@pytest.mark.asyncio
async def test_local_gateway_aborts_on_client_connection_error():
    module = _load_module()
    session = _MockLocalSession(
        health_error=aiohttp.ClientConnectionError("Connection refused")
    )

    gateway = module.LocalImageGateway(
        base_url="http://100.75.141.72:8000/v1",
        api_key="",
        session=session,
    )

    with pytest.raises(RuntimeError) as exc_info:
        await gateway.request_image_edit(data={}, files=[])

    assert "本地图片服务不可用或连接失败" in str(exc_info.value)
    assert session.get_called is True
    assert session.post_called is False


def test_main_plugin_gateway_selection():
    main_mod = importlib.import_module("astrbot_plugin_openai_image.main")
    provider_mod = importlib.import_module("core.provider_config")

    plugin = main_mod.OpenAIImagePlugin(context=SimpleNamespace(), config={})

    # local 开头的供应商使用 LocalImageGateway
    local_provider = provider_mod.ImageProviderConfig(
        provider_id="local_fastapi",
        name="本地接口",
        base_url="http://100.75.141.72:8000/v1",
        api_key="",
        proxy_url="",
        model="sdxl",
        endpoint_type="responses",
    )
    gw = plugin._create_image_gateway(local_provider)
    assert isinstance(gw, main_mod.LocalImageGateway)
    assert gw.health_endpoint == "http://100.75.141.72:8000/health"

    # 普通供应商使用标准 OpenAIImageGateway
    cloud_provider = provider_mod.ImageProviderConfig(
        provider_id="jucode",
        name="远程接口",
        base_url="https://api.jucode.top/v1",
        api_key="sk-test",
        proxy_url="",
        model="gpt-image-2",
        endpoint_type="images",
    )
    cloud_gw = plugin._create_image_gateway(cloud_provider)
    assert isinstance(cloud_gw, main_mod.OpenAIImageGateway)
    assert not isinstance(cloud_gw, main_mod.LocalImageGateway)

