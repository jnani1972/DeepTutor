"""List a playlist's or video's entries via yt-dlp, metadata only.

yt-dlp is used strictly for metadata (``extract_flat`` + ``download=False``);
no media is ever fetched.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from .models import ImportTarget, PlaylistEntry, TargetKind
from .notes import Chapter

MetadataExtractor = Callable[[str], Awaitable[dict]]

_UNLISTED_TITLES = {"[Private video]", "[Deleted video]"}


@dataclass
class Listing:
    title: str
    entries: list[PlaylistEntry]


class ListingError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def default_ydl_options() -> dict:
    """Equivalent of ``yt-dlp --flat-playlist -J``: metadata only."""
    return {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
    }


def details_ydl_options() -> dict:
    """Per-video metadata only: no format selection, no output template."""
    return {"skip_download": True, "quiet": True, "no_warnings": True}


async def _default_extract(url: str) -> dict:
    def run() -> dict:
        with YoutubeDL(default_ydl_options()) as ydl:
            return ydl.extract_info(url, download=False)

    return await asyncio.to_thread(run)


def _target_url(target: ImportTarget) -> str:
    if target.kind is TargetKind.PLAYLIST:
        return f"https://www.youtube.com/playlist?list={target.playlist_id}"
    return f"https://www.youtube.com/watch?v={target.video_id}"


def _parse_playlist(info: dict) -> Listing:
    try:
        raw_entries = info["entries"]
        title = info["title"]
    except KeyError as exc:
        raise ListingError("listing_parse_failed", f"missing {exc}") from exc
    entries: list[PlaylistEntry] = []
    for raw in raw_entries or []:
        if not raw or not raw.get("id") or raw.get("title") in _UNLISTED_TITLES:
            continue
        entries.append(
            PlaylistEntry(
                raw["id"],
                raw.get("title") or raw["id"],
                int(raw.get("duration") or 0),
                len(entries) + 1,
            )
        )
    return Listing(title, entries)


def _parse_video(info: dict) -> Listing:
    try:
        video_id = info["id"]
        title = info["title"]
    except KeyError as exc:
        raise ListingError("listing_parse_failed", f"missing {exc}") from exc
    entry = PlaylistEntry(video_id, title, int(info.get("duration") or 0), 1)
    return Listing(title, [entry])


async def list_target(
    target: ImportTarget, *, extract: MetadataExtractor | None = None
) -> Listing:
    extract = extract or _default_extract
    try:
        info = await extract(_target_url(target))
    except DownloadError as exc:
        code = (
            "playlist_unavailable"
            if target.kind is TargetKind.PLAYLIST
            else "video_unavailable"
        )
        raise ListingError(code, str(exc)) from exc
    if target.kind is TargetKind.PLAYLIST:
        return _parse_playlist(info)
    return _parse_video(info)


async def _details_extract(url: str) -> dict:
    def run() -> dict:
        with YoutubeDL(details_ydl_options()) as ydl:
            return ydl.extract_info(url, download=False)

    return await asyncio.to_thread(run)


async def fetch_video_details(
    video_id: str, *, extract: MetadataExtractor | None = None
) -> tuple[list[Chapter], str]:
    """Chapters and description for one video; optional, so errors give empties."""
    extract = extract or _details_extract
    try:
        info = await extract(f"https://www.youtube.com/watch?v={video_id}")
        chapters = [
            Chapter(float(c["start_time"]), c["title"])
            for c in info.get("chapters") or []
        ]
        return chapters, info.get("description") or ""
    except Exception:
        return [], ""
