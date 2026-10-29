"""Key-frame selection for sampled video screenshots."""

from __future__ import annotations

import io

from PIL import Image

_THUMB_SIZE = (32, 18)


def thumbnail_signature(image_bytes: bytes) -> bytes:
    """Return the raw 32x18 grayscale thumbnail (576 bytes) of an image."""
    with Image.open(io.BytesIO(image_bytes)) as image:
        return image.convert("L").resize(_THUMB_SIZE, Image.BILINEAR).tobytes()


def signature_distance(a: bytes, b: bytes) -> float:
    """Mean absolute difference between two signatures, scaled 0-1."""
    return sum(abs(x - y) for x, y in zip(a, b)) / (len(a) * 255)


class FrameSelector:
    """Decide which sampled frames to keep, comparing against the last kept one."""

    def __init__(
        self,
        *,
        scene_threshold: float = 0.12,
        dedupe_floor: float = 0.02,
        periodic_seconds: float = 5.0,
    ) -> None:
        self.scene_threshold = scene_threshold
        self.dedupe_floor = dedupe_floor
        self.periodic_seconds = periodic_seconds
        self._last_time: float | None = None
        self._last_signature: bytes | None = None

    def offer(self, t_seconds: float, signature: bytes) -> bool:
        if self._last_signature is None or self._last_time is None:
            keep = True
        else:
            diff = signature_distance(self._last_signature, signature)
            keep = diff > self.scene_threshold or (
                t_seconds - self._last_time >= self.periodic_seconds
                and diff > self.dedupe_floor
            )
        if keep:
            self._last_time = t_seconds
            self._last_signature = signature
        return keep
