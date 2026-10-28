"""Capture local media files the owner already has, via ffmpeg.

Feeds the same ``CaptureSink`` as the browser path: audio as ``audio.webm``,
1 fps frames, plus chapters and description read from container metadata.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import re
import tempfile

from deeptutor.services.workspace.service import WorkspaceError, get_content_workspace_service

from .capture import CaptureResult, CaptureSink
from .listing import Listing, ListingError
from .models import PlaylistEntry
from .notes import Chapter

MEDIA_EXTENSIONS = {
    ".mp4", ".mkv", ".webm", ".mov", ".m4v", ".mp3", ".m4a", ".wav", ".ogg", ".opus",
}  # fmt: skip

_AUDIO_CHUNK_BYTES = 1024 * 1024


def file_video_id(folder: Path, path: Path) -> str:
    relative = Path(path).relative_to(folder).as_posix()
    return "file-" + hashlib.sha1(relative.encode("utf-8")).hexdigest()[:12]


def _natural_key(name: str) -> list[tuple[int, int, str]]:
    return [
        (0, int(part), "") if part.isdigit() else (1, 0, part.casefold())
        for part in re.split(r"(\d+)", name)
        if part
    ]


def _media_files(folder: Path) -> list[Path]:
    files = [
        p
        for p in folder.iterdir()
        if p.suffix.lower() in MEDIA_EXTENSIONS and p.is_file() and not p.is_symlink()
    ]
    return sorted(files, key=lambda p: _natural_key(p.name))


async def _run(*argv: str) -> bytes:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        tail = stderr.decode("utf-8", "replace").strip()[-2000:]
        raise RuntimeError(f"{argv[0]} failed ({process.returncode}): {tail}")
    return stdout


async def _probe(path: Path, *flags: str) -> dict:
    out = await _run(
        "ffprobe", "-v", "error", *flags, "-of", "json", str(path)
    )  # fmt: skip
    return json.loads(out or b"{}")


def _seconds(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


async def list_media_folder(folder: Path) -> Listing:
    folder = Path(folder).expanduser().resolve()
    try:
        # Private check, but it is exactly "is this root inside an allowed root"
        # (validate() would also create an outputs/ dir inside the media folder).
        get_content_workspace_service()._assert_allowed_root(folder)
    except WorkspaceError as exc:
        raise ListingError("folder_not_allowed", str(exc)) from exc
    if not folder.is_dir():
        raise ListingError("folder_empty")
    files = _media_files(folder)
    if not files:
        raise ListingError("folder_empty")
    entries = []
    for position, path in enumerate(files, start=1):
        try:
            info = await _probe(path, "-show_entries", "format=duration")
            duration = int(_seconds((info.get("format") or {}).get("duration")))
        except RuntimeError:
            duration = 0
        entries.append(
            PlaylistEntry(file_video_id(folder, path), path.stem, duration, position)
        )
    return Listing(title=folder.name, entries=entries)


def media_path(folder: Path, video_id: str) -> Path:
    folder = Path(folder)
    for path in _media_files(folder):
        if file_video_id(folder, path) == video_id:
            return path
    raise KeyError(video_id)


class FileCapture:
    async def capture(
        self, entry: PlaylistEntry, out_dir: Path, *, source: Path
    ) -> CaptureResult:
        info = await _probe(
            source, "-show_streams", "-show_format", "-show_chapters"
        )  # fmt: skip
        streams = info.get("streams") or []
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
        has_video = any(
            s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
            for s in streams
        )
        sink = CaptureSink(out_dir)
        with tempfile.TemporaryDirectory(prefix="deeptutor-capture-") as tmp_name:
            tmp = Path(tmp_name)
            if has_audio:
                await self._extract_audio(source, tmp / "audio.webm", sink)
            if has_video:
                await self._extract_frames(source, tmp, sink)
        tags = {k.lower(): v for k, v in ((info.get("format") or {}).get("tags") or {}).items()}
        return sink.finish(
            ad_seconds=0.0,
            chapters=_chapters(info.get("chapters") or []),
            description=str(tags.get("description") or tags.get("comment") or ""),
        )

    @staticmethod
    async def _extract_audio(source: Path, target: Path, sink: CaptureSink) -> None:
        await _run(
            "ffmpeg", "-y", "-v", "error", "-i", str(source),
            "-vn", "-ac", "1", "-c:a", "libopus", str(target),
        )  # fmt: skip
        with target.open("rb") as handle:
            while chunk := handle.read(_AUDIO_CHUNK_BYTES):
                sink.append_audio(chunk)

    @staticmethod
    async def _extract_frames(source: Path, tmp: Path, sink: CaptureSink) -> None:
        await _run(
            "ffmpeg", "-y", "-v", "error", "-i", str(source),
            "-an", "-vf", "fps=1", "-q:v", "3", str(tmp / "%06d.jpg"),
        )  # fmt: skip
        for frame in sorted(tmp.glob("*.jpg")):
            sink.offer_frame(int(frame.stem) - 1, frame.read_bytes())
            frame.unlink()


def _chapters(rows: list[dict]) -> list[Chapter]:
    return [
        Chapter(_seconds(row.get("start_time")), str((row.get("tags") or {}).get("title") or ""))
        for row in rows
    ]
