"""URL parsing for YouTube video / playlist imports."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from deeptutor.reading.ingestion import normalize_url, youtube_video_id

from .models import ImportTarget, TargetKind

_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
_ERROR = "not a YouTube video or playlist URL"


def parse_import_url(url: str) -> ImportTarget:
    """Classify a URL as a YouTube playlist or single video.

    A ``list=`` parameter wins over ``v=`` unless it is an auto-generated
    ``RD`` mix, which is endless and user-specific.
    """
    parsed = urlparse((url or "").strip())
    if (parsed.hostname or "").lower().rstrip(".") not in _HOSTS:
        raise ValueError(_ERROR)
    playlist_id = (parse_qs(parsed.query).get("list") or [""])[0]
    if playlist_id and not playlist_id.startswith("RD"):
        return ImportTarget(TargetKind.PLAYLIST, playlist_id, None)
    try:
        video_id = youtube_video_id(normalize_url(url))
    except Exception as exc:
        raise ValueError(_ERROR) from exc
    if not video_id:
        raise ValueError(_ERROR)
    return ImportTarget(TargetKind.VIDEO, None, video_id)
