"""Real-time capture of a YouTube video's audio and key frames in a browser.

The video plays once, at 1x, in a Playwright-driven Chromium page. Audio comes
from the in-page ``<video>`` element (``capture_page.js``) and frames are
screenshots of that element. Nothing is downloaded, and bot checks are never
bypassed: a challenge raises :class:`BotChallengeError` so the user can act.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
import re
import time
from typing import Any, Literal, Protocol
from urllib.parse import parse_qs, urlparse

from PIL import UnidentifiedImageError

from deeptutor.video_import.frames import FrameSelector, thumbnail_signature
from deeptutor.video_import.models import CaptureMode, PlaylistEntry
from deeptutor.video_import.notes import Chapter

_PAGE_SCRIPT = Path(__file__).with_name("capture_page.js")
_WATCH_URL = "https://www.youtube.com/watch?v={video_id}"
_LOAD_TIMEOUT_MS = 45_000
_SCREENSHOT_TIMEOUT_MS = 5_000


class BotChallengeError(Exception):
    """YouTube asked for a human check; the user must resolve it."""


class VideoUnavailableError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


@dataclass(frozen=True)
class CapturedFrame:
    t_seconds: float
    path: Path


@dataclass
class CaptureResult:
    audio_path: Path | None
    frames: list[CapturedFrame]
    ad_seconds: float
    chapters: list[Chapter] = field(default_factory=list)
    description: str = ""


# --------------------------------------------------------------------------
# Page metadata (pure)
# --------------------------------------------------------------------------


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if "simpleText" in value:
            return str(value["simpleText"])
        return "".join(str(run.get("text", "")) for run in value.get("runs", []))
    return ""


def _walk(node: Any) -> Iterator[dict]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _marker_seconds(renderer: dict) -> float | None:
    for key in ("timeRangeStartMillis", "startMillis"):
        if key in renderer:
            return int(renderer[key]) / 1000
    start = renderer.get("onTap", {}).get("watchEndpoint", {}).get("startTimeSeconds")
    return float(start) if start is not None else None


def _structured_chapters(initial_data: dict) -> list[Chapter]:
    bar: list[Chapter] = []
    panel: list[Chapter] = []
    for node in _walk(initial_data):
        for key, bucket in (("chapterRenderer", bar), ("macroMarkersListItemRenderer", panel)):
            renderer = node.get(key)
            if isinstance(renderer, dict):
                seconds = _marker_seconds(renderer)
                title = _text(renderer.get("title")).strip()
                if seconds is not None and title:
                    bucket.append(Chapter(seconds, title))
    return sorted(set(bar or panel), key=lambda c: c.t_seconds)


_TIMESTAMP_LINE = re.compile(r"^\s*\(?((?:\d{1,2}:)?\d{1,2}:\d{2})\)?\s*[-–—:|]?\s*(.+?)\s*$")


def _description_chapters(description: str) -> list[Chapter]:
    chapters: list[Chapter] = []
    for line in description.splitlines():
        match = _TIMESTAMP_LINE.match(line)
        if not match:
            continue
        seconds = 0
        for part in match.group(1).split(":"):
            seconds = seconds * 60 + int(part)
        chapters.append(Chapter(float(seconds), match.group(2)))
    if len(chapters) >= 3 and chapters[0].t_seconds == 0:
        return chapters
    return []


def parse_page_metadata(player_response: dict, initial_data: dict) -> tuple[list[Chapter], str]:
    """Chapters and description from a watch page's embedded JSON."""
    description = str(player_response.get("videoDetails", {}).get("shortDescription") or "")
    chapters = _structured_chapters(initial_data) or _description_chapters(description)
    return chapters, description


# --------------------------------------------------------------------------
# Page classification and ad gating (pure)
# --------------------------------------------------------------------------

_CHALLENGE_URL_MARKERS = ("google.com/sorry", "/sorry/index", "consent.youtube.com", "consent.google.com")
_TEXT_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("challenge", ("not a bot", "unusual traffic")),
    ("age_restricted", ("confirm your age", "age-restricted", "inappropriate for some users")),
    ("members_only", ("join this channel", "members-only", "members only")),
    ("private", ("video is private", "private video")),
    ("removed", ("video unavailable", "has been removed", "no longer available", "terminated")),
)
_STATUS_CODES = {"LOGIN_REQUIRED": "sign_in_required", "ERROR": "removed"}


def classify_page(
    url: str, page_text: str, playability_status: str | None
) -> Literal["ok", "challenge"] | str:
    """``"ok"``, ``"challenge"`` or a :class:`VideoUnavailableError` code."""
    if any(marker in url for marker in _CHALLENGE_URL_MARKERS):
        return "challenge"
    if playability_status == "OK":
        return "ok"
    text = page_text.lower().replace("’", "'")
    for verdict, markers in _TEXT_MARKERS:
        if any(marker in text for marker in markers):
            return verdict
    return _STATUS_CODES.get(playability_status or "", "unplayable")


class AdGate:
    """Track ``ad-showing`` transitions and how long ads ran."""

    def __init__(self) -> None:
        self.in_ad = False
        self.ad_seconds = 0.0
        self._ad_started = 0.0

    def update(self, ad_showing: bool, t_seconds: float) -> Literal["record", "pause", "resume", "skip"]:
        if ad_showing and not self.in_ad:
            self.in_ad = True
            self._ad_started = t_seconds
            return "pause"
        if ad_showing:
            return "skip"
        if self.in_ad:
            self.in_ad = False
            self.ad_seconds += t_seconds - self._ad_started
            return "resume"
        return "record"

    def finish(self, t_seconds: float) -> None:
        if self.in_ad:
            self.update(False, t_seconds)


# --------------------------------------------------------------------------
# Playwright backend
# --------------------------------------------------------------------------


class CaptureBackend(Protocol):
    async def capture(self, entry: PlaylistEntry, out_dir: Path) -> CaptureResult: ...


_STATUS_JS = """() => {
  const status = (window.ytInitialPlayerResponse || {}).playabilityStatus || null;
  const body = document.body ? document.body.innerText.slice(0, 4000) : "";
  return [status ? status.status || null : null, JSON.stringify(status || {}) + "\\n" + body];
}"""
_METADATA_JS = "() => [window.ytInitialPlayerResponse || {}, window.ytInitialData || {}]"


def _watch_id(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.path != "/watch":
        return None
    return parse_qs(parsed.query).get("v", [None])[0]


class PlaywrightCapture(CaptureBackend):
    """Capture backend that plays the video in Chromium in real time."""

    def __init__(
        self,
        mode: CaptureMode,
        *,
        profile_dir: Path | None = None,
        sample_seconds: float = 1.0,
    ) -> None:
        if mode is CaptureMode.profile and profile_dir is None:
            raise ValueError("profile capture mode needs profile_dir")
        self.mode = mode
        self.profile_dir = profile_dir
        self.sample_seconds = sample_seconds

    async def capture(self, entry: PlaylistEntry, out_dir: Path) -> CaptureResult:
        from playwright.async_api import async_playwright

        out_dir.mkdir(parents=True, exist_ok=True)
        frames_dir = out_dir / "frames"
        frames_dir.mkdir(exist_ok=True)
        audio_path = out_dir / "audio.webm"
        audio_path.unlink(missing_ok=True)

        def on_audio_chunk(chunk_b64: str) -> None:
            with audio_path.open("ab") as handle:
                handle.write(base64.b64decode(chunk_b64))

        args = ["--autoplay-policy=no-user-gesture-required"]
        async with async_playwright() as pw:
            if self.mode is CaptureMode.profile:
                context = await pw.chromium.launch_persistent_context(
                    str(self.profile_dir), headless=False, args=args
                )
                browser = None
            else:
                browser = await pw.chromium.launch(
                    headless=self.mode is CaptureMode.headless, channel="chromium", args=args
                )
                context = await browser.new_context()
            try:
                page = await context.new_page()
                await page.expose_function("__dtAudioChunk", on_audio_chunk)
                result = await self._play(page, entry, frames_dir)
            finally:
                await context.close()
                if browser is not None:
                    await browser.close()

        has_audio = audio_path.exists() and audio_path.stat().st_size > 0
        result.audio_path = audio_path if has_audio else None
        return result

    async def _check_page(self, page: Any) -> None:
        status, text = await page.evaluate(_STATUS_JS)
        verdict = classify_page(page.url, text, status)
        if verdict == "challenge":
            raise BotChallengeError(f"YouTube asked for a human check at {page.url}")
        if verdict != "ok":
            raise VideoUnavailableError(verdict, f"video unavailable ({verdict})")

    async def _play(self, page: Any, entry: PlaylistEntry, frames_dir: Path) -> CaptureResult:
        from playwright.async_api import Error as PlaywrightError

        await page.goto(
            _WATCH_URL.format(video_id=entry.video_id),
            wait_until="domcontentloaded",
            timeout=_LOAD_TIMEOUT_MS,
        )
        await self._check_page(page)
        await page.wait_for_selector("#movie_player video", state="attached", timeout=_LOAD_TIMEOUT_MS)
        player_response, initial_data = await page.evaluate(_METADATA_JS)
        chapters, description = parse_page_metadata(player_response, initial_data)

        await page.evaluate(_PAGE_SCRIPT.read_text(encoding="utf-8"))
        await page.evaluate("() => window.__dtCapture.start()")

        video = page.locator("#movie_player video").first
        gate = AdGate()
        selector = FrameSelector()
        frames: list[CapturedFrame] = []
        player_duration = 0.0
        started = time.monotonic()
        elapsed = 0.0
        while True:
            tick = time.monotonic()
            elapsed = tick - started
            if _watch_id(page.url) != entry.video_id:
                await self._check_page(page)
                break
            state = await page.evaluate("() => window.__dtCapture.state()")
            if state["ended"]:
                break
            action = gate.update(bool(state["ad"]), elapsed)
            if action == "pause":
                await page.evaluate("() => window.__dtCapture.pause()")
            elif action == "resume":
                await page.evaluate("() => window.__dtCapture.resume()")
            if action in ("record", "resume"):
                if not player_duration and state["duration"] > 0:
                    player_duration = float(state["duration"])
                frame = await self._sample_frame(video, selector, float(state["t"]), frames_dir)
                if frame is not None:
                    frames.append(frame)
            limit = max(entry.duration_seconds, player_duration) * 1.5 + 120
            if elapsed > limit:
                break
            await asyncio.sleep(max(0.0, self.sample_seconds - (time.monotonic() - tick)))

        gate.finish(elapsed)
        try:
            await page.evaluate("() => window.__dtCapture.stop()")
        except PlaywrightError:
            pass  # page navigated away; chunks already delivered are kept
        return CaptureResult(
            audio_path=None,
            frames=frames,
            ad_seconds=gate.ad_seconds,
            chapters=chapters,
            description=description,
        )

    async def _sample_frame(
        self, video: Any, selector: FrameSelector, t_seconds: float, frames_dir: Path
    ) -> CapturedFrame | None:
        from playwright.async_api import Error as PlaywrightError

        try:
            image = await video.screenshot(type="jpeg", quality=80, timeout=_SCREENSHOT_TIMEOUT_MS)
            signature = thumbnail_signature(image)
        except (PlaywrightError, UnidentifiedImageError, OSError):
            return None  # one bad sample must not abort the video
        if not selector.offer(t_seconds, signature):
            return None
        path = frames_dir / f"{t_seconds:08.1f}.jpg"
        path.write_bytes(image)
        return CapturedFrame(t_seconds, path)


__all__ = [
    "AdGate",
    "BotChallengeError",
    "CaptureBackend",
    "CaptureResult",
    "CapturedFrame",
    "PlaywrightCapture",
    "VideoUnavailableError",
    "classify_page",
    "parse_page_metadata",
]
