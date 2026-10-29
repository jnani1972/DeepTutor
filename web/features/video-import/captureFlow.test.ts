import { describe, expect, it } from "vitest";

import type { ImportJob, ImportVideo } from "./api";
import { formatDuration, nextCaptureVideo, playerErrorCode } from "./captureFlow";

function video(video_id: string, position: number, stage: ImportVideo["stage"]): ImportVideo {
  return {
    video_id,
    title: video_id,
    position,
    duration_seconds: 60,
    stage,
    error_code: "",
    error_detail: "",
    speech_cues: 0,
    frame_notes: 0,
  };
}

function job(videos: ImportVideo[]): ImportJob {
  return {
    job_id: "j1",
    source_url: "",
    target: { kind: "playlist", playlist_id: "PL", video_id: null, folder: null },
    title: "t",
    kb_name: "kb",
    status: "waiting_for_playback",
    capture_mode: "browser",
    videos,
    created_at: "",
    updated_at: "",
    error_code: "",
    error_detail: "",
  };
}

describe("nextCaptureVideo", () => {
  it("picks the first queued or interrupted video by position", () => {
    const next = nextCaptureVideo(
      job([video("c", 3, "queued"), video("a", 1, "done"), video("b", 2, "capturing")]),
    );
    expect(next?.video_id).toBe("b");
  });

  it("returns null when nothing is left to capture", () => {
    expect(
      nextCaptureVideo(job([video("a", 1, "captured"), video("b", 2, "skipped")])),
    ).toBeNull();
  });
});

describe("playerErrorCode", () => {
  it("maps YouTube player errors to backend codes", () => {
    expect(playerErrorCode(2)).toBe("unplayable");
    expect(playerErrorCode(5)).toBe("unplayable");
    expect(playerErrorCode(100)).toBe("removed");
    expect(playerErrorCode(101)).toBe("embed_blocked");
    expect(playerErrorCode(150)).toBe("embed_blocked");
  });

  it("passes unknown codes through as numeric strings", () => {
    expect(playerErrorCode(999)).toBe("999");
  });
});

describe("formatDuration", () => {
  it("formats seconds as m:ss or h:mm:ss", () => {
    expect(formatDuration(215)).toBe("3:35");
    expect(formatDuration(3725)).toBe("1:02:05");
    expect(formatDuration(0)).toBe("0:00");
  });
});
