import { activeWorkspaceId, scopedUrl } from "@/lib/workspace-scope";

import type { FinishBody, ImportJob, ImportVideo } from "./api";

/** Mirrors `YOUTUBE_PLAYER_ERRORS` in deeptutor/video_import/capture.py. */
const YOUTUBE_PLAYER_ERRORS: Record<number, string> = {
  2: "unplayable",
  5: "unplayable",
  100: "removed",
  101: "embed_blocked",
  150: "embed_blocked",
};

export function playerErrorCode(code: number): string {
  return YOUTUBE_PLAYER_ERRORS[code] ?? String(code);
}

/** The next video the browser must play: queued, or one whose capture was interrupted. */
export function nextCaptureVideo(job: ImportJob): ImportVideo | null {
  const pending = job.videos
    .filter((video) => video.stage === "queued" || video.stage === "capturing")
    .sort((a, b) => a.position - b.position);
  return pending[0] ?? null;
}

export const TERMINAL_JOB_STATUSES = new Set(["done", "failed", "cancelled"]);

export function formatDuration(totalSeconds: number): string {
  const total = Math.max(0, Math.round(totalSeconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = String(total % 60).padStart(2, "0");
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${seconds}`
    : `${minutes}:${seconds}`;
}

/** Browser route for the deep-import pages, kept inside the active workspace. */
export function deepImportRoute(jobId?: string, workspaceId = activeWorkspaceId()): string {
  const path = jobId
    ? `/learning/deep-import/${encodeURIComponent(jobId)}`
    : "/learning/deep-import";
  return workspaceId ? scopedUrl(path, workspaceId) : path;
}

/** Failed frame uploads tolerated per video; frames are lossy by design. */
export const FRAME_FAILURE_LIMIT = 5;

export type CaptureOutcome =
  | { kind: "ended" }
  | { kind: "error"; code: string }
  | { kind: "stopped" }
  | { kind: "capture_failed"; message: string };

export interface CaptureResult {
  adSeconds: number;
  audioFailed: boolean;
  failedFrames: number;
}

export type FinishPlan =
  | { kind: "finish"; body: FinishBody }
  | { kind: "retry"; message: string };

/**
 * Decide what to tell the server once a video's capture stopped.
 *
 * A capture that lost audio (including the recorder's final flush) or hit the
 * frame-failure limit is never finished: the owner retries the video, and
 * `capture/start` resets the server-side sink. Sharing stopped → `ended:false`
 * so the server re-queues an incomplete capture.
 */
export function finishPlan(
  outcome: CaptureOutcome,
  result: CaptureResult,
  playerDuration: number,
): FinishPlan {
  const base = { ad_seconds: result.adSeconds, player_duration: playerDuration };
  if (outcome.kind === "error") {
    return { kind: "finish", body: { ended: false, ...base, error_code: outcome.code } };
  }
  if (outcome.kind === "capture_failed") return { kind: "retry", message: outcome.message };
  if (result.audioFailed) {
    return { kind: "retry", message: "An audio chunk could not be uploaded." };
  }
  if (result.failedFrames >= FRAME_FAILURE_LIMIT) {
    return {
      kind: "retry",
      message: `${result.failedFrames} frame uploads failed.`,
    };
  }
  return { kind: "finish", body: { ended: outcome.kind === "ended", ...base } };
}
