"""Server-side intake for captured audio and key frames.

Both the browser-session path (audio chunks and 1 fps frames streamed from the
owner's own browser) and the local-file path (ffmpeg) write into a
``CaptureSink``. Nothing here fetches media from YouTube.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
import shutil

from PIL import UnidentifiedImageError

from .frames import FrameSelector, thumbnail_signature
from .notes import Chapter

MAX_AUDIO_CHUNK_BYTES = 5 * 1024 * 1024
MAX_FRAME_BYTES = 2 * 1024 * 1024

YOUTUBE_PLAYER_ERRORS: dict[int, str] = {
    2: "unplayable",
    5: "unplayable",
    100: "removed",
    101: "embed_blocked",
    150: "embed_blocked",
}


class VideoUnavailableError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class CapturedFrame:
    t_seconds: float
    path: Path


@dataclass
class CaptureResult:
    audio_path: Path | None
    frames: list[CapturedFrame]
    ad_seconds: float
    chapters: list[Chapter] = field(default_factory=list)
    description: str = ""


class CaptureSink:
    def __init__(self, out_dir: Path, *, selector: FrameSelector | None = None) -> None:
        self.out_dir = Path(out_dir)
        self._frames_dir = self.out_dir / "frames"
        self._audio_path = self.out_dir / "audio.webm"
        self._frames_dir.mkdir(parents=True, exist_ok=True)
        self._selector = selector or FrameSelector()
        self._audio_bytes = 0
        self._frames: list[CapturedFrame] = []

    def append_audio(self, chunk: bytes) -> int:
        with self._audio_path.open("ab") as handle:
            handle.write(chunk)
        self._audio_bytes += len(chunk)
        return self._audio_bytes

    def offer_frame(self, t_seconds: float, jpeg: bytes) -> bool:
        try:
            signature = thumbnail_signature(jpeg)
        except (UnidentifiedImageError, OSError):
            return False
        if not self._selector.offer(t_seconds, signature):
            return False
        path = self._frames_dir / f"{t_seconds:08.1f}.jpg"
        path.write_bytes(jpeg)
        self._frames.append(CapturedFrame(t_seconds, path))
        return True

    def reset(self) -> None:
        self._audio_path.unlink(missing_ok=True)
        shutil.rmtree(self._frames_dir, ignore_errors=True)
        self._frames_dir.mkdir(parents=True, exist_ok=True)
        self._selector = FrameSelector()
        self._audio_bytes = 0
        self._frames = []

    def finish(
        self,
        *,
        ad_seconds: float,
        chapters: Sequence[Chapter] = (),
        description: str = "",
    ) -> CaptureResult:
        return CaptureResult(
            audio_path=self._audio_path if self._audio_bytes else None,
            frames=list(self._frames),
            ad_seconds=ad_seconds,
            chapters=list(chapters),
            description=description,
        )
