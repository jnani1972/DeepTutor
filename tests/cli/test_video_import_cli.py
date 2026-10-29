from __future__ import annotations

import json

from typer.testing import CliRunner

from deeptutor_cli import video_import as cli_module
from deeptutor_cli.main import app

runner = CliRunner()


class FakeJob:
    def __init__(self, job_id: str = "j1") -> None:
        self.job_id = job_id

    def to_dict(self) -> dict:
        return {"job_id": self.job_id, "status": "pending"}


class FakeImportRunner:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def create(self, **kwargs):
        self.calls.append(("create", kwargs))
        return FakeJob()

    def list_jobs(self):
        return [FakeJob("j1"), FakeJob("j2")]

    def get_job(self, job_id):
        if job_id == "nope":
            raise KeyError(job_id)
        return FakeJob(job_id)

    async def pause(self, job_id):
        self.calls.append(("pause", job_id))
        return FakeJob(job_id)

    async def drain(self):
        self.calls.append(("drain",))


def _patch(monkeypatch) -> FakeImportRunner:
    fake = FakeImportRunner()
    monkeypatch.setattr(cli_module, "_get_runner", lambda: fake)
    return fake


def test_start_folder_prints_job_json(monkeypatch, tmp_path):
    fake = _patch(monkeypatch)
    result = runner.invoke(
        app, ["video-import", "start", "--folder", str(tmp_path), "--kb", "k", "--only", "a"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["job_id"] == "j1"
    assert fake.calls[0] == (
        "create",
        {"url": None, "folder": str(tmp_path), "video_ids": ["a"], "kb_name": "k"},
    )


def test_start_requires_exactly_one_source(monkeypatch):
    _patch(monkeypatch)
    assert runner.invoke(app, ["video-import", "start"]).exit_code != 0


def test_status_and_pause(monkeypatch):
    _patch(monkeypatch)
    listing = runner.invoke(app, ["video-import", "status"])
    assert [j["job_id"] for j in json.loads(listing.stdout)] == ["j1", "j2"]
    one = runner.invoke(app, ["video-import", "status", "j2"])
    assert json.loads(one.stdout)["job_id"] == "j2"
    assert runner.invoke(app, ["video-import", "status", "nope"]).exit_code == 1
    paused = runner.invoke(app, ["video-import", "pause", "j1"])
    assert json.loads(paused.stdout)["job_id"] == "j1"
