import pytest

from deeptutor.video_import.models import (
    CaptureMode,
    ImportJob,
    ImportTarget,
    JobStatus,
    TargetKind,
    VideoState,
)
from deeptutor.video_import.urls import parse_import_url


@pytest.mark.parametrize(
    "url,kind,playlist_id,video_id",
    [
        ("https://www.youtube.com/playlist?list=PLabc123", TargetKind.PLAYLIST, "PLabc123", None),
        (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLabc123&index=3",
            TargetKind.PLAYLIST,
            "PLabc123",
            None,
        ),
        ("https://youtu.be/dQw4w9WgXcQ", TargetKind.VIDEO, None, "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", TargetKind.VIDEO, None, "dQw4w9WgXcQ"),
    ],
)
def test_parse_import_url(url, kind, playlist_id, video_id):
    assert parse_import_url(url) == ImportTarget(kind, playlist_id, video_id)


def test_mix_lists_are_treated_as_the_single_video():
    # "RD…" auto-generated mixes are endless and user-specific; import the video only.
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ"
    assert parse_import_url(url).kind is TargetKind.VIDEO


def test_rejects_non_youtube():
    with pytest.raises(ValueError):
        parse_import_url("https://example.com/watch?v=abc")


def test_job_round_trips_through_dict():
    job = ImportJob(
        job_id="j1",
        source_url="u",
        target=ImportTarget(TargetKind.VIDEO, None, "dQw4w9WgXcQ"),
        title="t",
        kb_name="kb",
        status=JobStatus.pending,
        capture_mode=CaptureMode.browser,
        videos=[VideoState("dQw4w9WgXcQ", "t", 1, 212)],
        created_at="2026-10-29T00:00:00Z",
        updated_at="2026-10-29T00:00:00Z",
    )
    assert ImportJob.from_dict(job.to_dict()) == job


def test_job_with_folder_target_round_trips():
    job = ImportJob(
        job_id="j2",
        source_url="D:/lectures",
        target=ImportTarget(TargetKind.FOLDER, None, None, folder="D:/lectures"),
        title="t",
        kb_name="kb",
        status=JobStatus.waiting_for_playback,
        capture_mode=CaptureMode.file,
        videos=[],
        created_at="2026-10-29T00:00:00Z",
        updated_at="2026-10-29T00:00:00Z",
    )
    restored = ImportJob.from_dict(job.to_dict())
    assert restored == job and restored.target.folder == "D:/lectures"
