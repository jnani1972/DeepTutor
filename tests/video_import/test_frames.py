from deeptutor.video_import.frames import (
    FrameSelector,
    signature_distance,
    thumbnail_signature,
)


def sig(v: int) -> bytes:
    return bytes([v]) * 576


def test_first_frame_is_kept():
    assert FrameSelector().offer(0.0, sig(10)) is True


def test_identical_frames_are_dropped_even_after_periodic_interval():
    s = FrameSelector()
    s.offer(0.0, sig(10))
    assert s.offer(6.0, sig(10)) is False


def test_small_change_kept_only_after_periodic_interval():
    s = FrameSelector()
    s.offer(0.0, sig(10))
    assert s.offer(2.0, sig(18)) is False  # 8/255 ~ 0.031 > floor, < scene, < 5 s
    assert s.offer(5.0, sig(18)) is True


def test_scene_change_kept_immediately():
    s = FrameSelector()
    s.offer(0.0, sig(10))
    assert s.offer(1.0, sig(60)) is True  # 50/255 ~ 0.196 > 0.12


def test_distance_of_real_images_is_zero_for_same_image(png_bytes):
    a = thumbnail_signature(png_bytes)
    assert len(a) == 576
    assert signature_distance(a, thumbnail_signature(png_bytes)) == 0.0
