import { ApiError, apiUrl, requestJson } from "@/lib/api";

/** Client for the backend's `/api/video-import` router (deep import). */
const BASE = "/api/video-import";

export type ImportKind = "playlist" | "video" | "folder";

export interface PreviewEntry {
  video_id: string;
  title: string;
  duration_seconds: number;
  position: number;
}

export interface PreviewResult {
  kind: ImportKind;
  title: string;
  entries: PreviewEntry[];
  total_seconds: number;
}

export type VideoStage =
  | "queued"
  | "capturing"
  | "captured"
  | "transcribed"
  | "described"
  | "done"
  | "skipped"
  | "failed";

export type JobStatus =
  | "pending"
  | "capturing"
  | "processing"
  | "paused"
  | "waiting_for_playback"
  | "done"
  | "failed"
  | "cancelled";

export interface ImportVideo {
  video_id: string;
  title: string;
  position: number;
  duration_seconds: number;
  stage: VideoStage;
  error_code: string;
  error_detail: string;
  speech_cues: number;
  frame_notes: number;
}

export interface ImportJob {
  job_id: string;
  source_url: string;
  target: {
    kind: ImportKind;
    playlist_id: string | null;
    video_id: string | null;
    folder: string | null;
  };
  title: string;
  kb_name: string;
  status: JobStatus;
  capture_mode: "browser" | "file";
  videos: ImportVideo[];
  created_at: string;
  updated_at: string;
  error_code: string;
  error_detail: string;
}

export interface ImportSource {
  url?: string;
  folder?: string;
}

export interface CreateJobBody extends ImportSource {
  video_ids?: string[];
  kb_name?: string;
}

export interface CaptureSession {
  sample_seconds: number;
  audio_timeslice_ms: number;
}

export interface FinishBody {
  ended: boolean;
  ad_seconds: number;
  player_duration: number;
  error_code?: string;
}

export interface UploadOptions {
  /** Retries after the first attempt; defaults to 2. */
  retries?: number;
  retryDelayMs?: number;
  signal?: AbortSignal;
}

const seg = encodeURIComponent;
const jobPath = (jobId: string) => `${BASE}/jobs/${seg(jobId)}`;
const capturePath = (jobId: string, videoId: string, step: string) =>
  `${jobPath(jobId)}/videos/${seg(videoId)}/capture/${step}`;

function postJson<T>(path: string, body?: unknown): Promise<T> {
  return requestJson<T>(apiUrl(path), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
}

export function previewImport(source: ImportSource): Promise<PreviewResult> {
  return postJson(`${BASE}/preview`, source);
}

export function createImportJob(body: CreateJobBody): Promise<ImportJob> {
  return postJson(`${BASE}/jobs`, body);
}

export function listImportJobs(): Promise<ImportJob[]> {
  return requestJson(apiUrl(`${BASE}/jobs`));
}

export function getImportJob(jobId: string): Promise<ImportJob> {
  return requestJson(apiUrl(jobPath(jobId)));
}

export function controlImportJob(
  jobId: string,
  action: "pause" | "resume" | "cancel",
): Promise<ImportJob> {
  return postJson(`${jobPath(jobId)}/${action}`);
}

export function startCapture(jobId: string, videoId: string): Promise<CaptureSession> {
  return postJson(capturePath(jobId, videoId, "start"));
}

export function finishCapture(
  jobId: string,
  videoId: string,
  body: FinishBody,
): Promise<ImportJob> {
  return postJson(capturePath(jobId, videoId, "finish"), body);
}

function delay(ms: number): Promise<void> {
  return ms > 0 ? new Promise((resolve) => setTimeout(resolve, ms)) : Promise.resolve();
}

/** Run an upload, retrying retryable failures (network, 5xx) up to `retries` times. */
async function withRetry<T>(run: () => Promise<T>, options: UploadOptions = {}): Promise<T> {
  const retries = options.retries ?? 2;
  const retryDelayMs = options.retryDelayMs ?? 500;
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await run();
    } catch (error) {
      const retryable = !(error instanceof ApiError) || error.retryable;
      if (!retryable || attempt >= retries || options.signal?.aborted) throw error;
      await delay(retryDelayMs * (attempt + 1));
    }
  }
}

function postRaw<T>(url: string, body: Blob, contentType: string, signal?: AbortSignal): Promise<T> {
  return requestJson<T>(url, {
    method: "POST",
    headers: { "Content-Type": contentType },
    body,
    signal,
  });
}

export function uploadAudioChunk(
  jobId: string,
  videoId: string,
  chunk: Blob,
  options?: UploadOptions,
): Promise<{ bytes: number }> {
  const url = apiUrl(capturePath(jobId, videoId, "audio"));
  return withRetry(() => postRaw(url, chunk, "audio/webm", options?.signal), options);
}

export function uploadFrame(
  jobId: string,
  videoId: string,
  t: number,
  frame: Blob,
  options?: UploadOptions,
): Promise<{ kept: boolean }> {
  const seconds = Math.max(0, Number.isFinite(t) ? t : 0).toFixed(1);
  const url = apiUrl(`${capturePath(jobId, videoId, "frame")}?t=${seconds}`);
  return withRetry(() => postRaw(url, frame, "image/jpeg", options?.signal), options);
}
