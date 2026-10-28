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

import httpx

CHAT_MODEL = "qwen/qwen3-30b-a3b-2507"
EMBEDDING_MODEL = "text-embedding-nomic-embed-text-v1.5"
CHAT_GPU_OFFLOAD = 0.35  # not settable over REST; see module docstring
CHAT_CONTEXT = 16384

LM_MODELS_PATH = "/api/v1/models"
LM_LOAD_PATH = "/api/v1/models/load"
LM_UNLOAD_PATH = "/api/v1/models/unload"
STT_PS_PATH = "/api/ps"


class ModelSwitcher:
    def __init__(
        self,
        *,
        lmstudio_base: str = "http://host.docker.internal:1234",
        stt_base: str = "http://speaches:8000",
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._lm = lmstudio_base.rstrip("/")
        self._stt = stt_base.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=300)

    async def _loaded(self) -> list[str]:
        response = await self._http.get(self._lm + LM_MODELS_PATH)
        response.raise_for_status()
        return [
            instance["id"]
            for model in response.json().get("models", [])
            for instance in model.get("loaded_instances", [])
        ]

    async def free_for_stt(self) -> None:
        for instance_id in await self._loaded():
            response = await self._http.post(
                self._lm + LM_UNLOAD_PATH, json={"instance_id": instance_id}
            )
            response.raise_for_status()

    async def free_for_vision(self) -> None:
        response = await self._http.get(self._stt + STT_PS_PATH)
        response.raise_for_status()
        for model_id in response.json().get("models", []):
            unload = await self._http.delete(f"{self._stt}{STT_PS_PATH}/{model_id}")
            unload.raise_for_status()

    async def restore_chat(self) -> None:
        loaded = set(await self._loaded())
        if CHAT_MODEL not in loaded:
            await self._load({"model": CHAT_MODEL, "context_length": CHAT_CONTEXT})
        if EMBEDDING_MODEL not in loaded:
            await self._load({"model": EMBEDDING_MODEL})

    async def _load(self, payload: dict) -> None:
        response = await self._http.post(self._lm + LM_LOAD_PATH, json=payload)
        response.raise_for_status()
