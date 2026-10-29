"""CLI: import local media folders (and inspect YouTube imports) as JSON.

Browser capture of YouTube videos is web-only; this CLI starts folder jobs end
to end and controls/inspects any job.

Only one process may process imports or change jobs (see
``deeptutor.video_import.ownership``). While the DeepTutor server runs, it owns
them: ``start``/``pause``/``resume``/``cancel`` here refuse with a clear
message (``preview``/``status`` still work). With no server running, the CLI
takes ownership and ``start --folder`` and ``resume`` process to completion.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from rich.console import Console
import typer

console = Console()


def _get_runner() -> Any:
    from deeptutor.video_import.runner import get_import_runner

    return get_import_runner()


def _emit(payload: Any) -> None:
    console.print_json(json.dumps(payload))


def _fail(message: str) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(1)


def _source(url: str | None, folder: Path | None) -> tuple[str | None, str | None]:
    if (url is None) == (folder is None):
        _fail("Give exactly one of URL or --folder")
    return url, str(folder) if folder is not None else None


def register(app: typer.Typer) -> None:
    @app.command("preview")
    def preview(
        url: str | None = typer.Argument(None, help="YouTube video or playlist URL."),
        folder: Path | None = typer.Option(None, "--folder", help="Local media folder."),
    ) -> None:
        """List what an import would cover, without starting it."""
        url, folder_path = _source(url, folder)

        async def go() -> dict[str, Any]:
            target, listing = await _get_runner().preview(url=url, folder=folder_path)
            return {
                "kind": target.kind.value,
                "title": listing.title,
                "entries": [
                    {
                        "video_id": e.video_id,
                        "title": e.title,
                        "duration_seconds": e.duration_seconds,
                        "position": e.position,
                    }
                    for e in listing.entries
                ],
                "total_seconds": sum(e.duration_seconds for e in listing.entries),
            }

        try:
            _emit(asyncio.run(go()))
        except Exception as exc:
            _fail(f"{getattr(exc, 'code', 'error')}: {exc}")

    @app.command("start")
    def start(
        url: str | None = typer.Argument(None, help="YouTube video or playlist URL."),
        folder: Path | None = typer.Option(None, "--folder", help="Local media folder."),
        kb: str | None = typer.Option(None, "--kb", help="Knowledge base name."),
        only: list[str] | None = typer.Option(None, "--only", help="Video id to import (repeatable)."),
    ) -> None:
        """Create an import job; folder jobs are processed until finished."""
        url, folder_path = _source(url, folder)

        async def go() -> dict[str, Any]:
            runner = _get_runner()
            job = await runner.create(
                url=url, folder=folder_path, video_ids=only or None, kb_name=kb
            )
            if folder_path is not None:
                await runner.drain()
                job = runner.get_job(job.job_id)
            return job.to_dict()

        try:
            _emit(asyncio.run(go()))
        except Exception as exc:
            _fail(f"{getattr(exc, 'code', 'error')}: {exc}")

    @app.command("status")
    def status(job_id: str | None = typer.Argument(None, help="Job id; omit to list all.")) -> None:
        """Show one job or list all jobs."""
        runner = _get_runner()
        if job_id is None:
            _emit([job.to_dict() for job in runner.list_jobs()])
            return
        try:
            _emit(runner.get_job(job_id).to_dict())
        except KeyError:
            _fail(f"Unknown import job: {job_id}")

    def _control(action: str, job_id: str) -> None:
        async def go() -> Any:
            runner = _get_runner()
            job = await getattr(runner, action)(job_id)
            if action == "resume":
                # This process owns the work now: finish it before exiting.
                await runner.drain()
                job = runner.get_job(job_id)
            return job

        try:
            job = asyncio.run(go())
        except KeyError:
            _fail(f"Unknown import job: {job_id}")
        except ValueError as exc:
            _fail(str(exc))
        _emit(job.to_dict())

    @app.command("pause")
    def pause(job_id: str = typer.Argument(...)) -> None:
        """Pause a job."""
        _control("pause", job_id)

    @app.command("resume")
    def resume(job_id: str = typer.Argument(...)) -> None:
        """Resume a paused or failed job and process it until it stops."""
        _control("resume", job_id)

    @app.command("cancel")
    def cancel(job_id: str = typer.Argument(...)) -> None:
        """Cancel a job."""
        _control("cancel", job_id)
