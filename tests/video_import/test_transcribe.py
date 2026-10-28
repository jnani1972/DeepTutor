from pathlib import Path

import pytest

from deeptutor.services.voice import TranscriptCue
from deeptutor.video_import.transcribe import transcribe_capture


@pytest.mark.asyncio
async def test_chunks_are_rebased_onto_video_time(tmp_path: Path):
    audio = tmp_path / "audio.webm"
    audio.write_bytes(b"x")

    async def chunker(_path):
        return [(0.0, 30.0, b"a"), (30.0, 60.0, b"b")]

    async def transcriber(data, **kw):
        assert kw["language"] is None
        return [TranscriptCue(1.0, 2.0, "hello" if data == b"a" else "world")]

    segs = await transcribe_capture(audio, chunker=chunker, transcriber=transcriber)
    assert [(round(s.start_seconds), s.text) for s in segs] == [(1, "hello"), (31, "world")]


@pytest.mark.asyncio
async def test_silence_yields_no_segments(tmp_path: Path):
    audio = tmp_path / "audio.webm"
    audio.write_bytes(b"x")

    async def chunker(_path):
        return [(0.0, 30.0, b"a"), (30.0, 60.0, b"b")]

    async def transcriber(_data, **_kw):
        return []

    assert await transcribe_capture(audio, chunker=chunker, transcriber=transcriber) == []
