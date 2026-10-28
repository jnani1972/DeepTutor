"""Per-video private study notes: chapters, description, and a speech/screen timeline."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from deeptutor.reading.ingestion import TranscriptSegment
from deeptutor.video_import.models import PlaylistEntry

_DESCRIPTION_LIMIT = 4000


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


def build_notes_markdown(
    entry: PlaylistEntry,
    playlist_title: str,
    segments: Sequence[TranscriptSegment],
    frames: Sequence[FrameNote],
    *,
    chapters: Sequence[Chapter] = (),
    description: str = "",
) -> str:
    lines = [
        f"# {entry.title}",
        "",
        f"Playlist: {playlist_title} · Video {entry.position} · "
        f"Duration {_clock(entry.duration_seconds)}",
        "",
    ]
    if chapters:
        lines += ["## Chapters"]
        lines += [f"[{_clock(c.t_seconds)}] {c.title}" for c in chapters]
        lines.append("")
    description = description.strip()[:_DESCRIPTION_LIMIT].strip()
    if description:
        lines += ["## Description", description, ""]
    lines.append("## Timeline")
    if not segments:
        lines.append("No speech was detected in this video.")
    events = [(s.start_seconds, 0, f"speech: {s.text}") for s in segments]
    events += [(f.t_seconds, 1, f"screen: {f.text}") for f in frames]
    events.sort(key=lambda e: (e[0], e[1]))
    lines += [f"[{_clock(t)}] {text}" for t, _, text in events]
    return "\n".join(lines) + "\n"


def notes_filename(entry: PlaylistEntry) -> str:
    return f"{entry.position:03d}-{entry.video_id}.md"
