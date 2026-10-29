import { AdDetector } from "./adHeuristic";
import { FrameLookback, PrerollGate } from "./captureGates";
import { type CaptureResult, FRAME_FAILURE_LIMIT } from "./captureFlow";
import { FreezeDetector } from "./freezeDetector";

export { FRAME_FAILURE_LIMIT };

const PLAYING = 1;

/** The subset of the YouTube player the capture loop reads. */
export interface CapturePlayer {
  getCurrentTime(): number;
  getDuration(): number;
  getPlayerState?(): number;
}

/** A MediaRecorder reduced to what the session drives; `stop` resolves after the final chunk. */
export interface RecorderLike {
  readonly state: RecordingState;
  start(timesliceMs: number): void;
  pause(): void;
  resume(): void;
  stop(): Promise<void>;
}

export interface GrabbedFrame {
  t: number;
  blob: Blob;
  changed: boolean;
}

export interface CaptureStatus {
  inAd: boolean;
  frozen: boolean;
  failedFrames: number;
}

export interface CaptureSessionDeps {
  player: CapturePlayer;
  timesliceMs: number;
  createRecorder(onData: (chunk: Blob) => void): RecorderLike;
  uploadAudio(chunk: Blob): Promise<unknown>;
  uploadFrame(t: number, blob: Blob): Promise<unknown>;
  /** Crop the shared tab to the player; null when no frame is available. */
  grabFrame(): Promise<GrabbedFrame | null>;
  isHidden(): boolean;
  now(): number;
  onAudioFailure?(error: Error): void;
  onFrameFailureLimit?(failedFrames: number): void;
  onStatus?(status: CaptureStatus): void;
}

/**
 * One video's capture: audio recorder, frame sampling and upload bookkeeping.
 *
 * - Nothing is recorded or grabbed until `PrerollGate` opens (pre-roll ads).
 * - While `AdDetector` says "ad" the recorder is paused and no frames are
 *   grabbed; frames already grabbed inside the ad's stall window are dropped
 *   from the 2.5 s `FrameLookback` buffer before they are uploaded.
 * - The recorder also pauses whenever the player is not PLAYING (owner pause,
 *   buffering), so audio-file time keeps tracking player time; speech
 *   timestamps are audio-file time while frame timestamps are player time.
 * - `detach()` stops the recorder, uploads its final chunk and every buffered
 *   frame, and waits for all uploads, so its result is final.
 */
export class VideoCaptureSession {
  inAd = false;
  frozen = false;
  failedFrames = 0;
  audioFailed = false;

  private readonly ads = new AdDetector();
  private readonly freeze = new FreezeDetector();
  private readonly gate = new PrerollGate();
  private readonly lookback = new FrameLookback<{ t: number; blob: Blob }>();
  private recorder: RecorderLike | null = null;
  private audioChain: Promise<void> = Promise.resolve();
  private frameChain: Promise<void> = Promise.resolve();
  private readonly grabs = new Set<Promise<void>>();
  private closed = false;
  private limitReported = false;

  constructor(private readonly deps: CaptureSessionDeps) {}

  private sample() {
    const { player } = this.deps;
    return {
      state: Number(player.getPlayerState?.() ?? -1),
      currentTime: Number(player.getCurrentTime()) || 0,
    };
  }

  private emitStatus() {
    this.deps.onStatus?.({
      inAd: this.inAd,
      frozen: this.frozen,
      failedFrames: this.failedFrames,
    });
  }

  private startRecorder() {
    const recorder = this.deps.createRecorder((chunk) => this.enqueueAudio(chunk));
    recorder.start(this.deps.timesliceMs);
    this.recorder = recorder;
  }

  private enqueueAudio(chunk: Blob) {
    if (!chunk.size) return;
    this.audioChain = this.audioChain.then(async () => {
      if (this.audioFailed) return;
      try {
        await this.deps.uploadAudio(chunk);
      } catch (caught) {
        this.audioFailed = true;
        this.deps.onAudioFailure?.(caught instanceof Error ? caught : new Error(String(caught)));
      }
    });
  }

  private enqueueFrame(frame: { t: number; blob: Blob }) {
    this.frameChain = this.frameChain.then(async () => {
      try {
        await this.deps.uploadFrame(frame.t, frame.blob);
      } catch {
        this.failedFrames += 1;
        this.emitStatus();
        if (this.failedFrames >= FRAME_FAILURE_LIMIT && !this.limitReported) {
          this.limitReported = true;
          this.deps.onFrameFailureLimit?.(this.failedFrames);
        }
      }
    });
  }

  /** Ad / pre-roll tick (every ~500 ms). */
  tickAd(): void {
    if (this.closed) return;
    const sample = this.sample();
    const verdict = this.ads.update({ wallMs: this.deps.now(), ...sample });
    const ad = verdict === "ad";
    if (ad && !this.inAd && this.ads.adStartedAtMs !== null) {
      this.lookback.dropSince(this.ads.adStartedAtMs);
    }
    if (ad !== this.inAd) {
      this.inAd = ad;
      this.emitStatus();
    }
    if (!this.gate.open) {
      if (this.gate.update(sample)) this.startRecorder();
      return;
    }
    const recorder = this.recorder;
    if (!recorder) return;
    const record = !ad && sample.state === PLAYING;
    if (!record && recorder.state === "recording") recorder.pause();
    else if (record && recorder.state === "paused") recorder.resume();
  }

  /** Frame tick (every sample period): release held frames, grab a new one. */
  async tickFrame(): Promise<void> {
    if (this.closed) return;
    const wallMs = this.deps.now();
    for (const frame of this.lookback.release(wallMs)) this.enqueueFrame(frame);
    const sample = this.sample();
    if (!this.gate.open || this.inAd || sample.state !== PLAYING) return;
    const grab = this.deps
      .grabFrame()
      .then((frame) => {
        if (!frame) return;
        const frozen = this.freeze.update({
          hidden: this.deps.isHidden(),
          frameChanged: frame.changed,
          currentTime: frame.t,
        });
        if (frozen !== this.frozen) {
          this.frozen = frozen;
          this.emitStatus();
        }
        this.lookback.push(wallMs, { t: frame.t, blob: frame.blob });
      })
      .catch(() => {
        /* a frame that cannot be encoded is simply not sampled */
      });
    this.grabs.add(grab);
    await grab;
    this.grabs.delete(grab);
  }

  /** Stop, flush every upload and return the final result for `finishPlan`. */
  async detach(): Promise<CaptureResult> {
    this.closed = true;
    if (this.recorder && this.recorder.state !== "inactive") await this.recorder.stop();
    await Promise.allSettled([...this.grabs]);
    for (const frame of this.lookback.drain()) this.enqueueFrame(frame);
    await this.frameChain;
    await this.audioChain;
    return {
      adSeconds: Math.round(this.ads.adSeconds * 10) / 10,
      audioFailed: this.audioFailed,
      failedFrames: this.failedFrames,
    };
  }
}
