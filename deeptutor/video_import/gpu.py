"""Swap models on a shared 8 GB GPU between import phases.

Routes (verified live against LM Studio 0.4.26 and speaches 0.9.0-rc.3):

* LM Studio ``GET  {lmstudio}/api/v1/models``        -> ``models[].loaded_instances[].id``
* LM Studio ``POST {lmstudio}/api/v1/models/unload`` body ``{"instance_id": id}``
* LM Studio ``POST {lmstudio}/api/v1/models/load``   body ``{"model", "context_length"?}``
  The REST load route rejects ``gpu`` (unrecognized_keys) and documents no GPU
  offload ratio, so ``CHAT_GPU_OFFLOAD`` cannot be sent; LM Studio applies the
  model's saved default offload instead.
* speaches ``GET    {stt}/api/ps``             -> ``{"models": [id, ...]}``
* speaches ``DELETE {stt}/api/ps/{model_id}``  unloads a model
"""

from __future__ import annotations

from collections.abc import Callable
import logging

import httpx

logger = logging.getLogger(__name__)

CHAT_MODEL = "qwen/qwen3-30b-a3b-2507"
EMBEDDING_MODEL = "text-embedding-nomic-embed-text-v1.5"
CHAT_GPU_OFFLOAD = 0.35  # not settable over REST; see module docstring
CHAT_CONTEXT = 16384

LM_MODELS_PATH = "/api/v1/models"
LM_LOAD_PATH = "/api/v1/models/load"
LM_UNLOAD_PATH = "/api/v1/models/unload"
STT_PS_PATH = "/api/ps"


def configured_vision_model() -> str | None:
    """Model id of the document-parsing ``image_description_model`` selection."""
    from deeptutor.services.config.runtime_settings import load_document_parsing_settings
    from deeptutor.services.llm.image_description import resolve_image_description_config

    selection = load_document_parsing_settings().get("image_description_model")
    if not selection:
        return None  # frames go to the chat model, which LM Studio loads on demand
    try:
        return resolve_image_description_config(selection).model
    except Exception:
        logger.warning("Could not resolve the image description model", exc_info=True)
        return None


class ModelSwitcher:
    """Each ``free_for_*``/``restore_chat`` leaves only that phase's models in VRAM."""

    def __init__(
        self,
        *,
        lmstudio_base: str = "http://host.docker.internal:1234",
        stt_base: str = "http://speaches:8000",
        http: httpx.AsyncClient | None = None,
        vision_model: Callable[[], str | None] = configured_vision_model,
    ) -> None:
        self._lm = lmstudio_base.rstrip("/")
        self._stt = stt_base.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=300)
        self._vision_model = vision_model

    async def _models(self) -> tuple[set[str], list[tuple[str, str]]]:
        """(every model key LM Studio knows, [(key, loaded instance id)])."""
        response = await self._http.get(self._lm + LM_MODELS_PATH)
        response.raise_for_status()
        models = response.json().get("models", [])
        known = {model.get("key", "") for model in models}
        loaded = [
            (model.get("key", ""), instance["id"])
            for model in models
            for instance in model.get("loaded_instances", [])
        ]
        return known, loaded

    async def _unload_except(self, keep: set[str]) -> set[str]:
        """Unload every LM Studio instance whose model is not in ``keep``;
        return the kept models that are loaded."""
        _, loaded = await self._models()
        for key, instance_id in loaded:
            if key in keep:
                continue
            response = await self._http.post(
                self._lm + LM_UNLOAD_PATH, json={"instance_id": instance_id}
            )
            response.raise_for_status()
        return {key for key, _ in loaded if key in keep}

    async def free_for_stt(self) -> None:
        await self._unload_except(set())

    async def free_for_vision(self) -> None:
        response = await self._http.get(self._stt + STT_PS_PATH)
        response.raise_for_status()
        for model_id in response.json().get("models", []):
            unload = await self._http.delete(f"{self._stt}{STT_PS_PATH}/{model_id}")
            unload.raise_for_status()
        vision = self._vision_model()
        kept = await self._unload_except({vision} if vision else set())
        if vision and vision not in kept:
            known, _ = await self._models()
            if vision in known:  # a cloud vision model is not LM Studio's to load
                await self._load({"model": vision})

    async def restore_chat(self) -> None:
        # The vision model (~6 GB) and the chat model do not fit together in 8 GB.
        loaded = await self._unload_except({CHAT_MODEL, EMBEDDING_MODEL})
        if CHAT_MODEL not in loaded:
            await self._load({"model": CHAT_MODEL, "context_length": CHAT_CONTEXT})
        if EMBEDDING_MODEL not in loaded:
            await self._load({"model": EMBEDDING_MODEL})

    async def _load(self, payload: dict) -> None:
        response = await self._http.post(self._lm + LM_LOAD_PATH, json=payload)
        response.raise_for_status()
