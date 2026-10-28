"""Shared data models for the YouTube video import pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class TargetKind(str, Enum):
    PLAYLIST = "playlist"
    VIDEO = "video"
    FOLDER = "folder"


@dataclass(frozen=True)
class ImportTarget:
    kind: TargetKind
    playlist_id: str | None
    video_id: str | None
    folder: str | None = None


@dataclass
class PlaylistEntry:
    video_id: str
    title: str
    duration_seconds: int
    position: int  # 1-based


class VideoStage(str, Enum):
    queued = "queued"
    capturing = "capturing"
    captured = "captured"
    transcribed = "transcribed"
    described = "described"
    done = "done"
    skipped = "skipped"
    failed = "failed"


class JobStatus(str, Enum):
    pending = "pending"
    capturing = "capturing"
    processing = "processing"
    paused = "paused"
    waiting_for_playback = "waiting_for_playback"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


class CaptureMode(str, Enum):
    browser = "browser"
    file = "file"


@dataclass
class VideoState:
    video_id: str
    title: str
    position: int
    duration_seconds: int
    stage: VideoStage = VideoStage.queued
    error_code: str = ""
    error_detail: str = ""
    speech_cues: int = 0
    frame_notes: int = 0


@dataclass
class ImportJob:
    job_id: str
    source_url: str
    target: ImportTarget
    title: str
    kb_name: str
    status: JobStatus
    capture_mode: CaptureMode
    videos: list[VideoState]
    created_at: str
    updated_at: str
    error_code: str = ""
    error_detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["capture_mode"] = self.capture_mode.value
        data["target"]["kind"] = self.target.kind.value
        for raw, video in zip(data["videos"], self.videos):
            raw["stage"] = video.stage.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ImportJob:
        target = data["target"]
        return cls(
            job_id=data["job_id"],
            source_url=data["source_url"],
            target=ImportTarget(
                TargetKind(target["kind"]),
                target.get("playlist_id"),
                target.get("video_id"),
                target.get("folder"),
            ),
            title=data["title"],
            kb_name=data["kb_name"],
            status=JobStatus(data["status"]),
            capture_mode=CaptureMode(data["capture_mode"]),
            videos=[
                VideoState(
                    video_id=v["video_id"],
                    title=v["title"],
                    position=v["position"],
                    duration_seconds=v["duration_seconds"],
                    stage=VideoStage(v.get("stage", "queued")),
                    error_code=v.get("error_code", ""),
                    error_detail=v.get("error_detail", ""),
                    speech_cues=v.get("speech_cues", 0),
                    frame_notes=v.get("frame_notes", 0),
                )
                for v in data["videos"]
            ],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            error_code=data.get("error_code", ""),
            error_detail=data.get("error_detail", ""),
        )
