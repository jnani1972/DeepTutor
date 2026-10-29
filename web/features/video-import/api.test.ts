import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  controlImportJob,
  createImportJob,
  finishCapture,
  getImportJob,
  listImportJobs,
  previewImport,
  startCapture,
  uploadAudioChunk,
  uploadFrame,
} from "./api";

type Call = { url: URL; init: RequestInit };

let calls: Call[];
let responses: Array<() => Response>;

function json(body: unknown, status = 200): () => Response {
  return () =>
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
}

beforeEach(() => {
  calls = [];
  responses = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: new URL(String(input), "http://localhost"), init: init ?? {} });
      const next = responses.shift();
      if (!next) throw new Error("unexpected fetch");
      return next();
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("video import api", () => {
  it("previews a URL with POST /api/video-import/preview", async () => {
    responses.push(json({ kind: "playlist", title: "P", entries: [], total_seconds: 0 }));
    const result = await previewImport({ url: "https://youtube.com/playlist?list=PL1" });
    expect(result.kind).toBe("playlist");
    expect(calls[0].url.pathname).toBe("/api/video-import/preview");
    expect(calls[0].init.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init.body))).toEqual({
      url: "https://youtube.com/playlist?list=PL1",
    });
    expect(calls[0].init.credentials).toBe("include");
  });

  it("surfaces the backend's structured 422 message", async () => {
    responses.push(json({ detail: { code: "private", message: "Playlist is private" } }, 422));
    await expect(previewImport({ folder: "x" })).rejects.toThrow("Playlist is private");
  });

  it("creates a job with selected video ids and kb name", async () => {
    responses.push(json({ job_id: "j1" }));
    await createImportJob({ folder: "videos/course", video_ids: ["a", "b"], kb_name: "kb" });
    expect(calls[0].url.pathname).toBe("/api/video-import/jobs");
    expect(calls[0].init.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init.body))).toEqual({
      folder: "videos/course",
      video_ids: ["a", "b"],
      kb_name: "kb",
    });
  });

  it("lists, reads and controls jobs", async () => {
    responses.push(json([]), json({ job_id: "j 1" }), json({ job_id: "j 1" }));
    await listImportJobs();
    await getImportJob("j 1");
    await controlImportJob("j 1", "pause");
    expect(calls[0].url.pathname).toBe("/api/video-import/jobs");
    expect(calls[0].init.method ?? "GET").toBe("GET");
    expect(calls[1].url.pathname).toBe("/api/video-import/jobs/j%201");
    expect(calls[2].url.pathname).toBe("/api/video-import/jobs/j%201/pause");
    expect(calls[2].init.method).toBe("POST");
  });

  it("starts a capture session", async () => {
    responses.push(json({ sample_seconds: 1, audio_timeslice_ms: 5000 }));
    const session = await startCapture("j1", "vid1");
    expect(session).toEqual({ sample_seconds: 1, audio_timeslice_ms: 5000 });
    expect(calls[0].url.pathname).toBe("/api/video-import/jobs/j1/videos/vid1/capture/start");
    expect(calls[0].init.method).toBe("POST");
  });

  it("uploads audio as raw audio/webm", async () => {
    responses.push(json({ bytes: 3 }));
    const chunk = new Blob([new Uint8Array([1, 2, 3])], { type: "audio/webm;codecs=opus" });
    await expect(uploadAudioChunk("j1", "vid1", chunk)).resolves.toEqual({ bytes: 3 });
    expect(calls[0].url.pathname).toBe("/api/video-import/jobs/j1/videos/vid1/capture/audio");
    expect(calls[0].init.method).toBe("POST");
    expect(new Headers(calls[0].init.headers).get("Content-Type")).toBe("audio/webm");
    expect(calls[0].init.body).toBe(chunk);
  });

  it("uploads a frame as image/jpeg with ?t=12.0", async () => {
    responses.push(json({ kept: true }));
    const frame = new Blob([new Uint8Array([9])], { type: "image/jpeg" });
    await expect(uploadFrame("j1", "vid1", 12, frame)).resolves.toEqual({ kept: true });
    expect(calls[0].url.pathname).toBe("/api/video-import/jobs/j1/videos/vid1/capture/frame");
    expect(calls[0].url.searchParams.get("t")).toBe("12.0");
    expect(calls[0].init.method).toBe("POST");
    expect(new Headers(calls[0].init.headers).get("Content-Type")).toBe("image/jpeg");
    expect(calls[0].init.body).toBe(frame);
  });

  it("retries a failed upload up to 2 times", async () => {
    responses.push(json({ detail: "boom" }, 503), json({ detail: "boom" }, 502), json({ kept: false }));
    const frame = new Blob([new Uint8Array([9])], { type: "image/jpeg" });
    await expect(uploadFrame("j1", "vid1", 1.25, frame, { retryDelayMs: 0 })).resolves.toEqual({
      kept: false,
    });
    expect(calls).toHaveLength(3);
  });

  it("gives up after 2 retries", async () => {
    responses.push(json({}, 503), json({}, 503), json({}, 503), json({ bytes: 1 }));
    const chunk = new Blob([new Uint8Array([1])]);
    await expect(uploadAudioChunk("j1", "vid1", chunk, { retryDelayMs: 0 })).rejects.toThrow();
    expect(calls).toHaveLength(3);
  });

  it("does not retry a non-retryable refusal (409 no capture)", async () => {
    responses.push(json({ detail: "No capture in progress for this video" }, 409));
    const chunk = new Blob([new Uint8Array([1])]);
    await expect(uploadAudioChunk("j1", "vid1", chunk, { retryDelayMs: 0 })).rejects.toThrow(
      "No capture in progress",
    );
    expect(calls).toHaveLength(1);
  });

  it("finishes a capture with the player outcome", async () => {
    responses.push(json({ job_id: "j1" }));
    await finishCapture("j1", "vid1", {
      ended: false,
      ad_seconds: 4.5,
      player_duration: 215,
      error_code: "embed_blocked",
    });
    expect(calls[0].url.pathname).toBe("/api/video-import/jobs/j1/videos/vid1/capture/finish");
    expect(calls[0].init.method).toBe("POST");
    expect(new Headers(calls[0].init.headers).get("Content-Type")).toBe("application/json");
    expect(JSON.parse(String(calls[0].init.body))).toEqual({
      ended: false,
      ad_seconds: 4.5,
      player_duration: 215,
      error_code: "embed_blocked",
    });
  });
});
