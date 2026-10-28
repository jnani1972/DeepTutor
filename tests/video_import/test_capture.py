import pytest

from deeptutor.video_import.capture import AdGate, classify_page, parse_page_metadata
from deeptutor.video_import.notes import Chapter


def player_response_with(*, desc: str = "") -> dict:
    return {"videoDetails": {"shortDescription": desc}}


def initial_data_with_markers(markers: list[tuple[int, str]]) -> dict:
    items = [
        {
            "macroMarkersListItemRenderer": {
                "title": {"simpleText": title},
                "timeDescription": {"simpleText": "0:00"},
                "onTap": {"watchEndpoint": {"startTimeSeconds": start_ms // 1000}},
            }
        }
        for start_ms, title in markers
    ]
    bar = [
        {"chapterRenderer": {"title": {"simpleText": title}, "timeRangeStartMillis": start_ms}}
        for start_ms, title in reversed(markers)
    ]
    return {
        "engagementPanels": [
            {
                "engagementPanelSectionListRenderer": {
                    "content": {"macroMarkersListRenderer": {"contents": items}}
                }
            }
        ],
        "playerOverlays": {
            "playerOverlayRenderer": {
                "decoratedPlayerBarRenderer": {
                    "decoratedPlayerBarRenderer": {
                        "playerBar": {
                            "multiMarkersPlayerBarRenderer": {
                                "markersMap": [
                                    {"key": "DESCRIPTION_CHAPTERS", "value": {"chapters": bar}}
                                ]
                            }
                        }
                    }
                }
            }
        },
    }


def test_page_metadata_prefers_structured_chapters():
    chapters, desc = parse_page_metadata(
        player_response_with(desc="About enzymes."),
        initial_data_with_markers([(0, "Intro"), (200_000, "Activation energy")]),
    )
    assert chapters == [Chapter(0.0, "Intro"), Chapter(200.0, "Activation energy")]
    assert desc == "About enzymes."


def test_page_metadata_falls_back_to_description_timestamps():
    desc = "Notes\n0:00 Intro\n3:20 Activation energy\n1:02:03 Summary"
    chapters, _ = parse_page_metadata(player_response_with(desc=desc), {})
    assert chapters == [
        Chapter(0.0, "Intro"),
        Chapter(200.0, "Activation energy"),
        Chapter(3723.0, "Summary"),
    ]


def test_description_timestamps_not_starting_at_zero_are_not_chapters():
    chapters, _ = parse_page_metadata(player_response_with(desc="1:00 A\n2:00 B\n3:00 C"), {})
    assert chapters == []


def test_ad_gate_pauses_and_resumes():
    g = AdGate()
    assert [g.update(a, t) for a, t in [(False, 0), (True, 1), (True, 2), (False, 3), (False, 4)]] == [
        "record",
        "pause",
        "skip",
        "resume",
        "record",
    ]


def test_ad_gate_accumulates_ad_seconds():
    g = AdGate()
    for a, t in [(False, 0), (True, 1), (True, 2), (False, 4), (True, 10), (False, 11.5)]:
        g.update(a, t)
    assert g.ad_seconds == pytest.approx(4.5)


@pytest.mark.parametrize(
    "url,text,status,expected",
    [
        ("https://www.google.com/sorry/index?continue=…", "", None, "challenge"),
        (
            "https://www.youtube.com/watch?v=x",
            "Sign in to confirm you’re not a bot",
            "LOGIN_REQUIRED",
            "challenge",
        ),
        (
            "https://www.youtube.com/watch?v=x",
            "Sign in to confirm your age",
            "LOGIN_REQUIRED",
            "age_restricted",
        ),
        (
            "https://www.youtube.com/watch?v=x",
            "Join this channel to get access",
            "UNPLAYABLE",
            "members_only",
        ),
        ("https://www.youtube.com/watch?v=x", "This video is private", "LOGIN_REQUIRED", "private"),
        ("https://www.youtube.com/watch?v=x", "Video unavailable", "ERROR", "removed"),
        ("https://www.youtube.com/watch?v=x", "", "OK", "ok"),
        ("https://www.youtube.com/watch?v=x", "Something went wrong. Refresh", None, "unplayable"),
    ],
)
def test_classify_page(url, text, status, expected):
    assert classify_page(url, text, status) == expected
