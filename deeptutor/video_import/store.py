"""Workspace-scoped, atomic persistence for video import jobs."""

from __future__ import annotations

import json
from pathlib import Path

from deeptutor.multi_user.paths import get_current_path_service
from deeptutor.services.file_io import atomic_write_json

from .models import ImportJob, PlaylistEntry, VideoStage
from .notes import notes_filename


class ImportJobStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (
            root or get_current_path_service().get_workspace_feature_dir("video_import")
        ).resolve()
        (self.root / "jobs").mkdir(parents=True, exist_ok=True)

    def _path(self, job_id: str) -> Path:
        return self.root / "jobs" / f"{job_id}.json"

    def save(self, job: ImportJob) -> None:
        atomic_write_json(self._path(job.job_id), job.to_dict())

    def load(self, job_id: str) -> ImportJob:
        try:
            data = json.loads(self._path(job_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise KeyError(job_id) from None
        return ImportJob.from_dict(data)

    def list(self) -> list[ImportJob]:
        jobs = [
            ImportJob.from_dict(json.loads(p.read_text(encoding="utf-8")))
            for p in (self.root / "jobs").glob("*.json")
        ]
        return sorted(jobs, key=lambda job: job.created_at, reverse=True)

    def video_dir(self, job_id: str, video_id: str) -> Path:
        path = self.root / "work" / job_id / video_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def notes_path(self, job_id: str, entry: PlaylistEntry) -> Path:
        return self.root / "notes" / job_id / notes_filename(entry)

    def done_video_ids(self, playlist_id: str) -> set[str]:
        return {
            video.video_id
            for job in self.list()
            if job.target.playlist_id == playlist_id
            for video in job.videos
            if video.stage == VideoStage.done
        }
