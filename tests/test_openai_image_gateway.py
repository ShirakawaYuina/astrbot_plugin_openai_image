from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_module():
    return importlib.import_module("core.gateways.openai_image_gateway")


def test_resolve_endpoint_candidates_appends_responses_suffix():
    module = _load_module()

    candidates = module.resolve_endpoint_candidates("https://api.jucode.cn/v1")

    assert candidates == ["https://api.jucode.cn/v1/responses"]


def test_resolve_endpoint_candidates_keeps_full_responses_endpoint():
    module = _load_module()

    candidates = module.resolve_endpoint_candidates("https://cdn.jucode.top/v1/responses")

    assert candidates == ["https://cdn.jucode.top/v1/responses"]


def test_resolve_images_endpoint_appends_generations_suffix():
    module = _load_module()

    endpoint = module.resolve_images_generations_endpoint("https://api.jucode.cn/v1")

    assert endpoint == "https://api.jucode.cn/v1/images/generations"


def test_resolve_images_edits_endpoint_appends_edits_suffix():
    module = _load_module()

    endpoint = module.resolve_images_edits_endpoint("https://api.jucode.cn/v1")

    assert endpoint == "https://api.jucode.cn/v1/images/edits"


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    async def json(self) -> dict:
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeBinaryResponse:
    def __init__(self, payload: bytes, content_type: str = "image/png"):
        self._payload = payload
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self) -> None:
        return None

    async def read(self) -> bytes:
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    def __init__(self, payloads_by_endpoint: dict[str, dict]):
        self.payloads_by_endpoint = payloads_by_endpoint
        self.called_endpoints: list[str] = []
        self.recorded_payloads: list[dict] = []
        self.recorded_proxies: list[str | None] = []
        self.closed = False

    def post(
        self,
        endpoint: str,
        json: dict,
        headers: dict,
        proxy: str | None = None,
    ):
        self.called_endpoints.append(endpoint)
        self.recorded_payloads.append(json)
        self.recorded_proxies.append(proxy)
        return _FakeResponse(self.payloads_by_endpoint[endpoint])


class _FakeBinarySession:
    def __init__(self, payloads_by_url: dict[str, tuple[bytes, str]]):
        self.payloads_by_url = payloads_by_url
        self.called_urls: list[str] = []
        self.recorded_proxies: list[str | None] = []
        self.closed = False

    def get(self, image_url: str, proxy: str | None = None):
        self.called_urls.append(image_url)
        self.recorded_proxies.append(proxy)
        payload, content_type = self.payloads_by_url[image_url]
        return _FakeBinaryResponse(payload=payload, content_type=content_type)


class _FakeMultipartSession:
    def __init__(self, payloads_by_endpoint: dict[str, dict]):
        self.payloads_by_endpoint = payloads_by_endpoint
        self.called_endpoints: list[str] = []
        self.recorded_data = []
        self.recorded_headers: list[dict] = []
        self.recorded_proxies: list[str | None] = []
        self.closed = False

    def post(self, endpoint: str, data, headers: dict, proxy: str | None = None):
        self.called_endpoints.append(endpoint)
        self.recorded_data.append(data)
        self.recorded_headers.append(headers)
        self.recorded_proxies.append(proxy)
        return _FakeResponse(self.payloads_by_endpoint[endpoint])


@pytest.mark.asyncio
async def test_gateway_posts_json_payload_to_responses_endpoint():
    module = _load_module()
    session = _FakeSession(
        {
            "https://cdn.jucode.top/v1/responses": {
                "output": [
                    {
                        "type": "image_generation_call",
                        "result": "aGVsbG8=",
                    }
                ]
            },
        }
    )
    gateway = module.OpenAIImageGateway(
        base_url="https://cdn.jucode.top/v1",
        api_key="demo-key",
        session=session,
    )

    result = await gateway.request_response({"model": "demo", "input": "draw a cat"})

    assert result["output"][0]["result"] == "aGVsbG8="
    assert session.called_endpoints == ["https://cdn.jucode.top/v1/responses"]
    assert session.recorded_proxies == [None]


@pytest.mark.asyncio
async def test_gateway_posts_json_payload_to_images_generations_endpoint():
    module = _load_module()
    session = _FakeSession(
        {
            "https://cdn.jucode.top/v1/images/generations": {
                "data": [
                    {
                        "b64_json": "aGVsbG8=",
                    }
                ]
            },
        }
    )
    gateway = module.OpenAIImageGateway(
        base_url="https://cdn.jucode.top/v1",
        api_key="demo-key",
        session=session,
    )

    result = await gateway.request_image_generation(
        {"model": "gpt-image-2", "prompt": "生成一只猫"}
    )

    assert result["data"][0]["b64_json"] == "aGVsbG8="
    assert session.called_endpoints == [
        "https://cdn.jucode.top/v1/images/generations"
    ]
    assert session.recorded_payloads == [
        {"model": "gpt-image-2", "prompt": "生成一只猫"}
    ]
    assert session.recorded_proxies == [None]


@pytest.mark.asyncio
async def test_gateway_passes_proxy_to_json_image_request():
    module = _load_module()
    session = _FakeSession(
        {
            "https://cdn.jucode.top/v1/responses": {
                "output": [
                    {
                        "type": "image_generation_call",
                        "result": "aGVsbG8=",
                    }
                ]
            },
        }
    )
    gateway = module.OpenAIImageGateway(
        base_url="https://cdn.jucode.top/v1",
        api_key="demo-key",
        proxy_url=" http://127.0.0.1:7890 ",
        session=session,
    )

    await gateway.request_response({"model": "demo", "input": "draw a cat"})

    assert session.recorded_proxies == ["http://127.0.0.1:7890"]


@pytest.mark.asyncio
async def test_gateway_posts_multipart_payload_to_images_edits_endpoint():
    module = _load_module()
    session = _FakeMultipartSession(
        {
            "https://cdn.jucode.top/v1/images/edits": {
                "data": [
                    {
                        "b64_json": "aGVsbG8=",
                    }
                ]
            },
        }
    )
    gateway = module.OpenAIImageGateway(
        base_url="https://cdn.jucode.top/v1",
        api_key="demo-key",
        session=session,
    )

    result = await gateway.request_image_edit(
        data={
            "model": "gpt-image-2",
            "prompt": "将图中的角色换成星见雅",
            "response_format": "b64_json",
        },
        files=[
            ("first.png", b"first", "image/png"),
            ("second.jpg", b"second", "image/jpeg"),
        ],
    )

    assert result["data"][0]["b64_json"] == "aGVsbG8="
    assert session.called_endpoints == ["https://cdn.jucode.top/v1/images/edits"]
    assert session.recorded_headers == [{"Authorization": "Bearer demo-key"}]
    assert session.recorded_proxies == [None]
    fields = list(session.recorded_data[0]._fields)
    assert [field[0]["name"] for field in fields] == [
        "model",
        "prompt",
        "response_format",
        "image",
        "image",
    ]
    assert fields[0][2] == "gpt-image-2"
    assert fields[1][2] == "将图中的角色换成星见雅"
    assert fields[2][2] == "b64_json"
    assert fields[3][0]["filename"] == "first.png"
    assert fields[3][2] == b"first"
    assert fields[4][0]["filename"] == "second.jpg"
    assert fields[4][2] == b"second"


@pytest.mark.asyncio
async def test_gateway_passes_proxy_to_multipart_image_request():
    module = _load_module()
    session = _FakeMultipartSession(
        {
            "https://cdn.jucode.top/v1/images/edits": {
                "data": [
                    {
                        "b64_json": "aGVsbG8=",
                    }
                ]
            },
        }
    )
    gateway = module.OpenAIImageGateway(
        base_url="https://cdn.jucode.top/v1",
        api_key="demo-key",
        proxy_url="http://127.0.0.1:7890",
        session=session,
    )

    await gateway.request_image_edit(
        data={
            "model": "gpt-image-2",
            "prompt": "将图中的角色换成星见雅",
            "response_format": "b64_json",
        },
        files=[("first.png", b"first", "image/png")],
    )

    assert session.recorded_proxies == ["http://127.0.0.1:7890"]


@pytest.mark.asyncio
async def test_gateway_downloads_url_image_with_proxy():
    module = _load_module()
    session = _FakeBinarySession(
        {
            "https://file.example.com/generated/demo.png": (
                b"downloaded-image",
                "image/png; charset=utf-8",
            )
        }
    )
    gateway = module.OpenAIImageGateway(
        base_url="https://cdn.jucode.top/v1",
        api_key="demo-key",
        proxy_url="http://127.0.0.1:7890",
        session=session,
    )

    image_bytes, mime_type = await gateway.download_image(
        "https://file.example.com/generated/demo.png"
    )

    assert image_bytes == b"downloaded-image"
    assert mime_type == "image/png"
    assert session.called_urls == ["https://file.example.com/generated/demo.png"]
    assert session.recorded_proxies == ["http://127.0.0.1:7890"]


@pytest.mark.asyncio
async def test_gateway_raises_business_error_when_response_contains_error_object():
    module = _load_module()
    session = _FakeSession(
        {
            "https://cdn.jucode.top/v1/responses": {
                "error": {
                    "message": "access token 无效",
                    "type": "invalid_request_error",
                }
            },
        }
    )
    gateway = module.OpenAIImageGateway(
        base_url="https://cdn.jucode.top/v1",
        api_key="demo-key",
        session=session,
    )

    with pytest.raises(RuntimeError, match="access token 无效"):
        await gateway.request_response({"model": "demo", "input": "draw a cat"})
