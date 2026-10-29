"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { AlertTriangle, Loader2, MonitorUp, Pause, Play, RotateCcw } from "lucide-react";

import { loadYouTubeApi } from "@/lib/youtube-iframe-api";
import type { YouTubePlayerLike } from "@/lib/video-player-controller";

import {
  type CaptureSession,
  type FinishBody,
  type ImportJob,
  type ImportVideo,
  controlImportJob,
  finishCapture,
  getImportJob,
  startCapture,
} from "./api";
import {
  TERMINAL_JOB_STATUSES,
  formatDuration,
  nextCaptureVideo,
  playerErrorCode,
} from "./captureFlow";
import { NO_AUDIO_TRACK, useTabCapture } from "./useTabCapture";

const POLL_MS = 5000;
const YOUTUBE_HOST = "https://www.youtube-nocookie.com";

type Phase = "idle" | "running" | "sharing_stopped" | "paused" | "processing" | "failure";

type Outcome =
  | { kind: "ended" }
  | { kind: "error"; code: string }
  | { kind: "stopped" }
  | { kind: "capture_failed"; message: string };

interface Failure {
  step: "start" | "capture" | "finish" | "poll";
  video?: ImportVideo;
  body?: FinishBody;
  message: string;
}

type PlayerHandle = YouTubePlayerLike & {
  getPlayerState?(): number;
  unMute?(): void;
  setVolume?(volume: number): void;
};

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export default function DeepImportCapture({ jobId }: { jobId: string }) {
  const { t } = useTranslation();
  const [job, setJob] = useState<ImportJob | null>(null);
  const [loadError, setLoadError] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [current, setCurrent] = useState<ImportVideo | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [pauseRequested, setPauseRequested] = useState(false);
  const [playerElement, setPlayerElement] = useState<HTMLDivElement | null>(null);

  const runningRef = useRef(false);
  const pauseRequestedRef = useRef(false);
  const outcomeRef = useRef<((outcome: Outcome) => void) | null>(null);
  const playerRef = useRef<PlayerHandle | null>(null);

  const settle = useCallback((outcome: Outcome) => {
    const resolve = outcomeRef.current;
    outcomeRef.current = null;
    resolve?.(outcome);
  }, []);

  const capture = useTabCapture({
    jobId,
    playerElement,
    onSharingEnded: () => {
      settle({ kind: "stopped" });
      if (!runningRef.current) setPhase("sharing_stopped");
    },
    onAudioFailure: (error) => settle({ kind: "capture_failed", message: error.message }),
  });
  const captureRef = useRef(capture);
  useEffect(() => {
    captureRef.current = capture;
  });

  const refresh = useCallback(async () => {
    try {
      const next = await getImportJob(jobId);
      setJob(next);
      setLoadError("");
      return next;
    } catch (error) {
      setLoadError(messageOf(error));
      return null;
    }
  }, [jobId]);

  // Initial load, then status polling every 5 s until the job settles.
  useEffect(() => {
    void refresh().then((loaded) => {
      if (loaded?.status === "paused") setPhase("paused");
    });
  }, [refresh]);
  const terminal = job ? TERMINAL_JOB_STATUSES.has(job.status) : false;
  useEffect(() => {
    if (terminal) return;
    const timer = window.setInterval(() => {
      if (!runningRef.current) void refresh();
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [refresh, terminal]);

  const destroyPlayer = useCallback(() => {
    const player = playerRef.current;
    playerRef.current = null;
    try {
      player?.pauseVideo();
      player?.destroy();
    } catch {
      /* the iframe may already be gone */
    }
    playerElement?.replaceChildren();
  }, [playerElement]);

  const playOne = useCallback(
    (video: ImportVideo, session: CaptureSession) =>
      new Promise<Outcome>((resolve) => {
        outcomeRef.current = resolve;
        // Sharing may have stopped while capture/start was in flight.
        if (!captureRef.current.isSharing()) {
          settle({ kind: "stopped" });
          return;
        }
        loadYouTubeApi()
          .then((YT) => {
            if (!outcomeRef.current || !playerElement) {
              settle({ kind: "capture_failed", message: "Player container is missing." });
              return;
            }
            const mount = document.createElement("div");
            mount.style.width = "100%";
            mount.style.height = "100%";
            playerElement.replaceChildren(mount);
            new YT.Player(mount, {
              videoId: video.video_id,
              host: YOUTUBE_HOST,
              width: "100%",
              height: "100%",
              playerVars: { origin: window.location.origin, playsinline: 1, rel: 0 },
              events: {
                onReady: (event) => {
                  if (!outcomeRef.current) return;
                  const player = event.target as PlayerHandle;
                  playerRef.current = player;
                  if (!captureRef.current.isSharing()) {
                    settle({ kind: "stopped" });
                    return;
                  }
                  // Keep the owner's own playback audible.
                  player.unMute?.();
                  player.setVolume?.(100);
                  captureRef.current.attach(video.video_id, player, session);
                  player.playVideo();
                },
                onStateChange: (event) => {
                  if (event.data === 0) settle({ kind: "ended" });
                },
                onError: (event) => settle({ kind: "error", code: playerErrorCode(event.data) }),
              },
            });
          })
          .catch((error) => settle({ kind: "capture_failed", message: messageOf(error) }));
      }),
    [playerElement, settle],
  );

  /** POST capture/finish; on failure keep the body so the owner can retry it. */
  const sendFinish = useCallback(
    async (video: ImportVideo, body: FinishBody) => {
      try {
        setJob(await finishCapture(jobId, video.video_id, body));
        return true;
      } catch (error) {
        setFailure({ step: "finish", video, body, message: messageOf(error) });
        setPhase("failure");
        return false;
      }
    },
    [jobId],
  );

  /** Capture one video end to end. Returns whether the loop may continue. */
  const runVideo = useCallback(
    async (video: ImportVideo) => {
      setCurrent(video);
      let session: CaptureSession;
      try {
        session = await startCapture(jobId, video.video_id);
      } catch (error) {
        setFailure({ step: "start", video, message: messageOf(error) });
        setPhase("failure");
        return false;
      }
      const outcome = await playOne(video, session);
      // Fall back to the listed duration: with player_duration 0 the server would
      // accept an interrupted capture as complete.
      const duration = Number(playerRef.current?.getDuration()) || video.duration_seconds;
      const stats = await captureRef.current.detach();
      destroyPlayer();
      if (outcome.kind === "capture_failed") {
        setFailure({ step: "capture", video, message: outcome.message });
        setPhase("failure");
        return false;
      }
      const body: FinishBody = {
        ended: outcome.kind === "ended",
        ad_seconds: stats.adSeconds,
        player_duration: duration,
        ...(outcome.kind === "error" ? { error_code: outcome.code } : {}),
      };
      if (!(await sendFinish(video, body))) return false;
      if (outcome.kind === "stopped") {
        setPhase("sharing_stopped");
        return false;
      }
      return true;
    },
    [destroyPlayer, jobId, playOne, sendFinish],
  );

  const runLoop = useCallback(async () => {
    if (runningRef.current) return;
    runningRef.current = true;
    setFailure(null);
    setPhase("running");
    try {
      for (;;) {
        if (!captureRef.current.isSharing()) {
          setPhase("sharing_stopped");
          return;
        }
        let latest: ImportJob;
        try {
          latest = await getImportJob(jobId);
          setJob(latest);
        } catch (error) {
          setFailure({ step: "poll", message: messageOf(error) });
          setPhase("failure");
          return;
        }
        if (pauseRequestedRef.current) {
          pauseRequestedRef.current = false;
          setPauseRequested(false);
          try {
            setJob(await controlImportJob(jobId, "pause"));
          } catch (error) {
            setLoadError(messageOf(error));
          }
          captureRef.current.stop();
          setPhase("paused");
          return;
        }
        if (latest.status === "paused") {
          captureRef.current.stop();
          setPhase("paused");
          return;
        }
        const next = nextCaptureVideo(latest);
        if (!next) {
          captureRef.current.stop();
          setPhase("processing");
          return;
        }
        if (!(await runVideo(next))) return;
      }
    } finally {
      runningRef.current = false;
      setCurrent(null);
    }
  }, [jobId, runVideo]);

  /** Share the tab if needed, then capture every remaining video. */
  const begin = useCallback(async () => {
    if (!captureRef.current.isSharing()) {
      await captureRef.current.start();
      if (!captureRef.current.isSharing()) return; // cancelled, failed or no audio
    }
    await runLoop();
  }, [runLoop]);

  const retry = useCallback(async () => {
    if (failure?.step === "finish" && failure.video && failure.body) {
      setFailure(null);
      setPhase("running");
      if (!(await sendFinish(failure.video, failure.body))) return;
      if (!failure.body.ended && !failure.body.error_code && !captureRef.current.isSharing()) {
        setPhase("sharing_stopped");
        return;
      }
    }
    await begin();
  }, [begin, failure, sendFinish]);

  const resume = useCallback(async () => {
    try {
      setJob(await controlImportJob(jobId, "resume"));
    } catch (error) {
      setLoadError(messageOf(error));
      return;
    }
    await begin();
  }, [begin, jobId]);

  const requestPause = useCallback(() => {
    pauseRequestedRef.current = true;
    setPauseRequested(true);
  }, []);

  if (!job) {
    return (
      <div className="mx-auto w-full max-w-3xl p-6 text-sm text-[var(--muted-foreground)]">
        {loadError ? (
          <p className="text-[var(--destructive)]">{loadError}</p>
        ) : (
          <Loader2 className="h-4 w-4 animate-spin" />
        )}
      </div>
    );
  }

  const browserJob = job.capture_mode === "browser";
  const remaining = nextCaptureVideo(job);
  const needsPlayback = browserJob && !terminal && remaining !== null;
  const noAudio = capture.error === NO_AUDIO_TRACK;
  const shareError = capture.error && !noAudio ? capture.error : "";

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-4 overflow-y-auto p-6">
      <header>
        <h1 className="text-lg font-semibold text-[var(--foreground)]">{job.title}</h1>
        <p className="text-sm text-[var(--muted-foreground)]">
          {t("Knowledge base")}: {job.kb_name} · {t("Status")}: {t(`deep_import.status.${job.status}`)}
        </p>
        {job.error_code ? (
          <p className="text-sm text-[var(--destructive)]">
            {job.error_code}
            {job.error_detail ? ` — ${job.error_detail}` : ""}
          </p>
        ) : null}
      </header>

      {loadError ? <p className="text-sm text-[var(--destructive)]">{loadError}</p> : null}

      {noAudio ? (
        <Banner tone="danger">
          <p>
            {t(
              "The shared tab has no audio. Share again and keep \"Also share tab audio\" switched on.",
            )}
          </p>
          <ActionButton onClick={() => void begin()} icon={<MonitorUp className="h-4 w-4" />}>
            {t("Share this tab again")}
          </ActionButton>
        </Banner>
      ) : null}
      {shareError ? <Banner tone="danger">{shareError}</Banner> : null}
      {capture.frozen ? (
        <Banner tone="warning">
          {t("Keep this window visible — the captured picture stopped updating while it was hidden.")}
        </Banner>
      ) : null}
      {capture.inAd ? (
        <Banner tone="info">{t("Ad playing — recording is paused until the video resumes.")}</Banner>
      ) : null}
      {capture.failedFrames > 0 ? (
        <Banner tone="warning">
          {t("{{count}} frame uploads failed for this video.", { count: capture.failedFrames })}
        </Banner>
      ) : null}

      {failure ? (
        <Banner tone="danger">
          <p>
            {failure.video
              ? t("Capture of \"{{title}}\" failed: {{message}}", {
                  title: failure.video.title,
                  message: failure.message,
                })
              : failure.message}
          </p>
          <ActionButton onClick={() => void retry()} icon={<RotateCcw className="h-4 w-4" />}>
            {t("Retry this video")}
          </ActionButton>
        </Banner>
      ) : null}

      {needsPlayback && phase === "sharing_stopped" ? (
        <Banner tone="warning">
          <p>{t("Sharing stopped. The interrupted video will be captured again from the start.")}</p>
          <ActionButton onClick={() => void begin()} icon={<MonitorUp className="h-4 w-4" />}>
            {t("Share this tab again")}
          </ActionButton>
        </Banner>
      ) : null}

      {needsPlayback && phase === "idle" ? (
        <section className="rounded-lg border border-[var(--border)] bg-[var(--card)] p-4 text-sm">
          <ActionButton onClick={() => void begin()} icon={<MonitorUp className="h-4 w-4" />}>
            {t("Start: share this tab")}
          </ActionButton>
          <ul className="mt-3 list-disc space-y-1 pl-5 text-[var(--muted-foreground)]">
            <li>{t("When the browser asks, choose this tab.")}</li>
            <li>{t("Keep \"Also share tab audio\" switched on.")}</li>
            <li>{t("Keep this window visible while the videos play. You will still hear them.")}</li>
          </ul>
        </section>
      ) : null}

      {phase === "paused" && !terminal ? (
        <Banner tone="info">
          <p>{t("Import paused.")}</p>
          <ActionButton onClick={() => void resume()} icon={<Play className="h-4 w-4" />}>
            {t("Resume")}
          </ActionButton>
        </Banner>
      ) : null}

      {browserJob ? (
        <section className={current ? "" : "hidden"}>
          <div className="mb-2 flex items-center justify-between gap-2 text-sm">
            <span className="truncate text-[var(--foreground)]">
              {current ? t("Now capturing: {{title}}", { title: current.title }) : null}
            </span>
            <button
              type="button"
              onClick={requestPause}
              disabled={pauseRequested}
              className="inline-flex items-center gap-1 rounded-md border border-[var(--border)] px-3 py-1 text-sm disabled:opacity-50"
            >
              <Pause className="h-4 w-4" />
              {pauseRequested ? t("Pausing after this video…") : t("Pause")}
            </button>
          </div>
          <div
            ref={setPlayerElement}
            className="aspect-video w-full overflow-hidden rounded-lg bg-black"
          />
        </section>
      ) : null}

      {!needsPlayback && !terminal ? (
        <p className="flex items-center gap-2 text-sm text-[var(--muted-foreground)]">
          <Loader2 className="h-4 w-4 animate-spin" />
          {t("Processing — transcribing, describing frames and building the knowledge base.")}
        </p>
      ) : null}

      <ol className="divide-y divide-[var(--border)] rounded-lg border border-[var(--border)] text-sm">
        {job.videos
          .slice()
          .sort((a, b) => a.position - b.position)
          .map((video) => (
            <li key={video.video_id} className="flex items-center gap-3 px-3 py-2">
              <span className="w-6 text-right text-[var(--muted-foreground)]">{video.position}</span>
              <span className="min-w-0 flex-1 truncate text-[var(--foreground)]">{video.title}</span>
              <span className="text-[var(--muted-foreground)]">
                {formatDuration(video.duration_seconds)}
              </span>
              <span
                className={
                  video.stage === "failed" || video.stage === "skipped"
                    ? "text-[var(--destructive)]"
                    : "text-[var(--muted-foreground)]"
                }
                title={video.error_detail || undefined}
              >
                {t(`deep_import.stage.${video.stage}`)}
                {video.error_code ? ` (${video.error_code})` : ""}
              </span>
            </li>
          ))}
      </ol>
    </div>
  );
}

function Banner({
  tone,
  children,
}: {
  tone: "danger" | "warning" | "info";
  children: React.ReactNode;
}) {
  const color =
    tone === "danger"
      ? "border-[var(--destructive)] text-[var(--destructive)]"
      : tone === "warning"
        ? "border-amber-500 text-amber-700 dark:text-amber-300"
        : "border-[var(--border)] text-[var(--foreground)]";
  return (
    <div role={tone === "info" ? "status" : "alert"} className={`flex flex-col gap-2 rounded-lg border p-3 text-sm ${color}`}>
      {tone !== "info" ? <AlertTriangle className="h-4 w-4" /> : null}
      {children}
    </div>
  );
}

function ActionButton({
  onClick,
  icon,
  children,
}: {
  onClick: () => void;
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex w-fit items-center gap-2 rounded-md bg-[var(--foreground)] px-4 py-2 text-sm font-medium text-[var(--background)] hover:opacity-90"
    >
      {icon}
      {children}
    </button>
  );
}
