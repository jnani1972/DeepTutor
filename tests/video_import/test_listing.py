import pytest
from yt_dlp.utils import DownloadError

from deeptutor.video_import.listing import (
    ListingError,
    default_ydl_options,
    details_ydl_options,
    fetch_video_details,
    list_target,
)
from deeptutor.video_import.models import ImportTarget, PlaylistEntry, TargetKind
from deeptutor.video_import.notes import Chapter


@pytest.mark.asyncio
async def test_lists_playlist_entries_in_order(fixture_json):
    listing = await list_target(
        ImportTarget(TargetKind.PLAYLIST, "PLtest", None),
        extract=fixture_json("flat_playlist.json"),
    )
    assert listing.title == "Test Playlist"
    assert [(e.position, e.video_id, e.duration_seconds) for e in listing.entries] == [
        (1, "aaaaaaaaaaa", 754),
        (2, "bbbbbbbbbbb", 3600),
    ]


@pytest.mark.asyncio
async def test_single_video_listing(fixture_json):
    listing = await list_target(
        ImportTarget(TargetKind.VIDEO, None, "aaaaaaaaaaa"),
        extract=fixture_json("flat_video.json"),
    )
    assert listing.entries == [PlaylistEntry("aaaaaaaaaaa", "Test Video", 754, 1)]


@pytest.mark.asyncio
async def test_private_or_deleted_entries_are_left_out(fixture_json):
    listing = await list_target(
        ImportTarget(TargetKind.PLAYLIST, "PLgaps", None),
        extract=fixture_json("flat_playlist_with_gaps.json"),
    )
    assert [(e.position, e.video_id, e.duration_seconds) for e in listing.entries] == [
        (1, "aaaaaaaaaaa", 10),
        (2, "bbbbbbbbbbb", 0),
    ]


@pytest.mark.asyncio
async def test_extractor_error_maps_to_listing_error():
    async def boom(_url):
        raise DownloadError("ERROR: The playlist does not exist.")

    with pytest.raises(ListingError) as exc:
        await list_target(ImportTarget(TargetKind.PLAYLIST, "PLx", None), extract=boom)
    assert exc.value.code == "playlist_unavailable"


@pytest.mark.asyncio
async def test_video_extractor_error_and_parse_failure():
    async def boom(_url):
        raise DownloadError("ERROR: Video unavailable")

    with pytest.raises(ListingError) as exc:
        await list_target(ImportTarget(TargetKind.VIDEO, None, "x"), extract=boom)
    assert exc.value.code == "video_unavailable"

    async def no_entries(_url):
        return {"title": "t"}

    with pytest.raises(ListingError) as exc:
        await list_target(ImportTarget(TargetKind.PLAYLIST, "PLx", None), extract=no_entries)
    assert exc.value.code == "listing_parse_failed"


def test_default_extractor_never_fetches_media():
    opts = default_ydl_options()
    assert opts["extract_flat"] in ("in_playlist", True) and opts["skip_download"] is True
    assert "format" not in opts and "outtmpl" not in opts


@pytest.mark.asyncio
async def test_fetch_video_details_maps_chapters_and_description():
    async def extract(_url):
        return {
            "description": "About enzymes.",
            "chapters": [
                {"start_time": 0.0, "title": "Intro"},
                {"start_time": 200.0, "title": "Energy"},
            ],
        }

    assert await fetch_video_details("aaaaaaaaaaa", extract=extract) == (
        [Chapter(0.0, "Intro"), Chapter(200.0, "Energy")],
        "About enzymes.",
    )


@pytest.mark.asyncio
async def test_fetch_video_details_failure_is_empty():
    async def boom(_url):
        raise RuntimeError("x")

    assert await fetch_video_details("aaaaaaaaaaa", extract=boom) == ([], "")


def test_details_options_never_fetch_media():
    opts = details_ydl_options()
    assert opts["skip_download"] is True
    assert "format" not in opts and "outtmpl" not in opts
