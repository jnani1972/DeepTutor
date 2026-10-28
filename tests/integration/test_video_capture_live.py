"""Manual live check of the Playwright capture backend against real YouTube.

Run with ``DEEPTUTOR_LIVE_YOUTUBE=1 pytest tests/integration/test_video_capture_live.py -v -s``.
"""

import asyncio
import os
import shutil

import pytest

from deeptutor.video_import.capture import PlaywrightCapture
from deeptutor.video_import.models import CaptureMode, PlaylistEntry

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("DEEPTUTOR_LIVE_YOUTUBE") != "1",
        reason="set DEEPTUTOR_LIVE_YOUTUBE=1 to play a real YouTube video",
    ),
]

# FuseSchool "Bile and Emulsification" (3:35), Creative Commons Attribution.
VIDEO = PlaylistEntry("TNG-RRzwBVU", "Bile and Emulsification", 215, 1)


def test_live_capture_records_audio_and_frames(tmp_path):
    out_dir = tmp_path / VIDEO.video_id
    try:
        result = asyncio.run(PlaywrightCapture(CaptureMode.headless).capture(VIDEO, out_dir))
        audio_bytes = result.audio_path.stat().st_size if result.audio_path else 0
        print(
            f"audio={audio_bytes}B frames={len(result.frames)} ad_seconds={result.ad_seconds:.1f} "
            f"chapters={len(result.chapters)} description_chars={len(result.description)}"
        )
        assert audio_bytes > 0
        assert len(result.frames) >= 1
        assert result.ad_seconds >= 0
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
