import asyncio
import json
from pathlib import Path
import subprocess
import sys

import pytest

from deeptutor.multi_user.context import (
    get_current_user_or_none,
    reset_current_user,
    set_current_user,
)
from deeptutor.multi_user.models import CurrentUser, UserScope
from deeptutor.reading.ingestion import TranscriptSegment
from deeptutor.video_import import ownership
from deeptutor.video_import import runner as runner_module
from deeptutor.video_import.capture import CaptureSink
from deeptutor.video_import.describe import VisionUnavailableError
from deeptutor.video_import.files import file_video_id
from deeptutor.video_import.listing import Listing
from deeptutor.video_import.models import (
    CaptureMode,
    JobStatus,
    PlaylistEntry,
    VideoStage,
)
from deeptutor.video_import.notes import Chapter, FrameNote
from deeptutor.video_import.runner import ImportRunner
from deeptutor.video_import.store import ImportJobStore

PLAYLIST_URL = "https://www.youtube.com/playlist?list=PL123"


class FakeSwitcher:
    def __init__(self, calls: list) -> None:
        self.calls = calls

    async def free_for_stt(self) -> None:
        self.calls.append("free_for_stt")

    async def free_for_vision(self) -> None:
        self.calls.append("free_for_vision")

    async def restore_chat(self) -> None:
        self.calls.append("restore_chat")


class FakeFileCapture:
    def __init__(self, jpeg_of) -> None:
        self.jpeg_of = jpeg_of
        self.sources: list[Path] = []

    async def capture(self, entry, out_dir, *, source):
        self.sources.append(source)
        sink = CaptureSink(out_dir)
        sink.append_audio(b"audio")
        sink.offer_frame(0, self.jpeg_of(0))
        sink.offer_frame(5, self.jpeg_of(255))
        return sink.finish(ad_seconds=0.0, description="from file")


class Harness:
    def __init__(
        self, tmp_path: Path, jpeg_of, *, store=None, index_path=None, claim_poll_seconds=5.0
    ) -> None:
        self.calls: list = []
        self.kb_users: list = []
        self.listing = Listing(
            "My List",
            [PlaylistEntry("a", "A", 100, 1), PlaylistEntry("b", "B", 100, 2)],
        )
        self.speech = {"a": "hello", "b": "world"}
        self.store = ImportJobStore(root=tmp_path / "vi")
        self.file_capture = FakeFileCapture(jpeg_of)
        self.runner = ImportRunner(
            store or self.store,
            index_path=index_path,
            file_capture=self.file_capture,
            details=self.details,
            transcribe=self.transcribe,
            describe=self.describe,
            add_to_kb=self.add_to_kb,
            switcher=FakeSwitcher(self.calls),
            lister=self.lister,
            folder_lister=self.folder_lister,
            claim_poll_seconds=claim_poll_seconds,
        )
        self.on_transcribe = None
        self.vision_down = False

    async def details(self, video_id):
        return [Chapter(0.0, "Intro")], f"about {video_id}"

    async def transcribe(self, audio_path: Path):
        video_id = audio_path.parent.name
        self.calls.append(f"transcribe {video_id}")
        assert audio_path.read_bytes()
        if self.on_transcribe:
            await self.on_transcribe(video_id)
        text = self.speech.get(video_id, "")
        return [TranscriptSegment(1.0, 2.0, text)] if text else []

    async def describe(self, frames):
        video_id = frames[0].path.parent.parent.name if frames else "-"
        self.calls.append(f"describe {video_id}")
        assert all(frame.path.exists() for frame in frames)
        if self.vision_down:
            raise VisionUnavailableError("connection refused")
        return [FrameNote(frames[0].t_seconds, f"slide of {video_id}")] if frames else []

    async def add_to_kb(self, kb_name, notes_files):
        self.calls.append(("add_to_kb", kb_name, [p.name for p in notes_files]))
        assert all(p.exists() for p in notes_files)
        self.kb_users.append(get_current_user_or_none())

    async def lister(self, target):
        return self.listing

    async def folder_lister(self, folder):
        folder = Path(folder)
        files = sorted(folder.glob("*.mp3"))
        return Listing(
            folder.name,
            [
                PlaylistEntry(file_video_id(folder, p), p.stem, 60, i)
                for i, p in enumerate(files, start=1)
            ],
        )

    async def play(self, job_id, video_id, jpeg_of, *, until=95, ended=True):
        sink = self.runner.begin_capture(job_id, video_id)
        sink.append_audio(b"chunk")
        for t, grey in ((0, 0), (until // 2, 128), (until, 255)):
            sink.offer_frame(t, jpeg_of(grey))
        return await self.runner.finish_capture(
            job_id, video_id, ended=ended, ad_seconds=3.0, player_duration=100.0
        )


@pytest.fixture(autouse=True)
def stt_configured(monkeypatch):
    monkeypatch.setattr(runner_module, "_probe_stt_configuration", lambda: "")


@pytest.fixture
def user(tmp_path):
    owner = CurrentUser(
        id="u1",
        username="u1",
        role="user",
        scope=UserScope(kind="user", user_id="u1", root=tmp_path / "u1"),
    )
    token = set_current_user(owner)
    yield owner
    reset_current_user(token)


@pytest.fixture
def h(tmp_path, jpeg_of):
    return Harness(tmp_path, jpeg_of)


def _stages(job):
    return {v.video_id: v.stage for v in job.videos}


@pytest.mark.asyncio
async def test_browser_job_waits_for_playback_then_processes_automatically(
    h, jpeg_of, user, tmp_path
):
    job = await h.runner.create(url=PLAYLIST_URL)
    assert job.status is JobStatus.waiting_for_playback
    assert job.capture_mode is CaptureMode.browser
    assert job.kb_name == "yt-my-list"

    # Capture finishes outside the creating request's user context; the
    # worker must still index into the owner's knowledge bases.
    token = set_current_user(
        CurrentUser("other", "other", "user", UserScope("user", "other", tmp_path / "o"))
    )
    try:
        await h.play(job.job_id, "a", jpeg_of)
        assert h.store.load(job.job_id).status is JobStatus.waiting_for_playback
        await h.play(job.job_id, "b", jpeg_of)
        await h.runner.drain()
    finally:
        reset_current_user(token)

    assert h.calls == [
        "free_for_stt",
        "transcribe a",
        "transcribe b",
        "free_for_vision",
        "describe a",
        "describe b",
        "restore_chat",
        ("add_to_kb", "yt-my-list", ["001-a.md", "002-b.md"]),
    ]
    assert h.kb_users == [user]
    job = h.store.load(job.job_id)
    assert job.status is JobStatus.done
    assert _stages(job) == {"a": VideoStage.done, "b": VideoStage.done}
    assert job.videos[0].speech_cues == 1 and job.videos[0].frame_notes == 1
    notes = h.store.notes_path(job.job_id, h.listing.entries[0]).read_text(encoding="utf-8")
    assert "Intro" in notes and "about a" in notes and "hello" in notes and "slide of a" in notes
    work = h.store.root / "work" / job.job_id
    assert not list(work.rglob("audio.webm")) and not list(work.rglob("*.jpg"))


@pytest.mark.asyncio
async def test_incomplete_capture_requeues_video(h, jpeg_of):
    job = await h.runner.create(url=PLAYLIST_URL)
    job = await h.play(job.job_id, "a", jpeg_of, until=30, ended=False)
    video = job.videos[0]
    assert video.stage is VideoStage.queued
    assert video.error_code == "incomplete_capture"
    assert job.status is JobStatus.waiting_for_playback
    video_dir = h.store.video_dir(job.job_id, "a")
    assert not (video_dir / "audio.webm").exists()
    assert not list((video_dir / "frames").glob("*.jpg"))
    with pytest.raises(KeyError):
        h.runner.sink(job.job_id, "a")


@pytest.mark.asyncio
async def test_player_error_skips_video_with_code(h, jpeg_of):
    job = await h.runner.create(url=PLAYLIST_URL)
    h.runner.begin_capture(job.job_id, "a")
    job = await h.runner.finish_capture(
        job.job_id, "a", ended=False, ad_seconds=0, player_duration=0, error_code="embed_blocked"
    )
    assert job.videos[0].stage is VideoStage.skipped
    assert job.videos[0].error_code == "embed_blocked"

    await h.play(job.job_id, "b", jpeg_of)
    await h.runner.drain()
    job = h.store.load(job.job_id)
    assert _stages(job) == {"a": VideoStage.skipped, "b": VideoStage.done}
    assert job.status is JobStatus.done


@pytest.mark.asyncio
async def test_file_job_captures_and_processes_unattended(h, tmp_path):
    folder = tmp_path / "lectures"
    folder.mkdir()
    for name in ("1 intro.mp3", "2 more.mp3"):
        (folder / name).write_bytes(b"fake")
    job = await h.runner.create(folder=str(folder))
    assert job.capture_mode is CaptureMode.file
    assert job.kb_name == "files-lectures"
    await h.runner.drain()

    job = h.store.load(job.job_id)
    assert job.status is JobStatus.done
    assert [v.stage for v in job.videos] == [VideoStage.done, VideoStage.done]
    assert h.file_capture.sources == [folder / "1 intro.mp3", folder / "2 more.mp3"]
    assert h.calls[0] == "free_for_stt"
    assert h.calls[-1][0] == "add_to_kb"


@pytest.mark.asyncio
async def test_video_without_speech_finishes_done(h, jpeg_of):
    h.speech = {}
    job = await h.runner.create(url=PLAYLIST_URL)
    await h.play(job.job_id, "a", jpeg_of)
    await h.play(job.job_id, "b", jpeg_of)
    await h.runner.drain()
    job = h.store.load(job.job_id)
    assert job.status is JobStatus.done
    assert job.videos[0].speech_cues == 0
    notes = h.store.notes_path(job.job_id, h.listing.entries[0]).read_text(encoding="utf-8")
    assert "No speech was detected" in notes


@pytest.mark.asyncio
async def test_recover_requeues_interrupted_capture_only(h, jpeg_of, tmp_path):
    job = await h.runner.create(url=PLAYLIST_URL)
    await h.play(job.job_id, "a", jpeg_of)
    h.runner.begin_capture(job.job_id, "b")

    fresh = Harness(tmp_path, jpeg_of)  # a restarted process: no in-memory sinks
    await fresh.runner.recover()
    job = fresh.store.load(job.job_id)
    assert _stages(job) == {"a": VideoStage.captured, "b": VideoStage.queued}
    assert job.status is JobStatus.waiting_for_playback
    with pytest.raises(KeyError):
        fresh.runner.sink(job.job_id, "b")
    await fresh.runner.drain()
    assert fresh.calls == []

    await fresh.play(job.job_id, "b", jpeg_of)
    await fresh.runner.drain()
    assert fresh.store.load(job.job_id).status is JobStatus.done


@pytest.mark.asyncio
async def test_reimport_skips_done_videos_and_reuses_kb(h, jpeg_of):
    first = await h.runner.create(url=PLAYLIST_URL, kb_name="my-course")
    await h.play(first.job_id, "a", jpeg_of)
    h.runner.begin_capture(first.job_id, "b")
    await h.runner.finish_capture(
        first.job_id, "b", ended=False, ad_seconds=0, player_duration=0, error_code="removed"
    )
    await h.runner.drain()

    again = await h.runner.create(url=PLAYLIST_URL)
    assert again.kb_name == "my-course"
    assert _stages(again) == {"a": VideoStage.skipped, "b": VideoStage.queued}
    assert again.videos[0].error_code == "already_imported"


@pytest.mark.asyncio
async def test_pause_between_videos_restores_chat(h, jpeg_of):
    job = await h.runner.create(url=PLAYLIST_URL)

    async def pause_after_first(video_id):
        if video_id == "a":
            await h.runner.pause(job.job_id)

    h.on_transcribe = pause_after_first
    await h.play(job.job_id, "a", jpeg_of)
    await h.play(job.job_id, "b", jpeg_of)
    await h.runner.drain()

    paused = h.store.load(job.job_id)
    assert paused.status is JobStatus.paused
    assert _stages(paused) == {"a": VideoStage.transcribed, "b": VideoStage.captured}
    assert h.calls == ["free_for_stt", "transcribe a", "restore_chat"]

    h.on_transcribe = None
    await h.runner.resume(job.job_id)
    await h.runner.drain()
    assert h.store.load(job.job_id).status is JobStatus.done
    assert h.calls.count("transcribe a") == 1


@pytest.mark.asyncio
async def test_missing_stt_fails_job_and_restores_chat(h, jpeg_of, monkeypatch):
    monkeypatch.setattr(runner_module, "_probe_stt_configuration", lambda: "No STT model")
    job = await h.runner.create(url=PLAYLIST_URL)
    await h.play(job.job_id, "a", jpeg_of)
    await h.play(job.job_id, "b", jpeg_of)
    await h.runner.drain()
    job = h.store.load(job.job_id)
    assert job.status is JobStatus.failed
    assert (job.error_code, job.error_detail) == ("stt_not_configured", "No STT model")
    assert h.calls == ["restore_chat"]


@pytest.mark.asyncio
async def test_transcription_failure_fails_only_that_video(h, jpeg_of):
    async def boom(video_id):
        if video_id == "a":
            raise RuntimeError("stt down")

    h.on_transcribe = boom
    job = await h.runner.create(url=PLAYLIST_URL)
    await h.play(job.job_id, "a", jpeg_of)
    await h.play(job.job_id, "b", jpeg_of)
    await h.runner.drain()
    job = h.store.load(job.job_id)
    assert job.status is JobStatus.done
    assert _stages(job) == {"a": VideoStage.failed, "b": VideoStage.done}
    assert "stt down" in job.videos[0].error_detail
    # The job ended done, so nothing can retry the failed video: its media goes.
    assert not (h.store.root / "work" / job.job_id).exists()


@pytest.mark.asyncio
async def test_cancel_removes_captured_media(h, jpeg_of):
    job = await h.runner.create(url=PLAYLIST_URL)
    await h.play(job.job_id, "a", jpeg_of)
    h.runner.begin_capture(job.job_id, "b")
    assert list((h.store.root / "work" / job.job_id).rglob("*.jpg"))

    job = await h.runner.cancel(job.job_id)
    assert job.status is JobStatus.cancelled
    assert not (h.store.root / "work" / job.job_id).exists()
    with pytest.raises(KeyError):
        h.runner.sink(job.job_id, "b")


def _user(uid: str, tmp_path: Path) -> CurrentUser:
    return CurrentUser(uid, uid, "user", UserScope("user", uid, tmp_path / uid))


@pytest.mark.asyncio
async def test_jobs_are_scoped_to_their_owner(tmp_path, jpeg_of):
    stores: dict[str, ImportJobStore] = {}

    def scope_store():
        uid = get_current_user_or_none().id
        if uid not in stores:
            stores[uid] = ImportJobStore(root=tmp_path / "scopes" / uid)
        return stores[uid]

    def harness():
        return Harness(tmp_path, jpeg_of, store=scope_store, index_path=tmp_path / "index.json")

    h = harness()
    alice, bob = _user("alice", tmp_path), _user("bob", tmp_path)

    token = set_current_user(alice)
    try:
        first = await h.runner.create(url=PLAYLIST_URL, kb_name="alice-course")
        await h.play(first.job_id, "a", jpeg_of)
        await h.play(first.job_id, "b", jpeg_of)
        await h.runner.drain()
        assert stores["alice"].load(first.job_id).status is JobStatus.done
        interrupted_a = await h.runner.create(url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        h.runner.begin_capture(interrupted_a.job_id, "a")
    finally:
        reset_current_user(token)

    token = set_current_user(bob)
    try:
        job = await h.runner.create(url=PLAYLIST_URL)
        assert job.kb_name == "yt-my-list"
        assert _stages(job) == {"a": VideoStage.queued, "b": VideoStage.queued}
        with pytest.raises(KeyError):
            await h.runner.pause(first.job_id)
        with pytest.raises(KeyError):
            h.runner.begin_capture(first.job_id, "a")
        h.runner.begin_capture(job.job_id, "a")
    finally:
        reset_current_user(token)

    fresh = harness()  # restart, with no user context
    await fresh.runner.recover()
    for uid, job_id in (("alice", interrupted_a.job_id), ("bob", job.job_id)):
        recovered = stores[uid].load(job_id)
        assert recovered.status is JobStatus.waiting_for_playback
        assert recovered.videos[0].stage is VideoStage.queued


@pytest.mark.asyncio
async def test_invalid_kb_name_is_rejected_before_anything_is_saved(h):
    with pytest.raises(ValueError, match="reserved characters"):
        await h.runner.create(url=PLAYLIST_URL, kb_name="C++: what? #1")
    assert h.store.list() == []


@pytest.mark.asyncio
async def test_default_kb_name_from_a_title_with_reserved_characters_is_valid(h):
    h.listing = Listing("C++: Intro? #1 / 100% <fast>", h.listing.entries)
    job = await h.runner.create(url=PLAYLIST_URL)
    assert job.kb_name == "yt-c-intro-1-100-fast"


@pytest.mark.asyncio
async def test_unended_capture_without_player_duration_uses_listing_duration(h, jpeg_of):
    job = await h.runner.create(url=PLAYLIST_URL)
    sink = h.runner.begin_capture(job.job_id, "a")
    sink.offer_frame(5, jpeg_of(0))
    job = await h.runner.finish_capture(
        job.job_id, "a", ended=False, ad_seconds=0, player_duration=0
    )
    assert job.videos[0].stage is VideoStage.queued
    assert job.videos[0].error_detail == "Captured 5s of 100s"


@pytest.mark.asyncio
async def test_unended_capture_with_no_known_duration_is_incomplete(h, jpeg_of):
    h.listing = Listing("L", [PlaylistEntry("a", "A", 0, 1)])
    job = await h.runner.create(url=PLAYLIST_URL)
    sink = h.runner.begin_capture(job.job_id, "a")
    sink.offer_frame(500, jpeg_of(0))
    job = await h.runner.finish_capture(
        job.job_id, "a", ended=False, ad_seconds=0, player_duration=0
    )
    assert job.videos[0].error_code == "incomplete_capture"


@pytest.mark.asyncio
async def test_unreachable_vision_model_fails_job_and_keeps_frames(h, jpeg_of):
    h.vision_down = True
    job = await h.runner.create(url=PLAYLIST_URL)
    await h.play(job.job_id, "a", jpeg_of)
    await h.play(job.job_id, "b", jpeg_of)
    await h.runner.drain()

    job = h.store.load(job.job_id)
    assert job.status is JobStatus.failed
    assert (job.error_code, job.error_detail) == ("vision_unavailable", "connection refused")
    assert _stages(job) == {"a": VideoStage.transcribed, "b": VideoStage.transcribed}
    assert list((h.store.video_dir(job.job_id, "a") / "frames").glob("*.jpg"))
    assert h.calls[-1] == "restore_chat"

    h.vision_down = False
    await h.runner.resume(job.job_id)
    await h.runner.drain()
    job = h.store.load(job.job_id)
    assert job.status is JobStatus.done
    assert job.videos[0].frame_notes == 1


@pytest.mark.asyncio
async def test_saved_transcript_without_audio_is_reused(h, jpeg_of):
    job = await h.runner.create(url=PLAYLIST_URL)
    await h.play(job.job_id, "a", jpeg_of)
    # A crash after segments.json was written and audio deleted, before "transcribed".
    video_dir = h.store.video_dir(job.job_id, "a")
    (video_dir / "segments.json").write_text(json.dumps([[1.0, 2.0, "kept words"]]))
    (video_dir / "audio.webm").unlink()
    await h.play(job.job_id, "b", jpeg_of)
    await h.runner.drain()

    job = h.store.load(job.job_id)
    assert job.status is JobStatus.done
    assert "transcribe a" not in h.calls
    assert job.videos[0].speech_cues == 1
    notes = h.store.notes_path(job.job_id, h.listing.entries[0]).read_text(encoding="utf-8")
    assert "kept words" in notes


def _hold_lock(lock_path: Path) -> subprocess.Popen:
    """Another process (a server or CLI run) that owns video imports."""
    code = (
        "import sys; from pathlib import Path; from deeptutor.video_import import ownership; "
        "assert ownership.claim(Path(sys.argv[1])); print('held', flush=True); sys.stdin.read()"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout.readline().strip() == "held"
    return process


@pytest.mark.asyncio
async def test_another_process_owning_imports_blocks_changes_and_defers_recovery(
    h, jpeg_of, tmp_path
):
    job = await h.runner.create(url=PLAYLIST_URL)
    await h.play(job.job_id, "a", jpeg_of)
    h.runner.begin_capture(job.job_id, "b")
    lock_path = h.store.root / "worker.lock"
    ownership.release(lock_path)  # this "process" exits

    other = _hold_lock(lock_path)
    try:
        fresh = Harness(tmp_path, jpeg_of, claim_poll_seconds=0.05)
        for attempt in (
            fresh.runner.create(url=PLAYLIST_URL),
            fresh.runner.pause(job.job_id),
            fresh.runner.resume(job.job_id),
            fresh.runner.cancel(job.job_id),
        ):
            with pytest.raises(ownership.WorkerBusyError, match=r"pid \d+"):
                await attempt
        with pytest.raises(ownership.WorkerBusyError):
            fresh.runner.begin_capture(job.job_id, "b")

        await fresh.runner.recover()
        assert fresh.store.load(job.job_id).videos[1].stage is VideoStage.capturing
    finally:
        other.stdin.close()
        other.wait(timeout=30)
        other.stdout.close()

    await asyncio.wait_for(fresh.runner._claim_task, timeout=10)
    assert fresh.store.load(job.job_id).videos[1].stage is VideoStage.queued
    await fresh.runner.pause(job.job_id)
