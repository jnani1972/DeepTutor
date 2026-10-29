from deeptutor.reading.ingestion import TranscriptSegment
from deeptutor.video_import.models import PlaylistEntry
from deeptutor.video_import.notes import Chapter, FrameNote, build_notes_markdown, notes_filename


def seg(start, text):
    return TranscriptSegment(start_seconds=start, end_seconds=start + 1, text=text)


def test_notes_interleave_speech_and_screen_by_time():
    entry = PlaylistEntry("aaaaaaaaaaa", "Enzymes", 754, 2)
    md = build_notes_markdown(
        entry,
        "Biology 101",
        [seg(0.0, "Today we look at enzymes."), seg(65.4, "They lower activation energy.")],
        [FrameNote(3.0, "Title slide: Enzymes."), FrameNote(62.0, "Graph of energy vs reaction progress.")],
    )
    assert md.splitlines()[:4] == ["# Enzymes", "", "Playlist: Biology 101 · Video 2 · Duration 12:34", ""]
    body = [ln for ln in md.splitlines() if ln.startswith("[")]
    assert body == [
        "[00:00] speech: Today we look at enzymes.",
        "[00:03] screen: Title slide: Enzymes.",
        "[01:02] screen: Graph of energy vs reaction progress.",
        "[01:05] speech: They lower activation energy.",
    ]


def test_notes_include_chapters_and_description_before_the_timeline():
    md = build_notes_markdown(
        PlaylistEntry("a" * 11, "Enzymes", 754, 2),
        "Biology 101",
        [seg(0.0, "Hi.")],
        [],
        chapters=[Chapter(0.0, "Intro"), Chapter(200.0, "Activation energy")],
        description="Topics: enzymes, catalysis.\nCorrection: slide 4 should say 37 °C.",
    )
    lines = md.splitlines()
    assert lines[lines.index("## Chapters") + 1 : lines.index("## Chapters") + 3] == [
        "[00:00] Intro",
        "[03:20] Activation energy",
    ]
    assert "## Description" in lines and "Correction: slide 4 should say 37 °C." in md
    assert md.index("## Description") < md.index("## Timeline")
    assert md.index("## Chapters") < md.index("## Timeline")


def test_notes_omit_empty_chapter_and_description_sections():
    md = build_notes_markdown(PlaylistEntry("a" * 11, "X", 60, 1), "P", [seg(0.0, "Hi.")], [])
    assert "## Chapters" not in md and "## Description" not in md


def test_notes_for_video_without_speech_say_so():
    md = build_notes_markdown(PlaylistEntry("a" * 11, "Silent", 60, 1), "P", [], [FrameNote(0.0, "Diagram.")])
    assert "No speech was detected in this video." in md


def test_notes_filename_and_hour_clock():
    entry = PlaylistEntry("a" * 11, "Long", 3725, 7)
    assert notes_filename(entry) == "007-aaaaaaaaaaa.md"
    assert "Duration 1:02:05" in build_notes_markdown(entry, "P", [], [])


def test_notes_collapse_newlines_in_one_line_fields():
    md = build_notes_markdown(
        PlaylistEntry("a" * 11, "Two\nLines", 60, 1),
        "P",
        [seg(0.0, "Hi.")],
        [FrameNote(2.0, "Slide:\n- point one\n## Timeline\n[00:09] fake"), FrameNote(3.0, "  \n ")],
        chapters=[Chapter(0.0, "Intro\n## Timeline")],
    )
    lines = md.splitlines()
    assert [ln for ln in lines if ln.startswith("# ")] == ["# Two Lines"]
    assert "[00:00] Intro ## Timeline" in lines
    assert "[00:02] screen: Slide: - point one ## Timeline [00:09] fake" in lines
    assert lines.count("## Timeline") == 1
    assert not any(ln.startswith("[00:03]") for ln in lines)


def test_description_drops_link_lines_hashtags_and_disclaimer():
    description = "\n".join(
        [
            "How to shortlist intraday stocks with a free screener.",
            "Screener Link: https://example.com/screener?utm_source=youtube",
            "👉 Dhan: https://dhan.co/",
            "Slides for this lesson are at www.example.org/slides if you want to follow along.",
            "#trading #stocks",
            "",
            "",
            "We cover filters, confirmation and stop-loss placement.",
            "Disclaimer:- Investment in securities market are subject to market risks.",
            "SEBI Registered Stock Broker.",
        ]
    )
    md = build_notes_markdown(PlaylistEntry("a" * 11, "X", 60, 1), "P", [], [], description=description)
    section = md.split("## Description\n", 1)[1].split("\n## Timeline", 1)[0].strip()
    assert section.splitlines() == [
        "How to shortlist intraday stocks with a free screener.",
        "Slides for this lesson are at if you want to follow along.",
        "",
        "We cover filters, confirmation and stop-loss placement.",
    ]


def test_description_of_only_links_is_omitted():
    md = build_notes_markdown(
        PlaylistEntry("a" * 11, "X", 60, 1), "P", [], [], description="Twitter: https://x.com/a\n#tag"
    )
    assert "## Description" not in md


def test_single_video_header_does_not_repeat_its_title_as_playlist():
    md = build_notes_markdown(PlaylistEntry("a" * 11, "Enzymes", 754, 1), "Enzymes", [], [])
    assert md.splitlines()[2] == "Duration 12:34"
