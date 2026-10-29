import { describe, expect, it, vi } from "vitest";

import { finishPlan } from "./captureFlow";
import {
  type CaptureSessionDeps,
  type GrabbedFrame,
  type RecorderLike,
  FRAME_FAILURE_LIMIT,
  VideoCaptureSession,
} from "./captureSession";

const PLAYING = 1;

class MockRecorder implements RecorderLike {
  state: RecordingState = "inactive";
  started = 0;
  constructor(private readonly onData: (blob: Blob) => void) {}
  start() {
    this.state = "recording";
    this.started += 1;
  }
  pause() {
    this.state = "paused";
  }
  resume() {
    this.state = "recording";
  }
  emit(text: string) {
    this.onData(new Blob([text]));
  }
  async stop() {
    // MediaRecorder flushes one last chunk on stop.
    this.emit("final");
    this.state = "inactive";
  }
}

function harness(overrides: Partial<CaptureSessionDeps> = {}) {
  let wall = 0;
  let time = 0;
  let state = PLAYING;
  const recorders: MockRecorder[] = [];
  const deps: CaptureSessionDeps = {
    player: {
      getCurrentTime: () => time,
      getDuration: () => 100,
      getPlayerState: () => state,
    },
    timesliceMs: 5000,
    createRecorder: (onData) => {
      const recorder = new MockRecorder(onData);
      recorders.push(recorder);
      return recorder;
    },
    uploadAudio: vi.fn(async () => ({ bytes: 1 })),
    uploadFrame: vi.fn(async () => ({ kept: true })),
    grabFrame: vi.fn(
      async (): Promise<GrabbedFrame> => ({ t: time, blob: new Blob(["f"]), changed: true }),
    ),
    isHidden: () => false,
    now: () => wall,
    ...overrides,
  };
  const session = new VideoCaptureSession(deps);
  return {
    session,
    deps,
    recorders,
    advance(ms: number, seconds: number) {
      wall += ms;
      time += seconds;
    },
    setState(next: number) {
      state = next;
    },
  };
}

/** Play past the pre-roll gate so the recorder starts. */
function openGate(h: ReturnType<typeof harness>) {
  h.session.tickAd();
  h.advance(500, 0.5);
  h.session.tickAd();
}

describe("VideoCaptureSession", () => {
  it("does not record or send frames during a pre-roll (clock held at 0)", async () => {
    const h = harness();
    for (let i = 0; i < 6; i += 1) {
      h.session.tickAd();
      await h.session.tickFrame();
      h.advance(500, 0);
    }
    expect(h.recorders).toHaveLength(0);
    expect(h.deps.grabFrame).not.toHaveBeenCalled();
    openGate(h);
    expect(h.recorders).toHaveLength(1);
    expect(h.recorders[0].started).toBe(1);
  });

  it("reports a final-flush audio failure so finish is never sent as complete", async () => {
    const uploadAudio = vi.fn(async (chunk: Blob) => {
      if ((await chunk.text()) === "final") throw new Error("upload failed");
      return { bytes: chunk.size };
    });
    const onAudioFailure = vi.fn();
    const h = harness({ uploadAudio, onAudioFailure });
    openGate(h);
    h.recorders[0].emit("chunk-1");

    const result = await h.session.detach();

    expect(uploadAudio).toHaveBeenCalledTimes(2);
    expect(result.audioFailed).toBe(true);
    expect(onAudioFailure).toHaveBeenCalledTimes(1);
    const plan = finishPlan({ kind: "ended" }, result, 100);
    expect(plan.kind).toBe("retry");
    expect(plan).not.toHaveProperty("body");
  });

  it("finishes as ended when every upload succeeded", async () => {
    const h = harness();
    openGate(h);
    const result = await h.session.detach();
    expect(result.audioFailed).toBe(false);
    expect(finishPlan({ kind: "ended" }, result, 100)).toEqual({
      kind: "finish",
      body: { ended: true, ad_seconds: 0, player_duration: 100 },
    });
  });

  it("tolerates a few failed frames but fails the video at the limit", async () => {
    const onFrameFailureLimit = vi.fn();
    const h = harness({
      uploadFrame: vi.fn(async () => {
        throw new Error("frame failed");
      }),
      onFrameFailureLimit,
    });
    openGate(h);
    for (let i = 0; i < FRAME_FAILURE_LIMIT - 1; i += 1) {
      await h.session.tickFrame();
      h.advance(1000, 1);
    }
    const tolerated = { adSeconds: 0, audioFailed: false, failedFrames: FRAME_FAILURE_LIMIT - 1 };
    expect(finishPlan({ kind: "ended" }, tolerated, 100).kind).toBe("finish");

    await h.session.tickFrame();
    const result = await h.session.detach();
    expect(result.failedFrames).toBe(FRAME_FAILURE_LIMIT);
    expect(onFrameFailureLimit).toHaveBeenCalledTimes(1);
    expect(finishPlan({ kind: "ended" }, result, 100).kind).toBe("retry");
  });

  it("waits for in-flight frame uploads before detach resolves", async () => {
    let release: () => void = () => {};
    const uploadFrame = vi.fn(
      () => new Promise<{ kept: boolean }>((resolve) => (release = () => resolve({ kept: true }))),
    );
    const h = harness({ uploadFrame });
    openGate(h);
    await h.session.tickFrame();
    let detached = false;
    const pending = h.session.detach().then(() => {
      detached = true;
    });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(uploadFrame).toHaveBeenCalledTimes(1);
    expect(detached).toBe(false);
    release();
    await pending;
    expect(detached).toBe(true);
  });

  it("drops frames grabbed inside the ad's stall window", async () => {
    const h = harness();
    openGate(h); // wall 500, t 0.5
    await h.session.tickFrame(); // frame at wall 500 (before the stall)
    h.advance(1000, 1);
    h.session.tickAd(); // wall 1500, clock advanced → stall anchor 1500
    await h.session.tickFrame(); // frame at wall 1500 (stall)
    h.advance(1000, 0);
    await h.session.tickFrame(); // frame at wall 2500 (stall)
    h.advance(1000, 0);
    h.session.tickAd(); // wall 3500: stalled 2 s → ad, drop frames since 1500
    expect(h.session.inAd).toBe(true);
    await h.session.detach();
    expect(h.deps.uploadFrame).toHaveBeenCalledTimes(1);
    expect(vi.mocked(h.deps.uploadFrame).mock.calls[0][0]).toBeCloseTo(0.5);
  });

  it("pauses the recorder whenever the player is not playing", () => {
    const h = harness();
    openGate(h);
    const recorder = h.recorders[0];
    expect(recorder.state).toBe("recording");

    for (const state of [2 /* paused */, 3 /* buffering */]) {
      h.setState(state);
      h.advance(500, 0);
      h.session.tickAd();
      expect(recorder.state).toBe("paused");
      expect(h.session.inAd).toBe(false);

      h.setState(PLAYING);
      h.advance(500, 0.5);
      h.session.tickAd();
      expect(recorder.state).toBe("recording");
    }
  });
});
