"""Describe captured key frames with the configured local vision model."""

from __future__ import annotations

import base64
from collections.abc import Sequence
import logging

from deeptutor.services.llm.image_caption_batch import ImageCaptionBatcher
from deeptutor.services.llm.image_description import get_image_description_client
from deeptutor.video_import.capture import CapturedFrame
from deeptutor.video_import.notes import FrameNote

logger = logging.getLogger(__name__)

FRAME_PROMPT = (
    "Describe the teaching content visible in this video frame: slide titles and text, "
    "diagrams and what they show, equations, code, tables, and labelled objects. "
    "Be factual and concise. If the frame has no instructional content (a talking head, "
    "a blank or transition screen), answer exactly: NONE"
)
FRAME_SYSTEM_PROMPT = "You turn lecture video frames into accurate study notes."


def _image(frame: CapturedFrame) -> dict[str, str]:
    return {
        "base64": base64.b64encode(frame.path.read_bytes()).decode("ascii"),
        "mimetype": "image/jpeg",
        "filename": frame.path.name,
    }


async def describe_frames(
    frames: Sequence[CapturedFrame],
    *,
    batcher: ImageCaptionBatcher | None = None,
    batch_size: int = 4,
) -> list[FrameNote]:
    if not frames:
        return []
    if batcher is None:
        batcher = ImageCaptionBatcher(
            get_image_description_client(),
            prompt=FRAME_PROMPT,
            system_prompt=FRAME_SYSTEM_PROMPT,
        )
    notes: list[FrameNote] = []
    for start in range(0, len(frames), batch_size):
        batch = frames[start : start + batch_size]
        try:
            answers = await batcher.describe([_image(f) for f in batch])
        except Exception:
            logger.warning("Frame description batch at %.1fs failed", batch[0].t_seconds, exc_info=True)
            continue
        for frame, answer in zip(batch, answers):
            text = (answer or "").strip()
            if text and text != "NONE":
                notes.append(FrameNote(frame.t_seconds, text))
    return notes
