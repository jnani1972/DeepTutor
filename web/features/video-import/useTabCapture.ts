"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { type CaptureSession, uploadAudioChunk, uploadFrame } from "./api";
import type { CaptureResult } from "./captureFlow";
import {
  type CapturePlayer,
  type GrabbedFrame,
  type RecorderLike,
  VideoCaptureSession,
} from "./captureSession";

export type { CapturePlayer } from "./captureSession";

export const NO_AUDIO_TRACK = "no_audio_track";

const AUDIO_MIME = "audio/webm;codecs=opus";
const AD_TICK_MS = 500;
const FRAME_CHANGE_THRESHOLD = 0.004;
const SIGNATURE_W = 32;
const SIGNATURE_H = 18;

export interface UseTabCaptureOptions {
  jobId: string;
  playerElement: HTMLElement | null;
  /** The owner stopped sharing from the browser (track `ended`). */
  onSharingEnded?: () => void;
  /** An audio chunk could not be uploaded even after retries. */
  onAudioFailure?: (error: Error) => void;
  /** The current video reached the failed-frame-upload limit. */
  onFrameFailureLimit?: (failedFrames: number) => void;
}

function frameSignature(source: HTMLCanvasElement, scratch: HTMLCanvasElement): Uint8Array {
  scratch.width = SIGNATURE_W;
  scratch.height = SIGNATURE_H;
  const context = scratch.getContext("2d");
  const out = new Uint8Array(SIGNATURE_W * SIGNATURE_H);
  if (!context) return out;
  context.drawImage(source, 0, 0, SIGNATURE_W, SIGNATURE_H);
  const data = context.getImageData(0, 0, SIGNATURE_W, SIGNATURE_H).data;
  for (let i = 0; i < out.length; i += 1) {
    out[i] = (data[i * 4] + data[i * 4 + 1] + data[i * 4 + 2]) / 3;
  }
  return out;
}

function signatureDistance(a: Uint8Array, b: Uint8Array): number {
  let total = 0;
  for (let i = 0; i < a.length; i += 1) total += Math.abs(a[i] - b[i]);
  return total / a.length / 255;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** Adapt a MediaRecorder so `stop()` resolves after its final chunk was delivered. */
function mediaRecorder(stream: MediaStream, onData: (chunk: Blob) => void): RecorderLike {
  const recorder = new MediaRecorder(new MediaStream(stream.getAudioTracks()), {
    mimeType: AUDIO_MIME,
  });
  recorder.ondataavailable = (event) => onData(event.data);
  return {
    get state() {
      return recorder.state;
    },
    start: (timesliceMs) => recorder.start(timesliceMs),
    pause: () => recorder.pause(),
    resume: () => recorder.resume(),
    stop: () =>
      new Promise<void>((resolve) => {
        if (recorder.state === "inactive") {
          resolve();
          return;
        }
        recorder.addEventListener("stop", () => resolve(), { once: true });
        recorder.stop();
      }),
  };
}

/**
 * Shares the current tab once (`getDisplayMedia` with tab audio). For each
 * attached video a `VideoCaptureSession` records the tab audio (webm/opus,
 * uploaded per timeslice) and samples a JPEG of the player's rect every sample
 * period, with pre-roll and ad gating (see captureSession.ts).
 */
export function useTabCapture({
  jobId,
  playerElement,
  onSharingEnded,
  onAudioFailure,
  onFrameFailureLimit,
}: UseTabCaptureOptions) {
  const [sharing, setSharing] = useState(false);
  const [audioTrack, setAudioTrack] = useState(false);
  const [error, setError] = useState<string | undefined>();
  const [frozen, setFrozen] = useState(false);
  const [inAd, setInAd] = useState(false);
  const [failedFrames, setFailedFrames] = useState(0);

  const streamRef = useRef<MediaStream | null>(null);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const scratchRef = useRef<HTMLCanvasElement | null>(null);
  const sessionRef = useRef<VideoCaptureSession | null>(null);
  const timersRef = useRef<number[]>([]);
  const stoppingRef = useRef(false);
  const playerElementRef = useRef(playerElement);
  const callbacksRef = useRef({ onSharingEnded, onAudioFailure, onFrameFailureLimit });

  useEffect(() => {
    playerElementRef.current = playerElement;
    callbacksRef.current = { onSharingEnded, onAudioFailure, onFrameFailureLimit };
  });

  const clearTimers = () => {
    timersRef.current.forEach((timer) => window.clearInterval(timer));
    timersRef.current = [];
  };

  const releaseStream = useCallback(() => {
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    if (videoRef.current) {
      videoRef.current.srcObject = null;
      videoRef.current = null;
    }
    setSharing(false);
  }, []);

  const stop = useCallback(() => {
    stoppingRef.current = true;
    clearTimers();
    releaseStream();
  }, [releaseStream]);

  const start = useCallback(async () => {
    setError(undefined);
    if (streamRef.current) return;
    let stream: MediaStream;
    try {
      // Chrome-specific display-media hints are not in the DOM typings yet.
      const options = {
        video: { displaySurface: "browser" },
        audio: true,
        preferCurrentTab: true,
        selfBrowserSurface: "include",
        systemAudio: "exclude",
      } as DisplayMediaStreamOptions;
      stream = await navigator.mediaDevices.getDisplayMedia(options);
    } catch (caught) {
      setError(errorMessage(caught));
      return;
    }
    const hasAudio = stream.getAudioTracks().length > 0;
    setAudioTrack(hasAudio);
    if (!hasAudio) {
      stream.getTracks().forEach((track) => track.stop());
      setError(NO_AUDIO_TRACK);
      return;
    }
    if (typeof MediaRecorder === "undefined" || !MediaRecorder.isTypeSupported(AUDIO_MIME)) {
      stream.getTracks().forEach((track) => track.stop());
      setError(`This browser cannot record ${AUDIO_MIME}.`);
      return;
    }
    stoppingRef.current = false;
    streamRef.current = stream;
    const video = document.createElement("video");
    video.muted = true; // the preview only; the YouTube player stays audible
    video.playsInline = true;
    video.srcObject = stream;
    videoRef.current = video;
    try {
      await video.play();
    } catch (caught) {
      releaseStream();
      setError(errorMessage(caught));
      return;
    }
    stream.getVideoTracks()[0]?.addEventListener("ended", () => {
      if (stoppingRef.current) return;
      releaseStream();
      callbacksRef.current.onSharingEnded?.();
    });
    setSharing(true);
  }, [releaseStream]);

  /** Crop the shared tab to the player element and encode a JPEG (quality 0.8). */
  const grabFrame = useCallback(
    (player: CapturePlayer, last: { signature: Uint8Array | null }) =>
      async (): Promise<GrabbedFrame | null> => {
        const video = videoRef.current;
        const element = playerElementRef.current;
        if (!video || !element || !video.videoWidth) return null;
        const rect = element.getBoundingClientRect();
        const scale = video.videoWidth / Math.max(1, window.innerWidth);
        const canvas = (canvasRef.current ??= document.createElement("canvas"));
        const scratch = (scratchRef.current ??= document.createElement("canvas"));
        canvas.width = Math.max(1, Math.round(rect.width * scale));
        canvas.height = Math.max(1, Math.round(rect.height * scale));
        const context = canvas.getContext("2d");
        if (!context) return null;
        context.drawImage(
          video,
          rect.left * scale,
          rect.top * scale,
          canvas.width,
          canvas.height,
          0,
          0,
          canvas.width,
          canvas.height,
        );
        const t = Number(player.getCurrentTime()) || 0;
        const signature = frameSignature(canvas, scratch);
        const changed =
          !last.signature ||
          signatureDistance(signature, last.signature) > FRAME_CHANGE_THRESHOLD;
        last.signature = signature;
        const blob = await new Promise<Blob | null>((resolve) =>
          canvas.toBlob(resolve, "image/jpeg", 0.8),
        );
        return blob ? { t, blob, changed } : null;
      },
    [],
  );

  /** Begin capturing one video. `capture/start` must already have succeeded. */
  const attach = useCallback(
    (videoId: string, player: CapturePlayer, capture: CaptureSession) => {
      const stream = streamRef.current;
      if (!stream || sessionRef.current) return;
      setFrozen(false);
      setInAd(false);
      setFailedFrames(0);
      const session = new VideoCaptureSession({
        player,
        // A fresh recorder per video so each upload stream starts with a webm header.
        timesliceMs: capture.audio_timeslice_ms || 5000,
        createRecorder: (onData) => mediaRecorder(stream, onData),
        uploadAudio: (chunk) => uploadAudioChunk(jobId, videoId, chunk),
        uploadFrame: (t, blob) => uploadFrame(jobId, videoId, t, blob),
        grabFrame: grabFrame(player, { signature: null }),
        isHidden: () => document.visibilityState === "hidden",
        now: () => performance.now(),
        onAudioFailure: (failure) => callbacksRef.current.onAudioFailure?.(failure),
        onFrameFailureLimit: (count) => callbacksRef.current.onFrameFailureLimit?.(count),
        onStatus: (status) => {
          setInAd(status.inAd);
          setFrozen(status.frozen);
          setFailedFrames(status.failedFrames);
        },
      });
      sessionRef.current = session;
      timersRef.current.push(
        window.setInterval(() => session.tickAd(), AD_TICK_MS),
        window.setInterval(
          () => void session.tickFrame(),
          Math.max(250, (capture.sample_seconds || 1) * 1000),
        ),
      );
    },
    [grabFrame, jobId],
  );

  /** Stop the current video and wait until its final audio chunk and every frame are uploaded. */
  const detach = useCallback(async (): Promise<CaptureResult> => {
    clearTimers();
    const session = sessionRef.current;
    sessionRef.current = null;
    setInAd(false);
    setFrozen(false);
    if (!session) return { adSeconds: 0, audioFailed: false, failedFrames: 0 };
    return session.detach();
  }, []);

  const isSharing = useCallback(() => streamRef.current !== null, []);

  useEffect(() => () => stop(), [stop]);

  return {
    start,
    stop,
    sharing,
    audioTrack,
    error,
    frozen,
    inAd,
    failedFrames,
    attach,
    detach,
    isSharing,
  };
}
