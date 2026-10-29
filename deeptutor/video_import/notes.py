"""Per-video private study notes: chapters, description, and a speech/screen timeline."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import re

from deeptutor.reading.ingestion import TranscriptSegment
from deeptutor.video_import.models import PlaylistEntry

_DESCRIPTION_LIMIT = 4000
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_MIN_WORDS_AFTER_LINK = 4


@dataclass(frozen=True)
class FrameNote:
    t_seconds: float
    text: str


@dataclass(frozen=True)
class Chapter:
    t_seconds: float
    title: str


def _clock(seconds: float) -> str:
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _clean_description(text: str) -> str:
    """Keep the creator's explanation; drop link lists, hashtags and the legal tail."""
    kept: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.lower().startswith("disclaimer"):
            break
        if _URL.search(line):
            line = " ".join(_URL.sub("", line).split())
            if len(line.split()) < _MIN_WORDS_AFTER_LINK:
                continue
        if line and all(word.startswith("#") for word in line.split()):
            continue
        if line or (kept and kept[-1]):
            kept.append(line)
    return "\n".join(kept)


def build_notes_markdown(
    entry: PlaylistEntry,
    playlist_title: str,
    segments: Sequence[TranscriptSegment],
    frames: Sequence[FrameNote],
    *,
    chapters: Sequence[Chapter] = (),
    description: str = "",
) -> str:
    duration = f"Duration {_clock(entry.duration_seconds)}"
    if playlist_title != entry.title:
        duration = f"Playlist: {playlist_title} · Video {entry.position} · {duration}"
    lines = [f"# {_one_line(entry.title)}", "", duration, ""]
    if chapters:
        lines += ["## Chapters"]
        lines += [f"[{_clock(c.t_seconds)}] {_one_line(c.title)}" for c in chapters]
        lines.append("")
    description = _clean_description(description)[:_DESCRIPTION_LIMIT].strip()
    if description:
        lines += ["## Description", description, ""]
    lines.append("## Timeline")
    if not segments:
        lines.append("No speech was detected in this video.")
    events = [(s.start_seconds, 0, "speech", _one_line(s.text)) for s in segments]
    events += [(f.t_seconds, 1, "screen", _one_line(f.text)) for f in frames]
    events = [e for e in events if e[3]]
    events.sort(key=lambda e: (e[0], e[1]))
    lines += [f"[{_clock(t)}] {kind}: {text}" for t, _, kind, text in events]
    return "\n".join(lines) + "\n"


def notes_filename(entry: PlaylistEntry) -> str:
    return f"{entry.position:03d}-{entry.video_id}.md"
