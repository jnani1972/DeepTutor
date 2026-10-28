import pytest
from yt_dlp.utils import DownloadError

from deeptutor.video_import.listing import ListingError, default_ydl_options, list_target
from deeptutor.video_import.models import ImportTarget, PlaylistEntry, TargetKind


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
