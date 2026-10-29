"""Video import API: preview, jobs, browser capture upload.

A thin adapter over :class:`deeptutor.video_import.runner.ImportRunner`. The
runner scopes every job to the calling user and workspace, so another user's job
id surfaces here as ``KeyError`` and becomes a 404. ``deeptutor.video_import``
is imported lazily so a base install without the ``video-import`` extra
(``yt-dlp``) still boots the app.

Responses are job progress only; note text is never returned.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

router = APIRouter()

SAMPLE_SECONDS = 1
AUDIO_TIMESLICE_MS = 5000


def get_runner() -> Any:
    from deeptutor.video_import.runner import get_import_runner

    return get_import_runner()


class SourceBody(BaseModel):
    url: str | None = None
    folder: str | None = None


class CreateBody(SourceBody):
    video_ids: list[str] | None = None
    kb_name: str | None = None


class FinishBody(BaseModel):
    ended: bool
    ad_seconds: float = 0.0
    player_duration: float = 0.0
    error_code: str | None = None


def _listing_failure(exc: Exception) -> HTTPException | None:
    from deeptutor.video_import.listing import ListingError

    if isinstance(exc, ListingError):
        return HTTPException(422, detail={"code": exc.code, "message": str(exc)})
    return None


def _not_found(job_id: str) -> HTTPException:
    return HTTPException(404, detail=f"Unknown import job: {job_id}")


async def _read_limited(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, detail=f"Body exceeds {limit} bytes")
    body = await request.body()
    if len(body) > limit:
        raise HTTPException(413, detail=f"Body exceeds {limit} bytes")
    return body


def _sink(runner: Any, job_id: str, video_id: str) -> Any:
    try:
        return runner.sink(job_id, video_id)
    except KeyError:
        raise HTTPException(409, detail="No capture in progress for this video") from None


@router.post("/preview")
async def preview(body: SourceBody, runner: Any = Depends(get_runner)) -> dict[str, Any]:
    try:
        target, listing = await runner.preview(url=body.url, folder=body.folder)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    except Exception as exc:
        failure = _listing_failure(exc)
        if failure is None:
            raise
        raise failure from exc
    return {
        "kind": target.kind.value,
        "title": listing.title,
        "entries": [asdict(entry) for entry in listing.entries],
        "total_seconds": sum(entry.duration_seconds for entry in listing.entries),
    }


@router.post("/jobs")
async def create_job(body: CreateBody, runner: Any = Depends(get_runner)) -> dict[str, Any]:
    try:
        job = await runner.create(
            url=body.url, folder=body.folder, video_ids=body.video_ids, kb_name=body.kb_name
        )
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    except Exception as exc:
        failure = _listing_failure(exc)
        if failure is None:
            raise
        raise failure from exc
    return job.to_dict()


@router.get("/jobs")
async def list_jobs(runner: Any = Depends(get_runner)) -> list[dict[str, Any]]:
    jobs = await asyncio.to_thread(runner.list_jobs)
    return [job.to_dict() for job in jobs]


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, runner: Any = Depends(get_runner)) -> dict[str, Any]:
    try:
        job = await asyncio.to_thread(runner.get_job, job_id)
    except KeyError:
        raise _not_found(job_id) from None
    return job.to_dict()


async def _control(runner: Any, action: str, job_id: str) -> dict[str, Any]:
    try:
        job = await getattr(runner, action)(job_id)
    except KeyError:
        raise _not_found(job_id) from None
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return job.to_dict()


@router.post("/jobs/{job_id}/pause")
async def pause_job(job_id: str, runner: Any = Depends(get_runner)) -> dict[str, Any]:
    """Pause a job.

    On a job that is currently running this only sets a flag the worker honours
    at its next checkpoint, so the returned job may still show the old status.
    Poll ``GET /jobs/{job_id}`` for the settled state.
    """
    return await _control(runner, "pause", job_id)


@router.post("/jobs/{job_id}/resume")
async def resume_job(job_id: str, runner: Any = Depends(get_runner)) -> dict[str, Any]:
    return await _control(runner, "resume", job_id)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, runner: Any = Depends(get_runner)) -> dict[str, Any]:
    """Cancel a job.

    Like pause, cancelling a running job only sets a flag; the response may
    show the old status until the worker stops. Poll ``GET /jobs/{job_id}``.
    """
    return await _control(runner, "cancel", job_id)


@router.post("/jobs/{job_id}/videos/{video_id}/capture/start")
async def capture_start(
    job_id: str, video_id: str, runner: Any = Depends(get_runner)
) -> dict[str, int]:
    try:
        await asyncio.to_thread(runner.begin_capture, job_id, video_id)
    except KeyError:
        raise _not_found(job_id) from None
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return {"sample_seconds": SAMPLE_SECONDS, "audio_timeslice_ms": AUDIO_TIMESLICE_MS}


@router.post("/jobs/{job_id}/videos/{video_id}/capture/audio")
async def capture_audio(
    job_id: str, video_id: str, request: Request, runner: Any = Depends(get_runner)
) -> dict[str, int]:
    from deeptutor.video_import.capture import MAX_AUDIO_CHUNK_BYTES

    body = await _read_limited(request, MAX_AUDIO_CHUNK_BYTES)
    sink = _sink(runner, job_id, video_id)
    return {"bytes": await asyncio.to_thread(sink.append_audio, body)}


@router.post("/jobs/{job_id}/videos/{video_id}/capture/frame")
async def capture_frame(
    job_id: str,
    video_id: str,
    request: Request,
    t: float = Query(..., ge=0),
    runner: Any = Depends(get_runner),
) -> dict[str, bool]:
    from deeptutor.video_import.capture import MAX_FRAME_BYTES

    body = await _read_limited(request, MAX_FRAME_BYTES)
    sink = _sink(runner, job_id, video_id)
    return {"kept": await asyncio.to_thread(sink.offer_frame, t, body)}


@router.post("/jobs/{job_id}/videos/{video_id}/capture/finish")
async def capture_finish(
    job_id: str, video_id: str, body: FinishBody, runner: Any = Depends(get_runner)
) -> dict[str, Any]:
    try:
        job = await runner.finish_capture(
            job_id,
            video_id,
            ended=body.ended,
            ad_seconds=body.ad_seconds,
            player_duration=body.player_duration,
            error_code=body.error_code,
        )
    except KeyError:
        raise _not_found(job_id) from None
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return job.to_dict()
