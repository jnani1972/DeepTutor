# Video import: setup

Video import turns captured audio into a timed transcript using a speech-to-text (STT) service. The recommended setup is a local, GPU-accelerated Whisper server ([speaches](https://speaches.ai)) that speaks the OpenAI transcription API.

## 1. Start the speaches service

`docker-compose.yml` contains an optional `speaches` service.

- Image: `ghcr.io/speaches-ai/speaches:0.9.0-rc.3-cuda` (pinned, newest `*-cuda` tag when added)
- GPU: reserved through `deploy.resources.reservations.devices` (`driver: nvidia`, `count: 1`). Needs an NVIDIA GPU, current driver and the NVIDIA container runtime (Docker Desktop with WSL2 GPU support on Windows).
- Port: `127.0.0.1:8000:8000` (loopback only)
- Model cache: named volume `hf-hub-cache`
- Network: `deeptutor-network`, so DeepTutor reaches it as `http://speaches:8000/v1`

Environment variables set on the service:

| Variable | Value | Purpose |
| --- | --- | --- |
| `WHISPER__COMPUTE_TYPE` | `int8` | Works on Pascal GPUs (e.g. Quadro P4000, compute 6.1) and uses less VRAM |
| `WHISPER__INFERENCE_DEVICE` | `cuda` | Run Whisper on the GPU |
| `STT_MODEL_TTL` | `120` | Unload the model from the GPU 120 s after the last request (`-1` = never) |

Start only this service (it does not touch the other containers):

```
python scripts/docker_compose.py up -d --no-deps speaches
```

If your DeepTutor containers were started from a different compose project/network than this file creates, attach speaches to that network too, e.g. `docker network connect --alias speaches <your_network> speaches`.

## 2. Download the model

```
curl -X POST http://127.0.0.1:8000/v1/models/deepdml/faster-whisper-large-v3-turbo-ct2
curl http://127.0.0.1:8000/v1/models
```

## 3. Configure DeepTutor STT

In Settings -> Voice -> Speech-to-text, add a provider of type **OpenAI Compatible**:

- Base URL: `http://speaches:8000/v1`
- API key: leave empty
- Model: `deepdml/faster-whisper-large-v3-turbo-ct2`

Set it active. (Equivalent to `services.stt` in `data/user/settings/model_catalog.json`, with `"binding": "custom"`.)

Verify from the DeepTutor container:

```
docker exec deeptutor curl -sf http://speaches:8000/v1/models
```

## GPU memory note

An 8 GB GPU shared with a local chat model (for example LM Studio) can run out of VRAM while Whisper is loaded. The model TTL above releases the GPU shortly after transcription; lower `STT_MODEL_TTL` if the chat model needs the memory sooner. Transcription always runs in the language spoken in the video (no language is forced).

## Vision model

Key frames are described by a local vision model through DeepTutor's image description setting (Settings → Document parsing → Image description model).

Model: LM Studio `qwen/qwen3-vl-8b` (Qwen3 VL 8B Instruct, Q4_K_M GGUF, about 6.2 GB).

```
lms get qwen/qwen3-vl-8b -y
```

Add `qwen/qwen3-vl-8b` to the LM Studio profile's models in `model_catalog.json`, then set `image_description_model` in `document_parsing.json` to that profile and model id.

An 8 GB GPU cannot hold the chat model and the vision model together. Load the vision model only while frames are described, then restore the chat model:

```
lms unload --all
lms load qwen/qwen3-vl-8b -y
# ... describe frames ...
lms unload --all
lms load qwen/qwen3-30b-a3b-2507 -c 16384 --gpu 0.35 -y
lms load text-embedding-nomic-embed-text-v1.5 -y
```

Frames with no instructional content (talking head, blank screen) produce no note. A failed description batch is logged and skipped; the video import continues.
