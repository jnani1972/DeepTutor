import pytest

from deeptutor.video_import.models import (
    CaptureMode,
    ImportJob,
    ImportTarget,
    JobStatus,
    PlaylistEntry,
    TargetKind,
    VideoStage,
    VideoState,
)
from deeptutor.video_import.store import ImportJobStore


def _job(job_id, created, playlist="PLx", stages=(("a", VideoStage.done),)):
    return ImportJob(
        job_id=job_id,
        source_url="u",
        target=ImportTarget(TargetKind.PLAYLIST, playlist, None),
        title="t",
        kb_name="kb",
        status=JobStatus.pending,
        capture_mode=CaptureMode.browser,
        videos=[VideoState(v, v, i + 1, 10, stage) for i, (v, stage) in enumerate(stages)],
        created_at=created,
        updated_at=created,
    )


def test_round_trip_and_missing(tmp_path):
    store = ImportJobStore(tmp_path)
    job = _job("j1", "2026-01-01")
    store.save(job)
    assert store.load("j1") == job
    with pytest.raises(KeyError):
        store.load("nope")


def test_list_newest_first(tmp_path):
    store = ImportJobStore(tmp_path)
    store.save(_job("old", "2026-01-01"))
    store.save(_job("new", "2026-02-01"))
    assert [j.job_id for j in store.list()] == ["new", "old"]


def test_done_video_ids_filters_playlist_and_stage(tmp_path):
    store = ImportJobStore(tmp_path)
    store.save(_job("j1", "2026-01-01", stages=(("a", VideoStage.done), ("b", VideoStage.failed))))
    store.save(_job("j2", "2026-01-02", playlist="PLother", stages=(("z", VideoStage.done),)))
    assert store.done_video_ids("PLx") == {"a"}


def test_paths(tmp_path):
    store = ImportJobStore(tmp_path)
    vdir = store.video_dir("j1", "a")
    assert vdir.is_dir() and vdir == store.root / "work" / "j1" / "a"
    entry = PlaylistEntry("a", "T", 10, 3)
    assert store.notes_path("j1", entry) == store.root / "notes" / "j1" / "003-a.md"
