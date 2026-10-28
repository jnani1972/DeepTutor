import os
from pathlib import Path
import shutil
import subprocess

import pytest

from deeptutor.multi_user.context import reset_current_user, set_current_user
from deeptutor.multi_user.models import CurrentUser, UserScope
from deeptutor.services.workspace import service as service_module
from deeptutor.video_import.files import (
    FileCapture,
    file_video_id,
    list_media_folder,
    media_path,
)
from deeptutor.video_import.listing import ListingError
from deeptutor.video_import.models import PlaylistEntry
from deeptutor.video_import.notes import Chapter

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

TWO_COLOURS = (
    "color=c=red:s=128x72:d=3:r=10[a];color=c=blue:s=128x72:d=3:r=10[b];"
    "[a][b]concat=n=2:v=1:a=0[v]"
)
CHAPTERS = (
    ";FFMETADATA1\ndescription=A short lecture\n"
    "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=3000\ntitle=One\n"
    "[CHAPTER]\nTIMEBASE=1/1000\nSTART=3000\nEND=6000\ntitle=Two\n"
)


def _ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", *args], check=True, capture_output=True
    )


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    root = tmp_path_factory.mktemp("media")
    meta = root / "meta.txt"
    meta.write_text(CHAPTERS, encoding="utf-8")
    sine = ["-f", "lavfi", "-i", "sine=d=6"]
    _ffmpeg(
        *sine, "-filter_complex", TWO_COLOURS, "-i", str(meta), "-map", "[v]", "-map", "0:a",
        "-map_metadata", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        str(root / "clip.mkv"),
    )  # fmt: skip
    _ffmpeg(*sine, "-c:a", "libmp3lame", str(root / "song.mp3"))
    _ffmpeg(
        "-filter_complex", TWO_COLOURS, "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(root / "silent.mp4"),
    )  # fmt: skip
    return root


@pytest.fixture
def allowed(monkeypatch, tmp_path, media):
    from deeptutor.services.path_service import PathService

    paths = PathService(workspace_root=tmp_path / "runtime")
    paths.ensure_all_directories()
    monkeypatch.setattr(service_module, "get_path_service", lambda: paths)
    monkeypatch.delenv("DEEPTUTOR_WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("DEEPTUTOR_WORKSPACE_ALLOWED_ROOTS", os.pathsep.join([str(tmp_path), str(media)]))
    token = set_current_user(CurrentUser(
            id="u1", username="u1", role="user",
            scope=UserScope(kind="user", user_id="u1", root=tmp_path / "u1"),
        ))
    yield
    reset_current_user(token)


def _entry(folder: Path, name: str) -> PlaylistEntry:
    return PlaylistEntry(file_video_id(folder, folder / name), name, 6, 1)


@pytest.mark.asyncio
async def test_lists_media_in_natural_order_with_durations(allowed, tmp_path):
    folder = tmp_path / "lectures"
    folder.mkdir()
    for name in ("10 x.mp3", "2 x.mp3", "notes.txt"):
        (folder / name).write_bytes(b"not really audio")
    listing = await list_media_folder(folder)
    assert [e.title for e in listing.entries] == ["2 x", "10 x"]
    assert [e.position for e in listing.entries] == [1, 2]
    assert all(e.duration_seconds == 0 for e in listing.entries)
    assert media_path(folder, listing.entries[0].video_id) == folder / "2 x.mp3"
    with pytest.raises(KeyError):
        media_path(folder, "file-000000000000")


@pytest.mark.asyncio
async def test_listing_reports_probed_duration(allowed, media):
    listing = await list_media_folder(media)
    by_title = {e.title: e for e in listing.entries}
    assert by_title["clip"].duration_seconds == 6


@pytest.mark.asyncio
async def test_folder_outside_allowed_roots_is_rejected(allowed, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "a.mp3").write_bytes(b"x")
    with pytest.raises(ListingError) as caught:
        await list_media_folder(outside)
    assert caught.value.code == "folder_not_allowed"


@pytest.mark.asyncio
async def test_empty_folder_is_an_error(allowed, tmp_path):
    folder = tmp_path / "empty"
    folder.mkdir()
    with pytest.raises(ListingError) as caught:
        await list_media_folder(folder)
    assert caught.value.code == "folder_empty"


def test_file_video_id_is_stable(tmp_path):
    a = file_video_id(tmp_path, tmp_path / "sub" / "a.mp4")
    assert a == file_video_id(tmp_path, tmp_path / "sub" / "a.mp4")
    assert a.startswith("file-") and len(a) == len("file-") + 12
    assert a != file_video_id(tmp_path, tmp_path / "sub" / "b.mp4")


@pytest.mark.asyncio
async def test_capture_extracts_audio_frames_and_chapters(media, tmp_path):
    result = await FileCapture().capture(
        _entry(media, "clip.mkv"), tmp_path / "out", source=media / "clip.mkv"
    )
    assert result.audio_path is not None and result.audio_path.stat().st_size > 0
    assert result.audio_path.name == "audio.webm"
    assert len(result.frames) >= 2
    assert result.chapters == [Chapter(0.0, "One"), Chapter(3.0, "Two")]
    assert result.description == "A short lecture"
    assert result.ad_seconds == 0.0
    assert not list((tmp_path / "out").glob("*.jpg"))


@pytest.mark.asyncio
async def test_audio_only_file_has_no_frames(media, tmp_path):
    result = await FileCapture().capture(
        _entry(media, "song.mp3"), tmp_path / "out", source=media / "song.mp3"
    )
    assert result.audio_path is not None
    assert result.frames == []


@pytest.mark.asyncio
async def test_video_without_audio_has_no_audio_path(media, tmp_path):
    result = await FileCapture().capture(
        _entry(media, "silent.mp4"), tmp_path / "out", source=media / "silent.mp4"
    )
    assert result.audio_path is None
    assert len(result.frames) >= 2
