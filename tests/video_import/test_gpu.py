import json

import httpx
import pytest

from deeptutor.video_import import gpu
from deeptutor.video_import.gpu import ModelSwitcher


def _switcher(loaded: list[str], calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.method == "GET" and request.url.path == "/api/v1/models":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "key": key,
                            "loaded_instances": [{"id": key}] if key in loaded else [],
                        }
                        for key in (gpu.CHAT_MODEL, gpu.EMBEDDING_MODEL, "other")
                    ]
                },
            )
        return httpx.Response(200, json={})

    return ModelSwitcher(
        lmstudio_base="http://lm",
        stt_base="http://stt",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.asyncio
async def test_free_for_stt_unloads_every_loaded_model():
    calls: list = []
    await _switcher([gpu.CHAT_MODEL, gpu.EMBEDDING_MODEL], calls).free_for_stt()
    unloads = [c for c in calls if c[1] == "/api/v1/models/unload"]
    assert [c[2] for c in unloads] == [
        {"instance_id": gpu.CHAT_MODEL},
        {"instance_id": gpu.EMBEDDING_MODEL},
    ]


@pytest.mark.asyncio
async def test_free_for_vision_unloads_stt_models():
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json={"models": ["m/whisper"]})
        return httpx.Response(200, json={})

    switcher = ModelSwitcher(
        stt_base="http://stt",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await switcher.free_for_vision()
    assert ("DELETE", "/api/ps/m/whisper") in calls


@pytest.mark.asyncio
async def test_restore_chat_loads_chat_and_embedding_when_unloaded():
    calls: list = []
    await _switcher([], calls).restore_chat()
    loads = [c[2] for c in calls if c[1] == "/api/v1/models/load"]
    assert loads == [
        {"model": gpu.CHAT_MODEL, "context_length": 16384},
        {"model": gpu.EMBEDDING_MODEL},
    ]


@pytest.mark.asyncio
async def test_restore_chat_is_noop_when_loaded():
    calls: list = []
    await _switcher([gpu.CHAT_MODEL, gpu.EMBEDDING_MODEL], calls).restore_chat()
    assert not [c for c in calls if c[1] == "/api/v1/models/load"]
