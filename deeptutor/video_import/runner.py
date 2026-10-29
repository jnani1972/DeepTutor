"""Owns import jobs end to end: capture sessions, processing phases, recovery.

Browser jobs are captured in the owner's own tab (``begin_capture`` /
``finish_capture``); file jobs are captured here with ffmpeg. Once nothing is
left to capture, a single background worker runs the GPU-bound phases one job
at a time: speech-to-text, then vision, then knowledge-base indexing. Every
state change is persisted, so a paused, failed or interrupted job resumes from
its saved video stages.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import shutil
import threading
import uuid

from deeptutor.multi_user.context import (
    get_current_user_or_none,
    reset_current_user,
    set_current_user,
)
from deeptutor.multi_user.models import CurrentUser, UserScope
from deeptutor.multi_user.paths import SYSTEM_ROOT, get_current_path_service
from deeptutor.reading.ingestion import TranscriptSegment, _probe_stt_configuration
from deeptutor.services.file_io import atomic_write_json

from .capture import CapturedFrame, CaptureResult, CaptureSink
from .describe import describe_frames
from .files import FileCapture, list_media_folder, media_path
from .gpu import ModelSwitcher
from .kb import add_notes_to_kb
from .listing import Listing, fetch_video_details, list_target
from .models import (
    CaptureMode,
    ImportJob,
    ImportTarget,
    JobStatus,
    PlaylistEntry,
    TargetKind,
    VideoStage,
    VideoState,
)
from .notes import Chapter, build_notes_markdown
from .store import ImportJobStore
from .transcribe import transcribe_capture
from .urls import parse_import_url

logger = logging.getLogger(__name__)

# A capture that stopped before the video ended must have seen at least this
# share of the player's duration to count as complete.
MIN_COVERAGE = 0.9

_WORKER_STATUSES = {JobStatus.pending, JobStatus.capturing, JobStatus.processing}
_PAUSABLE = _WORKER_STATUSES | {JobStatus.waiting_for_playback}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(text: str) -> str:
    slug = re.sub(r"[^\w]+", "-", text.casefold()).strip("-_")[:60].strip("-_")
    return slug or "import"


def _entry(video: VideoState) -> PlaylistEntry:
    return PlaylistEntry(video.video_id, video.title, video.duration_seconds, video.position)


def _owner_record(user: CurrentUser | None) -> dict[str, str]:
    if user is None:
        return {}
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "scope_kind": user.scope.kind,
        "scope_root": str(user.scope.root),
    }


def _owner_user(record: dict[str, str]) -> CurrentUser | None:
    if not record:
        return None
    return CurrentUser(
        id=record["id"],
        username=record["username"],
        role=record["role"],  # type: ignore[arg-type]
        scope=UserScope(
            kind=record["scope_kind"],  # type: ignore[arg-type]
            user_id=record["id"],
            root=Path(record["scope_root"]),
        ),
    )


@contextmanager
def _owner_context(job: ImportJob) -> Iterator[None]:
    """Run as the job's owner, as knowledge.py does for background KB work."""
    owner = _owner_user(job.owner)
    token = None
    if owner is not None and get_current_user_or_none() != owner:
        token = set_current_user(owner)
    try:
        if job.workspace_id:
            from deeptutor.services.workspace.context import workspace_context
            from deeptutor.services.workspace.knowledge import library_request

            with workspace_context(job.workspace_id):
                library_token = library_request.set(False)
                try:
                    yield
                finally:
                    library_request.reset(library_token)
        else:
            yield
    finally:
        if token is not None:
            reset_current_user(token)


def _current_workspace_id() -> str:
    from deeptutor.services.workspace.context import current_workspace_id

    return current_workspace_id()


class _TrackedSink(CaptureSink):
    """A capture sink that remembers the latest frame time it was offered.

    Frames arrive every second whether or not they are kept, so the latest
    offered timestamp measures how far playback actually got, even through a
    long static slide where the selector keeps nothing new.
    """

    def __init__(self, out_dir: Path) -> None:
        super().__init__(out_dir)
        self.max_offered_t = 0.0

    def offer_frame(self, t_seconds: float, jpeg: bytes) -> bool:
        self.max_offered_t = max(self.max_offered_t, float(t_seconds))
        return super().offer_frame(t_seconds, jpeg)

    def reset(self) -> None:
        super().reset()
        self.max_offered_t = 0.0


def _write_meta(out_dir: Path, result: CaptureResult) -> None:
    meta = {
        "ad_seconds": result.ad_seconds,
        "frames": [{"t": f.t_seconds, "file": f.path.name} for f in result.frames],
        "chapters": [asdict(c) for c in result.chapters],
        "description": result.description,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


def _clear_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


def _save_segments(video_dir: Path, segments: list[TranscriptSegment]) -> None:
    rows = [[s.start_seconds, s.end_seconds, s.text] for s in segments]
    (video_dir / "segments.json").write_text(json.dumps(rows), encoding="utf-8")
    (video_dir / "audio.webm").unlink(missing_ok=True)


def _load_described_inputs(
    video_dir: Path,
) -> tuple[dict, list[TranscriptSegment], list[CapturedFrame]]:
    meta = json.loads((video_dir / "meta.json").read_text(encoding="utf-8"))
    rows = json.loads((video_dir / "segments.json").read_text(encoding="utf-8"))
    segments = [TranscriptSegment(start, end, text) for start, end, text in rows]
    frames = [
        CapturedFrame(float(f["t"]), video_dir / "frames" / f["file"]) for f in meta["frames"]
    ]
    return meta, segments, frames


def _write_notes(path: Path, markdown: str, video_dir: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    shutil.rmtree(video_dir / "frames", ignore_errors=True)
    (video_dir / "segments.json").unlink(missing_ok=True)
    (video_dir / "meta.json").unlink(missing_ok=True)
    try:
        video_dir.rmdir()
    except OSError:
        pass


StoreResolver = Callable[[], ImportJobStore]

_scope_stores: dict[Path, ImportJobStore] = {}


def current_scope_store() -> ImportJobStore:
    """The job store of the current user's current workspace (cached per root)."""
    root = get_current_path_service().get_workspace_feature_dir("video_import").resolve()
    store = _scope_stores.get(root)
    if store is None:
        store = _scope_stores[root] = ImportJobStore(root)
    return store


def _purge_work(store: ImportJobStore, job_id: str) -> None:
    shutil.rmtree(store.root / "work" / job_id, ignore_errors=True)


class _ActiveIndex:
    """``{job_id: store root}`` of jobs that may need recovery after a restart.

    Job data stays in each owner's own workspace store; this small global file
    only tells ``recover()`` where to look, since a restart has no user context.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._entries: dict[str, str] | None = None

    def _load(self) -> dict[str, str]:
        if self._entries is None:
            try:
                self._entries = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                self._entries = {}
        return self._entries

    def entries(self) -> dict[str, str]:
        with self._lock:
            return dict(self._load())

    def update(self, job_id: str, root: Path, active: bool) -> None:
        with self._lock:
            entries = self._load()
            if active == (job_id in entries):
                return
            if active:
                entries[job_id] = str(root)
            else:
                del entries[job_id]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.path, entries)


class ImportRunner:
    """One runner and one worker for the whole process (one job at a time on
    the GPU), with each job's data kept in its owner's workspace store.

    ``store`` is either a fixed store or a resolver returning the store of the
    calling user's scope (the default). Every public method resolves the
    caller's store, so a job id from another user or workspace raises
    ``KeyError``.
    """

    def __init__(
        self,
        store: ImportJobStore | StoreResolver = current_scope_store,
        *,
        file_capture: FileCapture,
        details=fetch_video_details,
        transcribe=transcribe_capture,
        describe=describe_frames,
        add_to_kb=add_notes_to_kb,
        switcher: ModelSwitcher,
        lister=list_target,
        folder_lister=list_media_folder,
        index_path: Path | None = None,
    ) -> None:
        if isinstance(store, ImportJobStore):
            fixed = store
            self._resolve_store: StoreResolver = lambda: fixed
            index_path = index_path or fixed.root / "active_jobs.json"
        else:
            self._resolve_store = store
            index_path = index_path or SYSTEM_ROOT / "video_import" / "active_jobs.json"
        self._index = _ActiveIndex(index_path)
        self._file_capture = file_capture
        self._details = details
        self._transcribe = transcribe
        self._describe = describe
        self._add_to_kb = add_to_kb
        self._switcher = switcher
        self._lister = lister
        self._folder_lister = folder_lister
        self._sinks: dict[tuple[str, str, str], _TrackedSink] = {}
        self._queue: asyncio.Queue[tuple[ImportJobStore, str]] = asyncio.Queue()
        self._queued: set[str] = set()
        self._worker: asyncio.Task[None] | None = None
        self._running: str | None = None
        self._pause_requested: set[str] = set()
        self._cancel_requested: set[str] = set()

    # ── creation ──────────────────────────────────────────────────────────

    async def preview(
        self, *, url: str | None = None, folder: str | None = None
    ) -> tuple[ImportTarget, Listing]:
        if (url is None) == (folder is None):
            raise ValueError("Give exactly one of url or folder")
        if url is not None:
            target = parse_import_url(url)
            return target, await self._lister(target)
        resolved = Path(folder).expanduser().resolve()
        listing = await self._folder_lister(resolved)
        return ImportTarget(TargetKind.FOLDER, None, None, str(resolved)), listing

    async def create(
        self,
        *,
        url: str | None = None,
        folder: str | None = None,
        video_ids: list[str] | None = None,
        kb_name: str | None = None,
    ) -> ImportJob:
        store = self._resolve_store()
        target, listing = await self.preview(url=url, folder=folder)
        entries = listing.entries
        if video_ids is not None:
            wanted = set(video_ids)
            entries = [e for e in entries if e.video_id in wanted]
        if not entries:
            raise ValueError("Nothing to import")
        is_file = target.kind is TargetKind.FOLDER

        jobs = await asyncio.to_thread(store.list) if target.playlist_id else []
        if not kb_name:
            previous = next(
                (j for j in jobs if j.target.playlist_id == target.playlist_id), None
            )
            if previous is not None:
                kb_name = previous.kb_name
            elif is_file:
                kb_name = f"files-{_slug(Path(target.folder).name)}"
            else:
                kb_name = f"yt-{_slug(listing.title)}"
        done = (
            await asyncio.to_thread(store.done_video_ids, target.playlist_id)
            if target.playlist_id
            else set()
        )

        videos = []
        for entry in entries:
            video = VideoState(entry.video_id, entry.title, entry.position, entry.duration_seconds)
            if entry.video_id in done:
                video.stage = VideoStage.skipped
                video.error_code = "already_imported"
            videos.append(video)
        now = _now()
        job = ImportJob(
            job_id=uuid.uuid4().hex,
            source_url=url or target.folder or "",
            target=target,
            title=listing.title,
            kb_name=kb_name,
            status=JobStatus.pending if is_file else JobStatus.waiting_for_playback,
            capture_mode=CaptureMode.file if is_file else CaptureMode.browser,
            videos=videos,
            created_at=now,
            updated_at=now,
            owner=_owner_record(get_current_user_or_none()),
            workspace_id=_current_workspace_id(),
        )
        if not is_file and not self._has_capture_left(job):
            job.status = JobStatus.pending
        await self._save(store, job)
        if job.status is JobStatus.pending:
            self._enqueue(store, job.job_id)
        return job

    def list_jobs(self) -> list[ImportJob]:
        return self._resolve_store().list()

    def get_job(self, job_id: str) -> ImportJob:
        return self._resolve_store().load(job_id)

    # ── browser capture sessions ──────────────────────────────────────────

    def begin_capture(self, job_id: str, video_id: str) -> CaptureSink:
        store = self._resolve_store()
        job = store.load(job_id)
        if job.status not in (JobStatus.waiting_for_playback, JobStatus.capturing):
            raise ValueError(f"Job is {job.status.value}, not waiting for playback")
        video = self._video(job, video_id)
        if video.stage not in (VideoStage.queued, VideoStage.capturing):
            raise ValueError(f"Video is {video.stage.value}, not queued")
        key = (str(store.root), job_id, video_id)
        sink = self._sinks.get(key) or _TrackedSink(store.video_dir(job_id, video_id))
        sink.reset()
        self._sinks[key] = sink
        video.stage = VideoStage.capturing
        video.error_code = video.error_detail = ""
        job.status = JobStatus.capturing
        job.updated_at = _now()
        store.save(job)
        self._index.update(job_id, store.root, True)
        return sink

    def sink(self, job_id: str, video_id: str) -> CaptureSink:
        return self._sinks[(str(self._resolve_store().root), job_id, video_id)]

    async def finish_capture(
        self,
        job_id: str,
        video_id: str,
        *,
        ended: bool,
        ad_seconds: float,
        player_duration: float,
        error_code: str | None = None,
    ) -> ImportJob:
        store = self._resolve_store()
        key = (str(store.root), job_id, video_id)
        sink = self._sinks[key]
        job = await asyncio.to_thread(store.load, job_id)
        video = self._video(job, video_id)
        incomplete = (
            not ended
            and player_duration > 0
            and sink.max_offered_t < MIN_COVERAGE * player_duration
        )
        if error_code or incomplete:
            await asyncio.to_thread(sink.reset)
            if error_code:
                video.stage = VideoStage.skipped
                video.error_code = error_code
            else:
                video.stage = VideoStage.queued
                video.error_code = "incomplete_capture"
                video.error_detail = (
                    f"Captured {sink.max_offered_t:.0f}s of {player_duration:.0f}s"
                )
        else:
            chapters, description = await self._details(video_id)
            result = sink.finish(
                ad_seconds=ad_seconds, chapters=chapters, description=description
            )
            await asyncio.to_thread(_write_meta, sink.out_dir, result)
            video.stage = VideoStage.captured
        del self._sinks[key]
        if self._has_capture_left(job):
            job.status = JobStatus.waiting_for_playback
            await self._save(store, job)
        else:
            job.status = JobStatus.pending
            await self._save(store, job)
            self._enqueue(store, job_id)
        return job

    # ── control ───────────────────────────────────────────────────────────

    def start(self, job_id: str) -> None:
        store = self._resolve_store()
        store.load(job_id)  # KeyError for a job outside the caller's scope
        self._enqueue(store, job_id)

    def _enqueue(self, store: ImportJobStore, job_id: str) -> None:
        if job_id in self._queued:
            return
        self._queued.add(job_id)
        self._queue.put_nowait((store, job_id))
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(self._work())

    async def drain(self) -> None:
        """Wait until every queued job has been processed (or has stopped)."""
        await self._queue.join()

    async def pause(self, job_id: str) -> ImportJob:
        """Pause a job; a running job stops at its next video or phase boundary."""
        store = self._resolve_store()
        job = await asyncio.to_thread(store.load, job_id)
        if job.status not in _PAUSABLE:
            raise ValueError(f"Job is {job.status.value} and cannot be paused")
        if job_id == self._running:
            self._pause_requested.add(job_id)
            return job
        await self._drop_sinks(store, job)
        job.status = JobStatus.paused
        await self._save(store, job)
        return job

    async def resume(self, job_id: str) -> ImportJob:
        store = self._resolve_store()
        job = await asyncio.to_thread(store.load, job_id)
        if job_id == self._running and job_id in self._pause_requested:
            self._pause_requested.discard(job_id)
            return job
        if job.status not in (JobStatus.paused, JobStatus.failed):
            raise ValueError(f"Job is {job.status.value} and cannot be resumed")
        job.error_code = job.error_detail = ""
        if job.capture_mode is CaptureMode.browser and self._has_capture_left(job):
            job.status = JobStatus.waiting_for_playback
            await self._save(store, job)
        else:
            job.status = JobStatus.pending
            await self._save(store, job)
            self._enqueue(store, job_id)
        return job

    async def cancel(self, job_id: str) -> ImportJob:
        store = self._resolve_store()
        job = await asyncio.to_thread(store.load, job_id)
        if job.status in (JobStatus.done, JobStatus.cancelled):
            return job
        await self._drop_sinks(store, job)
        if job_id == self._running:
            self._cancel_requested.add(job_id)
            return job
        job.status = JobStatus.cancelled
        await self._save(store, job)
        return job

    async def recover(self) -> None:
        """Requeue work interrupted by a restart, across every user's scope.

        Capture sessions start over: in-memory sinks do not survive a restart.
        """
        for job_id, root in (await asyncio.to_thread(self._index.entries)).items():
            store = await asyncio.to_thread(ImportJobStore, Path(root))
            try:
                job = await asyncio.to_thread(store.load, job_id)
            except KeyError:
                await asyncio.to_thread(self._index.update, job_id, store.root, False)
                continue
            changed = False
            for video in job.videos:
                if video.stage is VideoStage.capturing:
                    video.stage = VideoStage.queued
                    changed = True
            browser = job.capture_mode is CaptureMode.browser
            if browser and job.status in (JobStatus.waiting_for_playback, JobStatus.capturing):
                status = (
                    JobStatus.waiting_for_playback
                    if self._has_capture_left(job)
                    else JobStatus.pending
                )
                changed = changed or status is not job.status
                job.status = status
            if changed:
                await self._save(store, job)
            if job.status in _WORKER_STATUSES:
                self._enqueue(store, job.job_id)

    # ── worker ────────────────────────────────────────────────────────────

    async def _work(self) -> None:
        while True:
            store, job_id = await self._queue.get()
            self._queued.discard(job_id)
            self._running = job_id
            try:
                job = await asyncio.to_thread(store.load, job_id)
                if job.status in _WORKER_STATUSES:
                    with _owner_context(job):
                        await self._process(store, job)
            except Exception:
                logger.exception("Video import job %s crashed", job_id)
            finally:
                self._running = None
                self._pause_requested.discard(job_id)
                self._cancel_requested.discard(job_id)
                self._queue.task_done()

    async def _process(self, store: ImportJobStore, job: ImportJob) -> None:
        restored = False
        try:
            if job.capture_mode is CaptureMode.file and self._at(job, VideoStage.queued):
                job.status = JobStatus.capturing
                await self._save(store, job)
                for video in self._at(job, VideoStage.queued):
                    if await self._halted(store, job):
                        return
                    await self._capture_file(store, job, video)
            if await self._halted(store, job):
                return
            job.status = JobStatus.processing
            await self._save(store, job)

            if self._at(job, VideoStage.captured):
                missing = _probe_stt_configuration()
                if missing:
                    job.status = JobStatus.failed
                    job.error_code = "stt_not_configured"
                    job.error_detail = missing
                    await self._save(store, job)
                    return
                await self._switcher.free_for_stt()
                for video in self._at(job, VideoStage.captured):
                    if await self._halted(store, job):
                        return
                    await self._transcribe_video(store, job, video)
            if await self._halted(store, job):
                return

            if self._at(job, VideoStage.transcribed):
                await self._switcher.free_for_vision()
                for video in self._at(job, VideoStage.transcribed):
                    if await self._halted(store, job):
                        return
                    await self._describe_video(store, job, video)
            if await self._halted(store, job):
                return

            described = self._at(job, VideoStage.described)
            if described:
                # Indexing needs the embedding model, which restore_chat reloads.
                await self._switcher.restore_chat()
                restored = True
                notes = [store.notes_path(job.job_id, _entry(v)) for v in described]
                await self._add_to_kb(job.kb_name, notes)
                for video in described:
                    video.stage = VideoStage.done
                await self._save(store, job)

            stages = [v.stage for v in job.videos]
            if VideoStage.done not in stages and VideoStage.failed in stages:
                job.status = JobStatus.failed
                job.error_code = "all_videos_failed"
            else:
                job.status = JobStatus.done
            await self._save(store, job)
        except Exception as exc:
            logger.exception("Video import job %s failed", job.job_id)
            job.status = JobStatus.failed
            job.error_code = "processing_failed"
            job.error_detail = str(exc)
            await self._save(store, job)
        finally:
            if not restored:
                try:
                    await self._switcher.restore_chat()
                except Exception:
                    logger.warning("Could not restore the chat models", exc_info=True)

    async def _halted(self, store: ImportJobStore, job: ImportJob) -> bool:
        if job.job_id in self._cancel_requested:
            job.status = JobStatus.cancelled
        elif job.job_id in self._pause_requested:
            job.status = JobStatus.paused
        else:
            return False
        await self._save(store, job)
        return True

    async def _capture_file(
        self, store: ImportJobStore, job: ImportJob, video: VideoState
    ) -> None:
        video_dir = await asyncio.to_thread(store.video_dir, job.job_id, video.video_id)
        # A stale partial capture would otherwise be appended to.
        await asyncio.to_thread(_clear_dir, video_dir)
        try:
            source = await asyncio.to_thread(media_path, Path(job.target.folder), video.video_id)
        except (KeyError, OSError) as exc:
            self._fail(video, "file_missing", f"Media file not found in the folder: {exc}")
        else:
            try:
                result = await self._file_capture.capture(
                    _entry(video), video_dir, source=source
                )
                await asyncio.to_thread(_write_meta, video_dir, result)
            except FileNotFoundError as exc:
                self._fail(
                    video, "ffmpeg_missing", f"ffmpeg/ffprobe is not installed on the server: {exc}"
                )
            except Exception as exc:
                self._fail(video, "capture_failed", str(exc))
            else:
                video.stage = VideoStage.captured
        await self._save(store, job)

    async def _transcribe_video(
        self, store: ImportJobStore, job: ImportJob, video: VideoState
    ) -> None:
        video_dir = await asyncio.to_thread(store.video_dir, job.job_id, video.video_id)
        audio = video_dir / "audio.webm"
        try:
            has_audio = await asyncio.to_thread(lambda: audio.exists() and audio.stat().st_size > 0)
            segments = list(await self._transcribe(audio)) if has_audio else []
        except Exception as exc:
            logger.warning("Transcribing %s failed", video.video_id, exc_info=True)
            self._fail(video, "transcribe_failed", str(exc))
        else:
            await asyncio.to_thread(_save_segments, video_dir, segments)
            video.stage = VideoStage.transcribed
            video.speech_cues = len(segments)
        await self._save(store, job)

    async def _describe_video(
        self, store: ImportJobStore, job: ImportJob, video: VideoState
    ) -> None:
        video_dir = await asyncio.to_thread(store.video_dir, job.job_id, video.video_id)
        try:
            meta, segments, frames = await asyncio.to_thread(_load_described_inputs, video_dir)
            frame_notes = await self._describe(frames)
            entry = _entry(video)
            markdown = build_notes_markdown(
                entry,
                job.title,
                segments,
                frame_notes,
                chapters=[Chapter(c["t_seconds"], c["title"]) for c in meta["chapters"]],
                description=meta["description"],
            )
            path = store.notes_path(job.job_id, entry)
            await asyncio.to_thread(_write_notes, path, markdown, video_dir)
        except Exception as exc:
            logger.warning("Describing %s failed", video.video_id, exc_info=True)
            self._fail(video, "describe_failed", str(exc))
        else:
            video.stage = VideoStage.described
            video.frame_notes = len(frame_notes)
        await self._save(store, job)

    # ── helpers ───────────────────────────────────────────────────────────

    async def _save(self, store: ImportJobStore, job: ImportJob) -> None:
        job.updated_at = _now()
        await asyncio.to_thread(store.save, job)
        await asyncio.to_thread(
            self._index.update, job.job_id, store.root, job.status in _PAUSABLE
        )
        if job.status in (JobStatus.done, JobStatus.cancelled):
            # Nothing can retry a finished or cancelled job: only notes persist.
            await asyncio.to_thread(_purge_work, store, job.job_id)

    async def _drop_sinks(self, store: ImportJobStore, job: ImportJob) -> None:
        for video in job.videos:
            sink = self._sinks.pop((str(store.root), job.job_id, video.video_id), None)
            if sink is not None:
                await asyncio.to_thread(sink.reset)
            if video.stage is VideoStage.capturing:
                video.stage = VideoStage.queued

    @staticmethod
    def _video(job: ImportJob, video_id: str) -> VideoState:
        for video in job.videos:
            if video.video_id == video_id:
                return video
        raise KeyError(video_id)

    @staticmethod
    def _at(job: ImportJob, stage: VideoStage) -> list[VideoState]:
        return [v for v in job.videos if v.stage is stage]

    @staticmethod
    def _has_capture_left(job: ImportJob) -> bool:
        return any(v.stage in (VideoStage.queued, VideoStage.capturing) for v in job.videos)

    @staticmethod
    def _fail(video: VideoState, code: str, detail: str) -> None:
        video.stage = VideoStage.failed
        video.error_code = code
        video.error_detail = detail


_runner: ImportRunner | None = None


def get_import_runner() -> ImportRunner:
    """The process-wide runner: one worker, stores resolved per caller scope."""
    global _runner
    if _runner is None:
        _runner = ImportRunner(file_capture=FileCapture(), switcher=ModelSwitcher())
    return _runner
