from deeptutor.video_import.capture import CaptureSink
from deeptutor.video_import.notes import Chapter


def test_sink_keeps_scene_changes_and_drops_duplicates(tmp_path, jpeg_of):
    sink = CaptureSink(tmp_path)
    assert sink.offer_frame(0.0, jpeg_of(10)) is True
    assert sink.offer_frame(1.0, jpeg_of(10)) is False
    assert sink.offer_frame(2.0, jpeg_of(200)) is True
    assert sorted(p.name for p in (tmp_path / "frames").iterdir()) == [
        "000000.0.jpg",
        "000002.0.jpg",
    ]


def test_sink_skips_undecodable_frame(tmp_path):
    assert CaptureSink(tmp_path).offer_frame(0.0, b"not a jpeg") is False


def test_sink_appends_audio_and_finishes(tmp_path, jpeg_of):
    sink = CaptureSink(tmp_path)
    sink.append_audio(b"ab")
    assert sink.append_audio(b"cd") == 4
    sink.offer_frame(0.0, jpeg_of(10))
    result = sink.finish(ad_seconds=3.0, chapters=[Chapter(0.0, "Intro")], description="d")
    assert result.audio_path.read_bytes() == b"abcd"
    assert len(result.frames) == 1 and result.ad_seconds == 3.0
    assert result.chapters == [Chapter(0.0, "Intro")] and result.description == "d"


def test_sink_without_audio_reports_none(tmp_path):
    assert CaptureSink(tmp_path).finish(ad_seconds=0.0).audio_path is None


def test_reset_clears_previous_attempt(tmp_path, jpeg_of):
    sink = CaptureSink(tmp_path)
    sink.append_audio(b"x")
    sink.offer_frame(0.0, jpeg_of(10))
    sink.reset()
    assert not (tmp_path / "audio.webm").exists()
    assert not any((tmp_path / "frames").glob("*.jpg"))
    assert sink.offer_frame(0.0, jpeg_of(10)) is True
