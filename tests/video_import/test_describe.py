from pathlib import Path

import pytest

from deeptutor.video_import.capture import CapturedFrame
from deeptutor.video_import.describe import VisionUnavailableError, describe_frames
from deeptutor.video_import.notes import FrameNote


class FakeBatcher:
    def __init__(self, results):
        self.results = list(results)
        self.calls: list[list[dict[str, str]]] = []

    async def describe(self, images):
        self.calls.append(images)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def frame(tmp_path: Path, t: float) -> CapturedFrame:
    path = tmp_path / f"f{t}.jpg"
    path.write_bytes(b"\xff\xd8jpeg")
    return CapturedFrame(float(t), path)


@pytest.mark.asyncio
async def test_none_answers_and_failures_are_dropped(tmp_path):
    frames = [frame(tmp_path, t) for t in (0.0, 5.0, 10.0, 15.0)]
    batcher = FakeBatcher([["  Slide: Enzymes ", "None.", None, "NONE"]])
    notes = await describe_frames(frames, batcher=batcher)
    assert notes == [FrameNote(0.0, "Slide: Enzymes")]
    assert batcher.calls[0][0]["mimetype"] == "image/jpeg"
    assert batcher.calls[0][0]["filename"] == "f0.0.jpg"


@pytest.mark.asyncio
async def test_batch_exception_drops_that_batch_only(tmp_path):
    frames = [frame(tmp_path, t) for t in range(6)]
    batcher = FakeBatcher([RuntimeError("boom"), ["A", "B"]])
    notes = await describe_frames(frames, batcher=batcher)
    assert notes == [FrameNote(4.0, "A"), FrameNote(5.0, "B")]


@pytest.mark.asyncio
async def test_every_batch_failing_means_vision_is_unavailable(tmp_path):
    frames = [frame(tmp_path, t) for t in range(6)]
    batcher = FakeBatcher([ConnectionError("refused"), RuntimeError("later")])
    with pytest.raises(VisionUnavailableError, match="refused"):
        await describe_frames(frames, batcher=batcher)
