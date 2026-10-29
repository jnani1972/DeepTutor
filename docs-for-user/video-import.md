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

An 8 GB GPU cannot hold the chat model and the vision model together, so the import switches models itself over LM Studio's REST API:

- Before describing frames it unloads the STT model and every LM Studio model except the configured image description model, then loads that model (only if LM Studio lists it; a cloud vision model is left alone).
- Before indexing, and whenever a job stops, it unloads everything except the chat and embedding models and loads those two again (`qwen/qwen3-30b-a3b-2507` with 16384 context, `text-embedding-nomic-embed-text-v1.5`). The REST API cannot set a GPU offload ratio, so LM Studio uses the model's saved default; set `--gpu 0.35` as that default if you need it.

The manual equivalent:

```
lms unload --all
lms load qwen/qwen3-vl-8b -y
# ... describe frames ...
lms unload --all
lms load qwen/qwen3-30b-a3b-2507 -c 16384 --gpu 0.35 -y
lms load text-embedding-nomic-embed-text-v1.5 -y
```

Frames with no instructional content (talking head, blank screen) produce no note. A single failed description batch is logged and skipped, and the video continues. If **every** batch of a video fails (the vision model is unreachable), the job stops as `failed` with error code `vision_unavailable`; the frames are kept and the video stays transcribed, so **Resume** describes it again once the model is back.

## Using video import

### API

All routes live under `/api/video-import` and use the same sign-in as the rest of the app. Responses are job progress only; notes are never returned.

| Route | Purpose |
| --- | --- |
| `POST /preview` `{url?, folder?}` | `{kind, title, entries, total_seconds}`. 400 for a bad request, 422 `{code}` when the listing fails |
| `POST /jobs` `{url?, folder?, video_ids?, kb_name?}` | Create a job. 400 when `kb_name` is not a valid knowledge base name (it contains a reserved character such as `/`, `\`, `:`, `?`, `#` or `%`); leave it out for an automatic `yt-<title>` / `files-<folder>` name, or the knowledge base of an earlier import of the same playlist |
| `GET /jobs`, `GET /jobs/{id}` | List / read jobs (404 for an unknown id) |
| `POST /jobs/{id}/pause`, `/resume`, `/cancel` | Control a job |
| `POST /jobs/{id}/videos/{vid}/capture/start` | Begin browser capture; returns `{sample_seconds, audio_timeslice_ms}` |
| `POST /jobs/{id}/videos/{vid}/capture/audio` | Raw `audio/webm` chunk (max 5 MiB, 413 above; 409 if no capture is in progress) |
| `POST /jobs/{id}/videos/{vid}/capture/frame?t=<seconds>` | Raw `image/jpeg` (max 2 MiB, 413 above; 409 as above) |
| `POST /jobs/{id}/videos/{vid}/capture/finish` `{ended, ad_seconds, player_duration, error_code?}` | End the capture and hand the video to processing |

Pause and cancel on a job that is currently running only set a flag that the worker honours at its next checkpoint. The response can therefore still show the old status; poll `GET /jobs/{id}` for the settled state.

### CLI

JSON output, like `deeptutor config`:

```
deeptutor video-import preview (URL | --folder PATH)
deeptutor video-import start (URL | --folder PATH) [--kb NAME] [--only ID ...]
deeptutor video-import status [JOB_ID]
deeptutor video-import pause|resume|cancel JOB_ID
```

Browser capture itself is web-only. The CLI runs folder jobs end to end and waits for them to finish.

Only one process runs imports at a time, because they share job files and the GPU. The owner holds an OS file lock, `worker.lock` under `data/system/video_import/` (its pid is in `worker.lock.pid`), released automatically when the process exits.

- **Server running.** The server owns imports for as long as it runs. `start`, `pause`, `resume` and `cancel` in the CLI refuse with `worker_busy` and the server's pid; use the web app or the API instead. `preview` and `status` still work.
- **No server running.** The CLI takes ownership. `start --folder` and `resume` process the job until it finishes, pauses or fails, then exit.
- **Server started while a CLI run is processing.** The server waits for the CLI to exit before resuming interrupted jobs; until then the web app answers job changes with 409 `worker_busy`.
- Run the CLI where the server runs (for the Docker stack: `docker exec deeptutor deeptutor video-import ...`). A lock cannot be shared between the host and a container.

### Web: browser capture

Open `/learning/deep-import`, paste a YouTube URL (or a workspace folder path), pick videos, then start. On the capture page:

1. Click **Start: share this tab**.
2. When the browser asks, choose this tab and keep **Also share tab audio** on.
3. Keep the window visible.

The videos play one at a time and stay audible to you.

- **Upload failures.** A failed audio or frame upload is retried twice. If a video still loses an audio chunk, or 5 or more frame uploads fail, the page shows the error with **Retry this video**. Nothing is sent as complete, and the retry captures the video again from the start.
- **Sharing stopped.** If you stop sharing, the current video is re-queued and captured again when you share once more.
- **Pausing and buffering.** Audio recording pauses whenever the player is paused or buffering and resumes with playback, so the transcript stays aligned with the frames.
- **Do not seek.** Speech timestamps come from the recorded audio, frame timestamps from the player clock. Skipping forward or back during capture misaligns the two (and skipped parts are simply missing from the notes).
- **Video start.** About 0.5 to 0.75 s at the very start of each video is not recorded: capture starts only after the video's own clock has visibly advanced (the pre-roll ad gate below).

### Ads during capture

Ads inside the embedded player are filtered heuristically: while an ad plays, the main video clock does not advance.

- **Pre-roll ads.** Recording and frame sampling start only after the video's own clock has advanced. Pre-roll ads are therefore not captured.
- **Mid-roll ads, frames.** Detection needs about 2 s of a stalled clock. Frames are held for 2.5 s before upload, so frames from that stall window are dropped.
- **Mid-roll ads, audio.** Audio from the stall window is already recorded. Up to about 2 s of mid-roll ad audio may enter the transcript, and the timestamps after each such ad can be shifted by about 2 s.

## Running the fork image (switching from the GHCR stack)

`compose.yaml` runs the upstream image `ghcr.io/hkuds/deeptutor:latest`, which does not contain video import. `docker-compose.yml` builds the fork (`deeptutor-deeptutor:latest`), joins network `deeptutor-network`, and includes the `speaches` service. No script change is needed: `scripts/docker_compose.py` passes its arguments to `docker compose`, so naming the file selects the stack.

```
# 1. Build the fork image (does not touch running containers)
python scripts/docker_compose.py -f docker-compose.yml build deeptutor

# 2. Stop and remove the GHCR stack (compose.yaml is the default file)
python scripts/docker_compose.py down

# 3. Start the fork stack, including speaches
python scripts/docker_compose.py -f docker-compose.yml up -d
```

- `./data` is a bind mount in both files, so settings, knowledge bases and accounts persist.
- The fork stack publishes the same ports (from `system.json`). Always pass `-f docker-compose.yml` from now on, for example `python scripts/docker_compose.py -f docker-compose.yml logs -f deeptutor`.
- speaches is now on `deeptutor-network` with the app, so the manual link is no longer needed. If you had run `docker network connect --alias speaches <network> speaches`, remove it: `docker network disconnect <old_network> speaches`.
- Both files use compose project `deeptutor`, so the existing `speaches` container and its model-cache volume are kept; `up -d` reuses it.
- Verify: `docker exec deeptutor curl -sf http://speaches:8000/v1/models`
