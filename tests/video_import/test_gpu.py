import json

import httpx
import pytest

from deeptutor.video_import import gpu
from deeptutor.video_import.gpu import ModelSwitcher

VISION = "qwen/qwen3-vl-8b"


def _switcher(loaded: list[str], calls: list, *, vision=None, stt_models=()):
    """A fake LM Studio + speaches; ``loaded`` changes as models load/unload."""
    loaded = list(loaded)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        path = request.url.path
        if request.method == "GET" and path == "/api/v1/models":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "key": key,
                            "loaded_instances": [{"id": key}] if key in loaded else [],
                        }
                        for key in (gpu.CHAT_MODEL, gpu.EMBEDDING_MODEL, VISION, "other")
                    ]
                },
            )
        if path == "/api/v1/models/unload":
            loaded.remove(body["instance_id"])
        if path == "/api/v1/models/load":
            loaded.append(body["model"])
        if request.method == "GET" and path == "/api/ps":
            return httpx.Response(200, json={"models": list(stt_models)})
        return httpx.Response(200, json={})

    return ModelSwitcher(
        lmstudio_base="http://lm",
        stt_base="http://stt",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        vision_model=lambda: vision,
    )


def _bodies(calls: list, path: str) -> list:
    return [c[2] for c in calls if c[1] == path]


@pytest.mark.asyncio
async def test_free_for_stt_unloads_every_loaded_model():
    calls: list = []
    await _switcher([gpu.CHAT_MODEL, gpu.EMBEDDING_MODEL], calls).free_for_stt()
    assert _bodies(calls, "/api/v1/models/unload") == [
        {"instance_id": gpu.CHAT_MODEL},
        {"instance_id": gpu.EMBEDDING_MODEL},
    ]


@pytest.mark.asyncio
async def test_free_for_vision_unloads_stt_and_chat_then_loads_vision():
    calls: list = []
    switcher = _switcher([gpu.CHAT_MODEL], calls, vision=VISION, stt_models=["m/whisper"])
    await switcher.free_for_vision()
    assert ("DELETE", "/api/ps/m/whisper", None) in calls
    assert _bodies(calls, "/api/v1/models/unload") == [{"instance_id": gpu.CHAT_MODEL}]
    assert _bodies(calls, "/api/v1/models/load") == [{"model": VISION}]


@pytest.mark.asyncio
async def test_free_for_vision_skips_loading_unknown_or_unconfigured_models():
    calls: list = []
    await _switcher([], calls, vision="gpt-4o").free_for_vision()
    await _switcher([], calls, vision=None).free_for_vision()
    assert not _bodies(calls, "/api/v1/models/load")


@pytest.mark.asyncio
async def test_restore_chat_unloads_vision_then_loads_chat_and_embedding():
    calls: list = []
    await _switcher([VISION], calls).restore_chat()
    assert _bodies(calls, "/api/v1/models/unload") == [{"instance_id": VISION}]
    assert _bodies(calls, "/api/v1/models/load") == [
        {"model": gpu.CHAT_MODEL, "context_length": 16384},
        {"model": gpu.EMBEDDING_MODEL},
    ]


@pytest.mark.asyncio
async def test_restore_chat_is_noop_when_loaded():
    calls: list = []
    await _switcher([gpu.CHAT_MODEL, gpu.EMBEDDING_MODEL], calls).restore_chat()
    assert not [c for c in calls if c[0] == "POST"]


def test_configured_vision_model_reads_the_image_description_selection(monkeypatch):
    from types import SimpleNamespace

    from deeptutor.services.config import runtime_settings
    from deeptutor.services.llm import image_description

    selection = {"profile_id": "p", "model_id": "m"}
    monkeypatch.setattr(
        runtime_settings,
        "load_document_parsing_settings",
        lambda: {"image_description_model": selection},
    )
    monkeypatch.setattr(
        image_description,
        "resolve_image_description_config",
        lambda chosen: SimpleNamespace(model=VISION if chosen is selection else "?"),
    )
    assert gpu.configured_vision_model() == VISION

    monkeypatch.setattr(runtime_settings, "load_document_parsing_settings", lambda: {})
    assert gpu.configured_vision_model() is None
