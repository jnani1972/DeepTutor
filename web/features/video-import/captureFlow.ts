import { activeWorkspaceId, scopedUrl } from "@/lib/workspace-scope";

import type { ImportJob, ImportVideo } from "./api";

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
