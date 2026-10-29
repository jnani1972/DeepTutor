from __future__ import annotations

from dataclasses import asdict
import subprocess
import sys
import textwrap

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from deeptutor.api.routers import video_import as router_module
from deeptutor.video_import.capture import MAX_AUDIO_CHUNK_BYTES, MAX_FRAME_BYTES
from deeptutor.video_import.listing import Listing, ListingError
from deeptutor.video_import.models import ImportTarget, PlaylistEntry, TargetKind


class FakeSink:
    def __init__(self) -> None:
        self.audio: list[bytes] = []
        self.frames: list[tuple[float, bytes]] = []

    def append_audio(self, chunk: bytes) -> int:
        self.audio.append(chunk)
        return len(chunk)

    def offer_frame(self, t: float, jpeg: bytes) -> bool:
        self.frames.append((t, jpeg))
        return True


class FakeJob:
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id

    def to_dict(self) -> dict:
        return {"job_id": self.job_id, "status": "pending", "videos": []}


class FakeRunner:
    def __init__(self) -> None:
        self.sink_obj = FakeSink()
        self.capturing = True
        self.calls: list[tuple] = []

    async def preview(self, *, url=None, folder=None):
        if url == "bad":
            raise ValueError("Give exactly one of url or folder")
        if url == "private":
            raise ListingError("video_private")
        entries = [PlaylistEntry("a", "A", 60, 1), PlaylistEntry("b", "B", 30, 2)]
        kind = TargetKind.FOLDER if folder else TargetKind.PLAYLIST
        return ImportTarget(kind, None, None, folder), Listing("T", entries)

    async def create(self, **kwargs):
        self.calls.append(("create", kwargs))
        return FakeJob("j1")

    def _job(self, job_id):
        if job_id != "j1":
            raise KeyError(job_id)
        return FakeJob(job_id)

    def list_jobs(self):
        return [FakeJob("j1")]

    def get_job(self, job_id):
        return self._job(job_id)

    async def pause(self, job_id):
        return self._job(job_id)

    async def resume(self, job_id):
        return self._job(job_id)

    async def cancel(self, job_id):
        return self._job(job_id)

    def begin_capture(self, job_id, video_id):
        self._job(job_id)
        return self.sink_obj

    def sink(self, job_id, video_id):
        if not self.capturing:
            raise KeyError(video_id)
        return self.sink_obj

    async def finish_capture(self, job_id, video_id, **kwargs):
        self.calls.append(("finish", job_id, video_id, kwargs))
        return self._job(job_id)


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def client(runner: FakeRunner) -> TestClient:
    app = FastAPI()
    app.include_router(router_module.router, prefix="/api/video-import")
    app.dependency_overrides[router_module.get_runner] = lambda: runner
    return TestClient(app)


BASE = "/api/video-import"


def _no_note_text(payload) -> None:
    if isinstance(payload, dict):
        assert "notes" not in payload and "text" not in payload
        for value in payload.values():
            _no_note_text(value)
    elif isinstance(payload, list):
        for value in payload:
            _no_note_text(value)


def test_preview_url_and_folder(client):
    body = client.post(f"{BASE}/preview", json={"url": "https://youtu.be/x"}).json()
    assert body["title"] == "T"
    assert body["total_seconds"] == 90
    assert body["entries"][0] == asdict(PlaylistEntry("a", "A", 60, 1))
    _no_note_text(body)
    folder = client.post(f"{BASE}/preview", json={"folder": "/media"}).json()
    assert folder["kind"] == "folder"


def test_preview_errors(client):
    assert client.post(f"{BASE}/preview", json={"url": "bad"}).status_code == 400
    response = client.post(f"{BASE}/preview", json={"url": "private"})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "video_private"


def test_create_and_lookup(client, runner):
    response = client.post(f"{BASE}/jobs", json={"url": "u", "video_ids": ["a"], "kb_name": "kb"})
    assert response.json()["job_id"] == "j1"
    assert runner.calls[0] == (
        "create",
        {"url": "u", "folder": None, "video_ids": ["a"], "kb_name": "kb"},
    )
    assert client.get(f"{BASE}/jobs").json() == [FakeJob("j1").to_dict()]
    assert client.get(f"{BASE}/jobs/j1").status_code == 200
    assert client.get(f"{BASE}/jobs/nope").status_code == 404
    for action in ("pause", "resume", "cancel"):
        assert client.post(f"{BASE}/jobs/j1/{action}").status_code == 200
        assert client.post(f"{BASE}/jobs/nope/{action}").status_code == 404


def test_create_value_error_is_400(client, runner):
    async def boom(**kwargs):
        raise ValueError("Nothing to import")

    runner.create = boom
    assert client.post(f"{BASE}/jobs", json={"folder": "/x"}).status_code == 400


def test_capture_start(client):
    body = client.post(f"{BASE}/jobs/j1/videos/a/capture/start").json()
    assert body == {"sample_seconds": 1, "audio_timeslice_ms": 5000}
    assert client.post(f"{BASE}/jobs/nope/videos/a/capture/start").status_code == 404


def test_audio_upload(client, runner):
    response = client.post(
        f"{BASE}/jobs/j1/videos/a/capture/audio",
        content=b"abc",
        headers={"content-type": "audio/webm"},
    )
    assert response.json() == {"bytes": 3}
    assert runner.sink_obj.audio == [b"abc"]


def test_audio_too_large_and_no_capture(client, runner):
    big = b"x" * (MAX_AUDIO_CHUNK_BYTES + 1)
    url = f"{BASE}/jobs/j1/videos/a/capture/audio"
    assert client.post(url, content=big).status_code == 413
    runner.capturing = False
    assert client.post(url, content=b"abc").status_code == 409


def test_frame_upload(client, runner):
    url = f"{BASE}/jobs/j1/videos/a/capture/frame"
    response = client.post(
        url, params={"t": 2.5}, content=b"jpeg", headers={"content-type": "image/jpeg"}
    )
    assert response.json() == {"kept": True}
    assert runner.sink_obj.frames == [(2.5, b"jpeg")]
    too_big = b"x" * (MAX_FRAME_BYTES + 1)
    assert client.post(url, params={"t": 1}, content=too_big).status_code == 413
    runner.capturing = False
    assert client.post(url, params={"t": 1}, content=b"j").status_code == 409


def test_finish(client, runner):
    response = client.post(
        f"{BASE}/jobs/j1/videos/a/capture/finish",
        json={"ended": True, "ad_seconds": 4.0, "player_duration": 120.0},
    )
    assert response.status_code == 200
    _no_note_text(response.json())
    assert runner.calls[-1] == (
        "finish",
        "j1",
        "a",
        {"ended": True, "ad_seconds": 4.0, "player_duration": 120.0, "error_code": None},
    )


def test_app_boots_without_yt_dlp():
    code = textwrap.dedent(
        """
        import sys
        sys.modules["yt_dlp"] = None
        sys.modules["yt_dlp.utils"] = None
        import deeptutor.api.main as main
        assert "/api/video-import/preview" in main.app.openapi()["paths"]
        assert "deeptutor.video_import.listing" not in sys.modules
        """
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr[-2000:]
