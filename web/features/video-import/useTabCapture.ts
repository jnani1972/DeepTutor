"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { AdDetector } from "./adHeuristic";
import { type CaptureSession, uploadAudioChunk, uploadFrame } from "./api";
import { FreezeDetector } from "./freezeDetector";

/** The subset of the YouTube player the capture loop reads. */
export interface CapturePlayer {
  getCurrentTime(): number;
  getDuration(): number;
  getPlayerState?(): number;
}

export interface CaptureStats {
  adSeconds: number;
  failedFrames: number;
}

export const NO_AUDIO_TRACK = "no_audio_track";

const AUDIO_MIME = "audio/webm;codecs=opus";
const AD_TICK_MS = 500;
const PLAYING = 1;
const FRAME_CHANGE_THRESHOLD = 0.004;
const SIGNATURE_W = 32;
const SIGNATURE_H = 18;

interface ActiveVideo {
  videoId: string;
  player: CapturePlayer;
  recorder: MediaRecorder | null;
  ads: AdDetector;
  freeze: FreezeDetector;
  inAd: boolean;
  audioChain: Promise<void>;
  audioFailed: boolean;
  frameBusy: boolean;
  failedFrames: number;
  lastSignature: Uint8Array | null;
  timers: number[];
}

export interface UseTabCaptureOptions {
  jobId: string;
  playerElement: HTMLElement | null;
  /** The owner stopped sharing from the browser (track `ended`). */
  onSharingEnded?: () => void;
  /** An audio chunk could not be uploaded even after retries. */
  onAudioFailure?: (error: Error) => void;
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

/**
 * Shares the current tab once (`getDisplayMedia` with tab audio), then for each
 * attached video records the tab audio (webm/opus, uploaded per timeslice) and
 * uploads a JPEG of the player's rect every sample period. While the
 * `AdDetector` reports an ad, the recorder is paused and frames are not sent.
 */
export function useTabCapture({
  jobId,
  playerElement,
  onSharingEnded,
  onAudioFailure,
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
  const activeRef = useRef<ActiveVideo | null>(null);
  const stoppingRef = useRef(false);
  const playerElementRef = useRef(playerElement);
  const callbacksRef = useRef({ onSharingEnded, onAudioFailure });

  useEffect(() => {
    playerElementRef.current = playerElement;
    callbacksRef.current = { onSharingEnded, onAudioFailure };
  });

  const releaseStream = useCallback(() => {
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    if (videoRef.current) {
      videoRef.current.srcObject = null;
      videoRef.current = null;
    }
    setSharing(false);
  }, []);

  const clearTimers = (active: ActiveVideo) => {
    active.timers.forEach((timer) => window.clearInterval(timer));
    active.timers = [];
  };

  const stop = useCallback(() => {
    stoppingRef.current = true;
    const active = activeRef.current;
    if (active) {
      clearTimers(active);
      if (active.recorder && active.recorder.state !== "inactive") active.recorder.stop();
    }
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

  const grabFrame = useCallback(
    (active: ActiveVideo) => {
      const video = videoRef.current;
      const element = playerElementRef.current;
      if (!video || !element || !video.videoWidth || active.frameBusy) return;
      const state = active.player.getPlayerState?.();
      if (active.inAd || state !== PLAYING) return;

      const rect = element.getBoundingClientRect();
      const scale = video.videoWidth / Math.max(1, window.innerWidth);
      const canvas = (canvasRef.current ??= document.createElement("canvas"));
      const scratch = (scratchRef.current ??= document.createElement("canvas"));
      canvas.width = Math.max(1, Math.round(rect.width * scale));
      canvas.height = Math.max(1, Math.round(rect.height * scale));
      const context = canvas.getContext("2d");
      if (!context) return;
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
      const t = Number(active.player.getCurrentTime()) || 0;
      const signature = frameSignature(canvas, scratch);
      const changed =
        !active.lastSignature ||
        signatureDistance(signature, active.lastSignature) > FRAME_CHANGE_THRESHOLD;
      active.lastSignature = signature;
      setFrozen(
        active.freeze.update({
          hidden: document.visibilityState === "hidden",
          frameChanged: changed,
          currentTime: t,
        }),
      );

      active.frameBusy = true;
      canvas.toBlob(
        (blob) => {
          if (!blob || activeRef.current !== active) {
            active.frameBusy = false;
            return;
          }
          uploadFrame(jobId, active.videoId, t, blob)
            .catch(() => {
              active.failedFrames += 1;
              setFailedFrames(active.failedFrames);
            })
            .finally(() => {
              active.frameBusy = false;
            });
        },
        "image/jpeg",
        0.8,
      );
    },
    [jobId],
  );

  /** Begin recording one video. `capture/start` must already have succeeded. */
  const attach = useCallback(
    (videoId: string, player: CapturePlayer, session: CaptureSession) => {
      const stream = streamRef.current;
      if (!stream || activeRef.current) return;
      const active: ActiveVideo = {
        videoId,
        player,
        recorder: null,
        ads: new AdDetector(),
        freeze: new FreezeDetector(),
        inAd: false,
        audioChain: Promise.resolve(),
        audioFailed: false,
        frameBusy: false,
        failedFrames: 0,
        lastSignature: null,
        timers: [],
      };
      activeRef.current = active;
      setFrozen(false);
      setInAd(false);
      setFailedFrames(0);

      // A fresh recorder per video so each upload stream starts with a webm header.
      const recorder = new MediaRecorder(new MediaStream(stream.getAudioTracks()), {
        mimeType: AUDIO_MIME,
      });
      recorder.ondataavailable = (event) => {
        if (!event.data.size) return;
        const chunk = event.data;
        active.audioChain = active.audioChain.then(async () => {
          if (active.audioFailed) return;
          try {
            await uploadAudioChunk(jobId, videoId, chunk);
          } catch (caught) {
            active.audioFailed = true;
            callbacksRef.current.onAudioFailure?.(
              caught instanceof Error ? caught : new Error(String(caught)),
            );
          }
        });
      };
      recorder.start(session.audio_timeslice_ms || 5000);
      active.recorder = recorder;

      active.timers.push(
        window.setInterval(() => {
          const verdict = active.ads.update({
            wallMs: performance.now(),
            state: Number(player.getPlayerState?.() ?? -1),
            currentTime: Number(player.getCurrentTime()) || 0,
          });
          const ad = verdict === "ad";
          if (ad !== active.inAd) {
            active.inAd = ad;
            setInAd(ad);
          }
          if (ad && recorder.state === "recording") recorder.pause();
          else if (!ad && recorder.state === "paused") recorder.resume();
        }, AD_TICK_MS),
        window.setInterval(
          () => grabFrame(active),
          Math.max(250, (session.sample_seconds || 1) * 1000),
        ),
      );
    },
    [grabFrame, jobId],
  );

  /** Stop recording the current video and wait until every audio chunk is uploaded. */
  const detach = useCallback(async (): Promise<CaptureStats & { audioFailed: boolean }> => {
    const active = activeRef.current;
    if (!active) return { adSeconds: 0, failedFrames: 0, audioFailed: false };
    clearTimers(active);
    const recorder = active.recorder;
    if (recorder && recorder.state !== "inactive") {
      await new Promise<void>((resolve) => {
        recorder.addEventListener("stop", () => resolve(), { once: true });
        recorder.stop();
      });
    }
    await active.audioChain;
    activeRef.current = null;
    setInAd(false);
    setFrozen(false);
    return {
      adSeconds: Math.round(active.ads.adSeconds * 10) / 10,
      failedFrames: active.failedFrames,
      audioFailed: active.audioFailed,
    };
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
