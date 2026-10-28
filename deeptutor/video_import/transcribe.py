"""Transcribe a captured ``audio.webm`` into timed transcript segments."""

from __future__ import annotations

from pathlib import Path

from deeptutor.reading.ingestion import (
    MediaChunker,
    Transcriber,
    TranscriptSegment,
    _chunk_media_audio,
    _rebase_cues,
    build_transcript_segments,
)
from deeptutor.services.voice import transcribe_audio_cues


async def transcribe_capture(
    audio_path: Path,
    *,
    chunker: MediaChunker | None = None,
    transcriber: Transcriber | None = None,
) -> list[TranscriptSegment]:
    """Chunk, transcribe and rebase one captured audio file onto video time.

    The language is always left to the STT model so each video is transcribed
    in the language actually spoken.
    """
    chunker = chunker or _chunk_media_audio
    transcriber = transcriber or transcribe_audio_cues
    cues: list[TranscriptSegment] = []
    for index, (start, end, audio) in enumerate(await chunker(audio_path), start=1):
        spoken = await transcriber(
            audio,
            filename=f"{audio_path.stem}-{index:04d}.mp3",
            content_type="audio/mpeg",
            language=None,
        )
        cues.extend(_rebase_cues(spoken, start, end))
    return build_transcript_segments(cues)
